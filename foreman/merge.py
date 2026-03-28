"""Plan merging — branch merge, conflict resolution, finalization."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from foreman.brain import ForemanBrain
from foreman.config import Config
from foreman.coordination import CoordinationDB, PlanStatus
from foreman.plan_parser import Plan
from foreman.worktree import (
    abort_merge, complete_merge, get_conflict_files,
    get_merge_diff, merge_branch, merge_touched_self, remove_worktree,
)

log = logging.getLogger(__name__)


class PlanMerger:
    def __init__(
        self,
        db: CoordinationDB,
        brain: ForemanBrain,
        config: Config,
    ) -> None:
        self.db = db
        self.brain = brain
        self.config = config
        self.plans: dict[str, Plan] = {}
        self.restart_pending = False
        self._merge_lock = asyncio.Lock()
        self.on_failure: Callable[[str], None] | None = None

    async def merge_plan(self, plan_name: str) -> None:
        async with self._merge_lock:
            plan_data = self.db.get_plan(plan_name)
            if not plan_data:
                return

            branch = plan_data["branch"]
            log.info("Merging branch %s for plan %s", branch, plan_name)

            success, output, pre_merge_ref = await merge_branch(branch, self.config.repo_root)

            if success:
                await self._finalize_merge(plan_name, pre_merge_ref)
                return

            log.warning("Merge conflict for %s, invoking brain", plan_name)
            resolved = await self._brain_resolve_conflict(plan_name, branch)

            if resolved:
                await self._finalize_merge(plan_name, pre_merge_ref)
            else:
                await abort_merge(self.config.repo_root)
                self.db.set_plan_status(
                    plan_name, PlanStatus.BLOCKED,
                    reason=f"Merge conflict: {output[:200]}",
                )
                if self.on_failure:
                    self.on_failure(plan_name)

    async def _finalize_merge(self, plan_name: str, pre_merge_ref: str) -> None:
        log.info("Merged %s successfully", plan_name)
        self.db.set_plan_status(plan_name, PlanStatus.DONE)
        await remove_worktree(plan_name, self.config)
        self._archive_plan(plan_name)

        if self.config.auto_restart and await merge_touched_self(self.config.repo_root, pre_merge_ref):
            log.info("Merge of %s modified foreman/ — restart pending", plan_name)
            self.restart_pending = True

    def _archive_plan(self, plan_name: str) -> None:
        plan = self.plans.get(plan_name)
        if not plan or not plan.file_path.exists():
            return
        plan.file_path.unlink()
        log.info("Removed completed plan %s", plan.file_path.name)

    async def _brain_resolve_conflict(self, plan_name: str, branch: str) -> bool:
        conflict_files = await get_conflict_files(self.config.repo_root)
        if not conflict_files:
            return False

        diff = await get_merge_diff(self.config.repo_root)

        plan = self.plans.get(plan_name)
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
            files=conflict_files,
        )
        if not success:
            log.error("Failed to complete merge after resolution: %s", output)
            return False

        log.info("Brain resolved merge conflict for %s", plan_name)
        return True
