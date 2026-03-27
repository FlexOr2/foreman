"""Async event loop that ties everything together."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from asyncinotify import Mask

from foreman.brain import ForemanBrain
from foreman.config import Config
from foreman.coordination import AgentType, CoordinationDB, PlanStatus
from foreman.monitor import StuckDetector, watch_done, watch_logs, watch_plans
from foreman.plan_parser import Plan, load_plans
from foreman.resolver import CircularDependencyError, get_ready_plans, validate_dag
from foreman.spawner import Spawner, _log_filename
from foreman.worktree import abort_merge, create_worktree, merge_branch, remove_worktree

log = logging.getLogger(__name__)


class ForemanLoop:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = CoordinationDB(config.coordination_db)
        self.brain = ForemanBrain(
            config.coordination_db.parent,
            allowed_tools=config.allowed_tools.get("brain", "Read,Edit,Bash,Glob,Grep"),
            permission_mode=config.agents.permission_mode,
        )
        self.spawner = Spawner(config)
        self.stuck = StuckDetector(
            config.timeouts.stuck_threshold,
            on_stuck=self._on_agent_stuck,
        )
        self._plans: dict[str, Plan] = {}
        self._schedule_event = asyncio.Event()
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        self.config.ensure_dirs()
        await self.spawner.setup()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_shutdown)

        await self._scan_plans()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._plan_watcher())
                tg.create_task(self._log_watcher())
                tg.create_task(self._done_watcher())
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

        self.brain.save()
        self.db.close()
        await self.spawner.teardown()
        log.info("Shutdown complete. tmux session left alive for manual inspection.")

    # --- Plan scanning ---

    async def _scan_plans(self) -> None:
        plans = load_plans(self.config.plans_dir)

        try:
            validate_dag(plans)
        except CircularDependencyError as e:
            log.error("Circular dependency: %s", e)
            return

        self._plans = {p.name: p for p in plans}

        known_plans = {p["plan"] for p in self.db.get_all_plans()}
        for plan in plans:
            if plan.name not in known_plans:
                self.db.upsert_plan(plan.name, PlanStatus.QUEUED)
                log.info("New plan detected: %s", plan.name)

    async def _plan_watcher(self) -> None:
        async def on_plan_event(file_path: Path, mask: Mask) -> None:
            name = file_path.stem

            if mask & (Mask.CREATE | Mask.MOVED_TO):
                log.info("New plan file: %s", file_path.name)
                await self._scan_plans()
                self._schedule_event.set()

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

        await watch_plans(self.config.plans_dir, on_plan_event)

    async def _log_watcher(self) -> None:
        await watch_logs(self.config.log_dir, self.stuck.on_log_activity)

    async def _done_watcher(self) -> None:
        done_dir = self.config.repo_root / ".foreman" / "done"

        async def on_done(plan_name: str) -> None:
            exit_code = self._read_exit_code(plan_name)
            log.info("Agent for %s finished (exit code: %s)", plan_name, exit_code)

            self.stuck.cancel(plan_name)

            if exit_code == 0:
                await self._on_agent_success(plan_name)
            else:
                self.db.set_plan_status(plan_name, PlanStatus.FAILED)
                log.error("Agent for %s failed (exit code %s)", plan_name, exit_code)

            self._schedule_event.set()

        await watch_done(done_dir, on_done)

    def _read_exit_code(self, plan_name: str) -> int:
        done_file = self.config.repo_root / ".foreman" / "done" / plan_name
        try:
            return int(done_file.read_text().strip())
        except (ValueError, FileNotFoundError):
            return 0

    # --- Scheduling ---

    async def _scheduler(self) -> None:
        while not self._shutdown.is_set():
            await self._try_spawn_ready()
            self._schedule_event.clear()
            done, _ = await asyncio.wait(
                [
                    asyncio.create_task(self._schedule_event.wait()),
                    asyncio.create_task(self._shutdown.wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in _:
                task.cancel()

    async def _try_spawn_ready(self) -> None:
        completed = self.db.get_completed_plan_names()
        running = self.db.get_running_plan_names()

        ready = get_ready_plans(list(self._plans.values()), completed, running)

        worker_count = sum(
            1 for name in running
            if self.db.get_plan_status(name) == PlanStatus.RUNNING
        )

        for plan in ready:
            if worker_count >= self.config.agents.max_parallel_workers:
                break
            await self._spawn_implementation(plan)
            worker_count += 1

    async def _spawn_implementation(self, plan: Plan) -> None:
        log.info("Spawning implementation agent for %s", plan.name)

        worktree_path, branch = await create_worktree(plan.name, self.config)

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
            plan, worktree_path, AgentType.IMPLEMENTATION, initial_message,
        )

        self.db.add_agent(
            plan.name, AgentType.IMPLEMENTATION,
            pid=pid,
            log_file=str(self.config.log_dir / _log_filename(plan.name, AgentType.IMPLEMENTATION)),
        )

        self.stuck.track(plan.name)

    async def _on_agent_success(self, plan_name: str) -> None:
        plan_data = self.db.get_plan(plan_name)
        if not plan_data:
            return

        branch = plan_data["branch"]
        log.info("Merging branch %s for plan %s", branch, plan_name)

        success, output = await merge_branch(branch, self.config.repo_root)

        if success:
            log.info("Merged %s successfully", branch)
            self.db.set_plan_status(plan_name, PlanStatus.DONE)
            await remove_worktree(plan_name, self.config)
        else:
            log.warning("Merge conflict for %s: %s", plan_name, output)
            await abort_merge(self.config.repo_root)
            self.db.set_plan_status(
                plan_name, PlanStatus.BLOCKED,
                reason=f"Merge conflict: {output[:200]}",
            )

    async def _on_agent_stuck(self, plan_name: str) -> None:
        log.warning("Agent %s is stuck — surfacing in dashboard", plan_name)
