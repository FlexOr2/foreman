"""Agent scheduling — spawning, slot management, review queuing."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from foreman.config import Config
from foreman.coordination import AgentType, CoordinationDB, PlanStatus, ReviewVerdict
from foreman.monitor import StuckDetector
from foreman.plan_parser import Plan
from foreman.resolver import get_ready_plans
from foreman.spawner import Spawner, log_filename
from foreman.worktree import branch_has_commits, create_worktree, remove_worktree

log = logging.getLogger(__name__)


class AgentScheduler:
    def __init__(
        self,
        db: CoordinationDB,
        spawner: Spawner,
        config: Config,
        stuck: StuckDetector,
    ) -> None:
        self.db = db
        self.spawner = spawner
        self.config = config
        self.stuck = stuck
        self.plans: dict[str, Plan] = {}
        self.pending_reviews: set[str] = set()
        self.active_agent_ids: dict[str, int] = {}
        self.schedule_event = asyncio.Event()
        self.on_merge: Callable[[str], Coroutine[Any, Any, None]] | None = None

    async def try_spawn_ready(self) -> None:
        completed = self.db.get_completed_plan_names()
        running = self.db.get_in_progress_plan_names()
        ready = get_ready_plans(list(self.plans.values()), completed, running)

        worker_count = sum(
            1 for name in running
            if self.db.get_plan_status(name) == PlanStatus.RUNNING
        )

        for plan in ready:
            if worker_count >= self.config.agents.max_parallel_workers:
                break
            try:
                await self.spawn_implementation(plan)
                worker_count += 1
            except Exception:
                log.error("Failed to spawn %s", plan.name, exc_info=True)
                self.db.set_plan_status(plan.name, PlanStatus.FAILED)
                self.cascade_failure(plan.name)

        reviewing_count = len(self.db.get_plans_by_status(PlanStatus.REVIEWING))
        while self.pending_reviews and reviewing_count < self.config.agents.max_parallel_reviews:
            plan_name = self.pending_reviews.pop()
            try:
                await self.spawn_review(plan_name)
            except Exception:
                log.error("Failed to spawn review for %s", plan_name, exc_info=True)
                self.pending_reviews.add(plan_name)
                break
            reviewing_count += 1

    async def drain_pending_reviews(self) -> None:
        reviewing_count = len(self.db.get_plans_by_status(PlanStatus.REVIEWING))
        while self.pending_reviews and reviewing_count < self.config.agents.max_parallel_reviews:
            plan_name = self.pending_reviews.pop()
            try:
                await self.spawn_review(plan_name)
            except Exception:
                log.error("Failed to spawn review for %s", plan_name, exc_info=True)
                self.pending_reviews.add(plan_name)
                break
            reviewing_count += 1

    async def spawn_implementation(self, plan: Plan) -> None:
        log.info("Spawning implementation agent for %s", plan.name)

        worktree_path, branch = await create_worktree(plan.name, self.config)

        plan_file = plan.file_path.resolve()
        initial_message = (
            f"Read and implement the plan at {plan_file}. "
            f"Branch: {branch}. "
            f"Commit all your changes when done."
        )

        try:
            pid = await self.spawner.spawn_agent(
                plan, worktree_path, AgentType.IMPLEMENTATION, initial_message,
            )

            with self.db.tx():
                self.db.upsert_plan(
                    plan.name,
                    PlanStatus.RUNNING,
                    branch=branch,
                    worktree_path=str(worktree_path),
                )
                agent_id = self.db.add_agent(
                    plan.name, AgentType.IMPLEMENTATION,
                    pid=pid,
                    log_file=str(self.config.log_dir / log_filename(plan.name, AgentType.IMPLEMENTATION)),
                )
                self.active_agent_ids[plan.name] = agent_id
        except BaseException:
            try:
                await remove_worktree(plan.name, self.config)
            except Exception:
                log.warning("Failed to clean up worktree for %s", plan.name, exc_info=True)
            raise

        self.stuck.track(plan.name)
        timeout = self.config.get_timeout(plan.name, AgentType.IMPLEMENTATION)
        if timeout > 0:
            self.stuck.track_timeout(plan.name, timeout)

    async def on_implementation_done(self, plan_name: str) -> None:
        if self.config.agents.auto_review:
            self.pending_reviews.add(plan_name)
            self.schedule_event.set()
        elif self.on_merge:
            await self.on_merge(plan_name)

    async def spawn_review(self, plan_name: str) -> None:
        plan = self.plans.get(plan_name)
        plan_data = self.db.get_plan(plan_name)
        if not plan or not plan_data:
            return

        await self.spawner.kill_agent(plan_name, AgentType.IMPLEMENTATION)
        await self.spawner.kill_agent(plan_name, AgentType.FIX)

        worktree_path = Path(plan_data["worktree_path"])
        plan_file = plan.file_path.resolve()
        initial_message = (
            f"Review the changes on this branch against main. "
            f"The original plan is at {plan_file}."
        )

        pid = await self.spawner.spawn_agent(
            plan, worktree_path, AgentType.REVIEW, initial_message,
        )

        with self.db.tx():
            self.db.set_plan_status(plan_name, PlanStatus.REVIEWING)
            agent_id = self.db.add_agent(
                plan_name, AgentType.REVIEW,
                pid=pid,
                log_file=str(self.config.log_dir / log_filename(plan_name, AgentType.REVIEW)),
            )
            self.active_agent_ids[plan_name] = agent_id

        self.stuck.track(plan_name)
        timeout = self.config.get_timeout(plan_name, AgentType.REVIEW)
        if timeout > 0:
            self.stuck.track_timeout(plan_name, timeout)
        log.info("Spawned review agent for %s", plan_name)

    async def on_review_done(self, plan_name: str) -> None:
        plan_data = self.db.get_plan(plan_name)
        if not plan_data:
            return

        verdict = self._read_review_verdict(plan_data["worktree_path"])

        if verdict is None:
            self.db.set_plan_status(
                plan_name, PlanStatus.BLOCKED,
                reason="Review verdict unreadable or missing",
            )
            self.cascade_failure(plan_name)
            return

        raw_decision = verdict.get("verdict", "")
        try:
            decision = ReviewVerdict(raw_decision.strip().lower())
        except ValueError:
            self.db.set_plan_status(
                plan_name, PlanStatus.BLOCKED,
                reason=f"Unknown review verdict: {raw_decision!r}",
            )
            self.cascade_failure(plan_name)
            return

        if decision == ReviewVerdict.CLEAN:
            log.info("Review passed for %s", plan_name)
            if self.on_merge:
                await self.on_merge(plan_name)

        elif decision == ReviewVerdict.FINDINGS:
            review_count = self._get_review_count(plan_name)
            if review_count > self.config.agents.max_review_retries:
                log.warning("Max review retries reached for %s", plan_name)
                self.db.set_plan_status(
                    plan_name, PlanStatus.BLOCKED,
                    reason="Max review retries exceeded",
                )
                self.cascade_failure(plan_name)
            else:
                issues = verdict.get("issues", [])
                log.info("Review found %d issues for %s, spawning fix agent", len(issues), plan_name)
                try:
                    await self.spawn_fix(plan_name, issues)
                except Exception:
                    log.error("Failed to spawn fix agent for %s", plan_name, exc_info=True)
                    self.db.set_plan_status(plan_name, PlanStatus.FAILED)
                    self.cascade_failure(plan_name)

        elif decision == ReviewVerdict.ARCHITECTURAL:
            reason = verdict.get("reason", "Architectural problem")
            log.warning("Architectural issue in %s: %s", plan_name, reason)
            self.db.set_plan_status(plan_name, PlanStatus.BLOCKED, reason=reason)
            self.cascade_failure(plan_name)

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

    async def spawn_fix(self, plan_name: str, issues: list[str]) -> None:
        plan = self.plans.get(plan_name)
        plan_data = self.db.get_plan(plan_name)
        if not plan or not plan_data:
            return

        await self.spawner.kill_agent(plan_name, AgentType.REVIEW)

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

        with self.db.tx():
            self.db.set_plan_status(plan_name, PlanStatus.RUNNING)
            agent_id = self.db.add_agent(
                plan_name, AgentType.FIX,
                pid=pid,
                log_file=str(self.config.log_dir / log_filename(plan_name, AgentType.FIX)),
            )
            self.active_agent_ids[plan_name] = agent_id

        self.stuck.track(plan_name)
        timeout = self.config.get_timeout(plan_name, AgentType.FIX)
        if timeout > 0:
            self.stuck.track_timeout(plan_name, timeout)
        log.info("Spawned fix agent for %s", plan_name)

    async def on_fix_done(self, plan_name: str) -> None:
        self.pending_reviews.add(plan_name)
        self.schedule_event.set()

    async def on_review_failure(self, plan_name: str) -> bool:
        branch = self.db.get_plan(plan_name)["branch"]
        has_commits = await branch_has_commits(branch, self.config.repo_root)
        log.debug("on_review_failure: %s branch=%s has_commits=%s", plan_name, branch, has_commits)
        if has_commits:
            self.db.set_plan_status(plan_name, PlanStatus.RUNNING)
            self.pending_reviews.add(plan_name)
            self.schedule_event.set()
            return True
        return False

    def cascade_failure(self, failed_plan: str) -> None:
        failed_status = self.db.get_plan_status(failed_plan)
        queue = [failed_plan]
        while queue:
            current = queue.pop()
            for plan in self.plans.values():
                if current in plan.depends_on:
                    status = self.db.get_plan_status(plan.name)
                    if status == PlanStatus.QUEUED:
                        self.db.set_plan_status(
                            plan.name, PlanStatus.BLOCKED,
                            reason=f"Dependency '{failed_plan}' is {failed_status}",
                        )
                        queue.append(plan.name)

    def finish_agent(self, plan_name: str) -> int | None:
        return self.active_agent_ids.pop(plan_name, None)
