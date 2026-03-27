"""Async event loop that ties everything together."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from asyncinotify import Mask

from foreman.brain import ForemanBrain
from foreman.innovate import innovate
from foreman.config import Config
from foreman.coordination import AgentType, CoordinationDB, PlanStatus, ReviewVerdict
from foreman.dashboard import run_dashboard
from foreman.monitor import StuckDetector, watch_done, watch_logs, watch_plans
from foreman.plan_parser import Plan, load_plans
from foreman.resolver import CircularDependencyError, get_ready_plans, validate_dag
from foreman.spawner import AGENT_TYPE_SEP, Spawner, _log_filename
from foreman.worktree import (
    abort_merge, complete_merge, create_worktree, get_conflict_files,
    get_merge_diff, merge_branch, merge_touched_self, remove_worktree,
)

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
        self._pending_reviews: set[str] = set()
        self._restart_pending = False
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
                tg.create_task(self._innovator_loop())
                tg.create_task(run_dashboard(self.config, self.db, self._shutdown))
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

        try:
            await self.brain.summarize_and_reset()
        except Exception:
            log.warning("Brain summarization failed", exc_info=True)
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
                    agent_type = self.db.get_active_agent_type(name) or AgentType.IMPLEMENTATION
                    log.info("Plan %s modified while running, notifying %s agent", name, agent_type.value)
                    await self.spawner.notify_agent(
                        name, agent_type,
                        f"The plan has been updated. Re-read {file_path} and adapt your approach.",
                    )
                else:
                    await self._scan_plans()

        await watch_plans(self.config.plans_dir, on_plan_event)

    async def _log_watcher(self) -> None:
        await watch_logs(self.config.log_dir, self.stuck.on_log_activity)

    async def _done_watcher(self) -> None:
        done_dir = self.config.repo_root / ".foreman" / "done"

        async def on_done(sentinel_name: str) -> None:
            if AGENT_TYPE_SEP in sentinel_name:
                plan_name, type_str = sentinel_name.split(AGENT_TYPE_SEP, 1)
                agent_type = AgentType(type_str)
            else:
                plan_name = sentinel_name
                agent_type = AgentType.IMPLEMENTATION

            exit_code = self._read_exit_code(sentinel_name)
            log.info(
                "Agent %s/%s finished (exit code: %s)",
                plan_name, agent_type.value, exit_code,
            )

            self.stuck.cancel(plan_name)

            sentinel_file = done_dir / sentinel_name
            sentinel_file.unlink(missing_ok=True)

            if exit_code != 0:
                self.db.set_plan_status(plan_name, PlanStatus.FAILED)
                log.error("Agent %s/%s failed (exit code %s)", plan_name, agent_type.value, exit_code)
            elif agent_type == AgentType.IMPLEMENTATION:
                await self._on_implementation_done(plan_name)
            elif agent_type == AgentType.REVIEW:
                await self._on_review_done(plan_name)
            elif agent_type == AgentType.FIX:
                await self._on_fix_done(plan_name)

            self._schedule_event.set()

        await watch_done(done_dir, on_done)

    def _read_exit_code(self, sentinel_name: str) -> int:
        done_file = self.config.repo_root / ".foreman" / "done" / sentinel_name
        try:
            return int(done_file.read_text().strip())
        except (ValueError, FileNotFoundError):
            return 0

    # --- Scheduling ---

    async def _scheduler(self) -> None:
        while not self._shutdown.is_set():
            self._schedule_event.clear()
            await self._try_spawn_ready()
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(self._schedule_event.wait()),
                    asyncio.create_task(self._shutdown.wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    async def _try_spawn_ready(self) -> None:
        if self._restart_pending:
            await self._try_restart()
            return

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
            try:
                await self._spawn_implementation(plan)
                worker_count += 1
            except Exception:
                log.error("Failed to spawn %s", plan.name, exc_info=True)
                self.db.set_plan_status(plan.name, PlanStatus.FAILED)

        reviewing_count = len(self.db.get_plans_by_status(PlanStatus.REVIEWING))
        while self._pending_reviews and reviewing_count < self.config.agents.max_parallel_reviews:
            plan_name = self._pending_reviews.pop()
            await self._spawn_review(plan_name)
            reviewing_count += 1

    # --- Implementation agents ---

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

    async def _on_implementation_done(self, plan_name: str) -> None:
        if self.config.agents.auto_review:
            self._pending_reviews.add(plan_name)
            self._schedule_event.set()
        else:
            await self._merge_plan(plan_name)

    # --- Review agents ---

    async def _spawn_review(self, plan_name: str) -> None:
        plan = self._plans.get(plan_name)
        plan_data = self.db.get_plan(plan_name)
        if not plan or not plan_data:
            return

        await self.spawner.kill_agent(plan_name, AgentType.IMPLEMENTATION)
        await self.spawner.kill_agent(plan_name, AgentType.FIX)

        self.db.set_plan_status(plan_name, PlanStatus.REVIEWING)

        worktree_path = Path(plan_data["worktree_path"])
        plan_file = plan.file_path.resolve()
        initial_message = (
            f"Review the changes on this branch against main. "
            f"The original plan is at {plan_file}."
        )

        pid = await self.spawner.spawn_agent(
            plan, worktree_path, AgentType.REVIEW, initial_message,
        )

        self.db.add_agent(
            plan_name, AgentType.REVIEW,
            pid=pid,
            log_file=str(self.config.log_dir / _log_filename(plan_name, AgentType.REVIEW)),
        )

        self.stuck.track(plan_name)
        log.info("Spawned review agent for %s", plan_name)

    async def _on_review_done(self, plan_name: str) -> None:
        plan_data = self.db.get_plan(plan_name)
        if not plan_data:
            return

        verdict = self._read_review_verdict(plan_data["worktree_path"])

        if verdict is None:
            self.db.set_plan_status(
                plan_name, PlanStatus.BLOCKED,
                reason="Review verdict unreadable or missing",
            )
            return

        decision = verdict.get("verdict")

        if decision == ReviewVerdict.CLEAN:
            log.info("Review passed for %s", plan_name)
            await self._merge_plan(plan_name)

        elif decision == ReviewVerdict.FINDINGS:
            review_count = self._get_review_count(plan_name)
            if review_count >= self.config.agents.max_review_retries:
                log.warning("Max review retries reached for %s", plan_name)
                self.db.set_plan_status(
                    plan_name, PlanStatus.BLOCKED,
                    reason="Max review retries exceeded",
                )
            else:
                issues = verdict.get("issues", [])
                log.info("Review found %d issues for %s, spawning fix agent", len(issues), plan_name)
                await self._spawn_fix(plan_name, issues)

        elif decision == ReviewVerdict.ARCHITECTURAL:
            reason = verdict.get("reason", "Architectural problem")
            log.warning("Architectural issue in %s: %s", plan_name, reason)
            self.db.set_plan_status(plan_name, PlanStatus.BLOCKED, reason=reason)

        else:
            self.db.set_plan_status(
                plan_name, PlanStatus.BLOCKED,
                reason=f"Unknown review verdict: {decision}",
            )

    def _read_review_verdict(self, worktree_path: str) -> dict | None:
        verdict_file = Path(worktree_path) / "REVIEW_VERDICT.json"
        try:
            return json.loads(verdict_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            log.warning("Could not read REVIEW_VERDICT.json from %s", worktree_path)
            return None

    def _get_review_count(self, plan_name: str) -> int:
        agents = self.db.get_agents_for_plan(plan_name)
        return sum(1 for a in agents if a["type"] == AgentType.REVIEW)

    # --- Fix agents ---

    async def _spawn_fix(self, plan_name: str, issues: list[str]) -> None:
        plan = self._plans.get(plan_name)
        plan_data = self.db.get_plan(plan_name)
        if not plan or not plan_data:
            return

        await self.spawner.kill_agent(plan_name, AgentType.REVIEW)

        self.db.set_plan_status(plan_name, PlanStatus.RUNNING)

        worktree_path = Path(plan_data["worktree_path"])
        plan_file = plan.file_path.resolve()
        initial_message = (
            f"Fix the issues found during review. "
            f"Original plan: {plan_file}. "
            f"Review findings: {json.dumps(issues)}"
        )

        pid = await self.spawner.spawn_agent(
            plan, worktree_path, AgentType.FIX, initial_message,
        )

        self.db.add_agent(
            plan_name, AgentType.FIX,
            pid=pid,
            log_file=str(self.config.log_dir / _log_filename(plan_name, AgentType.FIX)),
        )

        self.stuck.track(plan_name)
        log.info("Spawned fix agent for %s", plan_name)

    async def _on_fix_done(self, plan_name: str) -> None:
        self._pending_reviews.add(plan_name)
        self._schedule_event.set()

    # --- Merge ---

    async def _merge_plan(self, plan_name: str) -> None:
        plan_data = self.db.get_plan(plan_name)
        if not plan_data:
            return

        branch = plan_data["branch"]
        log.info("Merging branch %s for plan %s", branch, plan_name)

        success, output = await merge_branch(branch, self.config.repo_root)

        if success:
            await self._finalize_merge(plan_name)
            return

        log.warning("Merge conflict for %s, invoking brain", plan_name)
        resolved = await self._brain_resolve_conflict(plan_name, branch)

        if resolved:
            await self._finalize_merge(plan_name)
        else:
            await abort_merge(self.config.repo_root)
            self.db.set_plan_status(
                plan_name, PlanStatus.BLOCKED,
                reason=f"Merge conflict: {output[:200]}",
            )

    async def _finalize_merge(self, plan_name: str) -> None:
        log.info("Merged %s successfully", plan_name)
        self.db.set_plan_status(plan_name, PlanStatus.DONE)
        await remove_worktree(plan_name, self.config)

        if self.config.auto_restart and await merge_touched_self(self.config.repo_root):
            log.info("Merge of %s modified foreman/ — restart pending", plan_name)
            self._restart_pending = True

    async def _try_restart(self) -> None:
        running = self.db.get_running_plan_names()
        if running:
            log.info("Restart pending — waiting for %d active agents to finish", len(running))
            return

        if self._pending_reviews:
            log.info("Restart pending — waiting for %d pending reviews", len(self._pending_reviews))
            return

        log.info("All agents finished — preparing to restart")

        try:
            await self.brain.summarize_and_reset()
        except Exception:
            log.warning("Brain summarization failed before restart", exc_info=True)
            self.brain.save()

        self.db.close()
        await self.spawner.teardown()

        log.info("Restarting foreman to apply self-improvements")
        os.execv(sys.executable, [sys.executable, "-m", "foreman.cli", "start"])

    async def _brain_resolve_conflict(self, plan_name: str, branch: str) -> bool:
        conflict_files = await get_conflict_files(self.config.repo_root)
        if not conflict_files:
            return False

        diff = await get_merge_diff(self.config.repo_root)

        plan = self._plans.get(plan_name)
        plan_context = ""
        if plan:
            try:
                plan_context = plan.file_path.read_text()
            except FileNotFoundError:
                pass

        prompt = (
            f"A merge conflict occurred merging branch '{branch}' into main.\n\n"
            f"Conflicting files: {', '.join(conflict_files)}\n\n"
            f"Diff with conflict markers:\n```\n{diff[:8000]}\n```\n\n"
        )
        if plan_context:
            prompt += f"Original plan:\n```\n{plan_context[:4000]}\n```\n\n"
        prompt += (
            "Resolve the conflicts in the listed files. "
            "Edit each file to remove all conflict markers (<<<<<<, =======, >>>>>>>) "
            "and produce the correct merged result. "
            "If the conflict is too complex or ambiguous to resolve safely, "
            "respond with exactly: CANNOT_RESOLVE"
        )

        try:
            response = await self.brain.think(prompt)
        except Exception:
            log.error("Brain failed during conflict resolution for %s", plan_name, exc_info=True)
            return False

        if "CANNOT_RESOLVE" in response:
            log.warning("Brain cannot resolve conflict for %s", plan_name)
            return False

        remaining = await get_conflict_files(self.config.repo_root)
        if remaining:
            log.warning("Brain left unresolved conflicts in: %s", remaining)
            return False

        success, output = await complete_merge(
            self.config.repo_root,
            f"Merge branch '{branch}' (conflict resolved by Foreman brain)",
        )
        if not success:
            log.error("Failed to complete merge after resolution: %s", output)
            return False

        log.info("Brain resolved merge conflict for %s", plan_name)
        return True

    # --- Background innovation ---

    def _count_drafts(self) -> int:
        return sum(1 for _ in self.config.plans_dir.glob("draft-*.md"))

    async def _innovator_loop(self) -> None:
        if not self.config.innovate.enabled:
            return

        while not self._shutdown.is_set():
            if self._restart_pending:
                return

            if self._count_drafts() < self.config.innovate.max_drafts:
                await innovate(
                    self.config,
                    skip_review=self.config.innovate.skip_review,
                )

            await self._wait_for_interval(self.config.innovate.interval)

    async def _wait_for_interval(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _on_agent_stuck(self, plan_name: str) -> None:
        log.warning("Agent %s is stuck — surfacing in dashboard", plan_name)
