"""Async event loop that ties everything together."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from pathlib import Path

from asyncinotify import Mask

from foreman.brain import ForemanBrain
from foreman.config import Config
from foreman.coordination import AgentType, CoordinationDB, PlanStatus
from foreman.monitor import StuckDetector, watch_logs, watch_plans, wait_for_process
from foreman.plan_parser import Plan, load_plans
from foreman.resolver import CircularDependencyError, get_ready_plans, validate_dag
from foreman.spawner import Spawner
from foreman.worktree import (
    abort_merge,
    create_worktree,
    merge_branch,
    remove_worktree,
)

log = logging.getLogger(__name__)


class ForemanLoop:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = CoordinationDB(config.coordination_db)
        self.brain = ForemanBrain(
            config.coordination_db.parent,
            allowed_tools=config.allowed_tools.get("brain", "Read,Edit,Bash,Glob,Grep"),
        )
        self.spawner = Spawner(config)
        self.stuck = StuckDetector(
            config.timeouts.stuck_threshold,
            on_stuck=self._on_agent_stuck,
        )
        self._plans: dict[str, Plan] = {}
        self._agent_tasks: dict[str, asyncio.Task] = {}
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        """Main entry point. Runs until shutdown signal."""
        self.config.ensure_dirs()
        await self.spawner.setup()

        # Install signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_shutdown)

        # Initial plan scan
        await self._scan_plans()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._plan_watcher())
                tg.create_task(self._log_watcher())
                tg.create_task(self._scheduler())
                tg.create_task(self._shutdown_waiter())
        except* KeyboardInterrupt:
            pass
        finally:
            await self._graceful_shutdown()

    def _request_shutdown(self) -> None:
        log.info("Shutdown requested")
        self._shutdown.set()

    async def _shutdown_waiter(self) -> None:
        await self._shutdown.wait()
        raise KeyboardInterrupt

    async def _graceful_shutdown(self) -> None:
        log.info("Shutting down...")
        self.stuck.cancel_all()

        count = self.db.mark_all_running_as_interrupted()
        if count:
            log.info("Marked %d plans as INTERRUPTED", count)

        # Cancel agent watchers
        for task in self._agent_tasks.values():
            task.cancel()

        self.brain._save_session_id()
        self.db.close()
        await self.spawner.teardown()
        log.info("Shutdown complete. tmux session left alive for manual inspection.")

    # --- Plan scanning ---

    async def _scan_plans(self) -> None:
        """Load plans from disk, update DB, check DAG."""
        plans = load_plans(self.config.plans_dir)

        try:
            validate_dag(plans)
        except CircularDependencyError as e:
            log.error("Circular dependency: %s", e)
            return

        self._plans = {p.name: p for p in plans}

        # Register new plans in DB
        for plan in plans:
            existing = self.db.get_plan_status(plan.name)
            if existing is None:
                self.db.upsert_plan(plan.name, PlanStatus.QUEUED)
                log.info("New plan detected: %s", plan.name)

    async def _plan_watcher(self) -> None:
        """Watch plans/ directory for changes."""
        async def on_plan_event(file_path: Path, mask: Mask) -> None:
            name = file_path.stem

            if file_path.name.startswith("draft-"):
                return
            if file_path.name.startswith("prompt-"):
                return

            if mask & (Mask.CREATE | Mask.MOVED_TO):
                log.info("New plan file: %s", file_path.name)
                await self._scan_plans()
                # Trigger scheduler
                await self._try_spawn_ready()

            elif mask & Mask.MODIFY:
                status = self.db.get_plan_status(name)
                if status == PlanStatus.RUNNING:
                    log.info("Plan %s modified while running, notifying agent", name)
                    await self.spawner.notify_agent(
                        name,
                        f"The plan has been updated. Re-read {file_path} and adapt your approach.",
                    )
                else:
                    await self._scan_plans()

            elif mask & Mask.DELETE:
                log.info("Plan file deleted: %s", file_path.name)

        await watch_plans(self.config.plans_dir, on_plan_event)

    async def _log_watcher(self) -> None:
        """Watch log files for stuck detection."""
        await watch_logs(self.config.log_dir, self.stuck.on_log_activity)

    # --- Scheduling ---

    async def _scheduler(self) -> None:
        """Periodically check if new plans can be spawned."""
        while not self._shutdown.is_set():
            await self._try_spawn_ready()
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=5)
                break
            except asyncio.TimeoutError:
                pass

    async def _try_spawn_ready(self) -> None:
        """Spawn agents for ready plans if slots available."""
        completed = self.db.get_completed_plan_names()
        running = self.db.get_running_plan_names()

        all_plans = list(self._plans.values())
        ready = get_ready_plans(all_plans, completed, running)

        # Count current workers
        worker_count = len([
            p for p in self.db.get_plans_by_status(PlanStatus.RUNNING)
        ])
        review_count = len([
            p for p in self.db.get_plans_by_status(PlanStatus.REVIEWING)
        ])

        for plan in ready:
            if worker_count >= self.config.agents.max_parallel_workers:
                break
            await self._spawn_implementation(plan)
            worker_count += 1

    async def _spawn_implementation(self, plan: Plan) -> None:
        """Create worktree and spawn implementation agent."""
        log.info("Spawning implementation agent for %s", plan.name)

        worktree_path, branch = await create_worktree(
            plan.name,
            self.config.branch_prefix,
            self.config.worktree_dir,
            self.config.repo_root,
        )

        self.db.upsert_plan(
            plan.name,
            PlanStatus.RUNNING,
            branch=branch,
            worktree_path=str(worktree_path),
        )

        plan_file = plan.file_path.resolve()
        initial_message = (
            f"Read and implement the plan at {plan_file}. "
            f"Branch: {branch}. "
            f"Commit all your changes when done."
        )

        pid = await self.spawner.spawn_agent(
            plan, worktree_path, "implementation", initial_message,
        )

        agent_id = self.db.add_agent(
            plan.name, AgentType.IMPLEMENTATION,
            pid=pid,
            log_file=str(self.config.log_dir / f"{plan.name}-implementation.log"),
        )

        # Start watching for process exit
        if pid:
            task = asyncio.create_task(self._watch_agent(plan.name, pid, agent_id))
            self._agent_tasks[plan.name] = task

    async def _watch_agent(self, plan_name: str, pid: int, agent_id: int) -> None:
        """Watch an agent process and handle its exit."""
        exit_code = await wait_for_process(pid)
        log.info("Agent for %s exited with code %d", plan_name, exit_code)

        self.db.finish_agent(agent_id, exit_code)
        self.stuck.cancel(plan_name)
        self._agent_tasks.pop(plan_name, None)

        if exit_code == 0:
            await self._on_agent_success(plan_name)
        else:
            self.db.set_plan_status(plan_name, PlanStatus.FAILED)
            log.error("Agent for %s failed (exit code %d)", plan_name, exit_code)

    async def _on_agent_success(self, plan_name: str) -> None:
        """Handle successful agent completion — merge the branch."""
        plan_data = self.db.get_plan(plan_name)
        if not plan_data:
            return

        branch = plan_data["branch"]
        log.info("Merging branch %s for plan %s", branch, plan_name)

        success, output = await merge_branch(branch, self.config.repo_root)

        if success:
            log.info("Merged %s successfully", branch)
            self.db.set_plan_status(plan_name, PlanStatus.DONE)

            # Clean up worktree
            await remove_worktree(
                plan_name,
                self.config.worktree_dir,
                self.config.branch_prefix,
                self.config.repo_root,
            )

            # Check if new plans are unblocked
            await self._try_spawn_ready()
        else:
            log.warning("Merge conflict for %s: %s", plan_name, output)
            await abort_merge(self.config.repo_root)
            self.db.set_plan_status(
                plan_name, PlanStatus.BLOCKED,
                reason=f"Merge conflict: {output[:200]}",
            )

    async def _on_agent_stuck(self, plan_name: str) -> None:
        """Handle stuck agent detection."""
        log.warning("Agent %s is stuck — surfacing in dashboard", plan_name)
        # For now, just log. Dashboard will pick it up from DB.
        # Don't change status — user should intervene in the terminal.
