"""Plan merging — branch merge, conflict resolution, finalization."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from foreman.config import Config
from foreman.coordination import CoordinationDB, PlanStatus
from foreman.plan_parser import Plan
from foreman.worktree import abort_merge, merge_branch, merge_touched_self, remove_worktree

log = logging.getLogger(__name__)


class PlanMerger:
    def __init__(
        self,
        db: CoordinationDB,
        config: Config,
    ) -> None:
        self.db = db
        self.config = config
        self.plans: dict[str, Plan] = {}
        self._restart_requested_at: float | None = None
        self._merge_lock = asyncio.Lock()
        self.on_failure: Callable[[str], None] | None = None
        self.on_rebase_needed: Callable[[str], Coroutine[Any, Any, None]] | None = None

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

            log.warning("Merge conflict for %s, spawning rebase agent", plan_name)
            await abort_merge(self.config.repo_root)
            self.db.set_plan_status(plan_name, PlanStatus.RUNNING)
            if self.on_rebase_needed:
                await self.on_rebase_needed(plan_name)
            else:
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
            log.info("Merge of %s modified foreman/ — restart requested", plan_name)
            self._restart_requested_at = time.monotonic()

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested_at is not None

    def should_restart(self) -> bool:
        if self._restart_requested_at is None:
            return False
        elapsed = time.monotonic() - self._restart_requested_at
        if elapsed >= self.config.timeouts.restart_cooldown:
            return True
        return len(self.db.get_plans_by_status(PlanStatus.QUEUED)) == 0

    def _archive_plan(self, plan_name: str) -> None:
        plan = self.plans.get(plan_name)
        if not plan or not plan.file_path.exists():
            return
        plan.file_path.unlink()
        log.info("Removed completed plan %s", plan.file_path.name)
