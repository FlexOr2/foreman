"""Agent watchdog — orphan reconciliation, stuck detection, restart draining."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from foreman.config import Config
from foreman.coordination import AgentType, CoordinationDB, PlanStatus, StuckAction
from foreman.monitor import StuckDetector
from foreman.spawner import AGENT_TYPE_SEP, Spawner, read_exit_code

log = logging.getLogger(__name__)

WATCHDOG_INTERVAL = 30
TIMEOUT_GRACE_PERIOD = 60


class AgentWatchdog:
    def __init__(
        self,
        db: CoordinationDB,
        spawner: Spawner,
        stuck: StuckDetector,
        config: Config,
    ) -> None:
        self.db = db
        self.spawner = spawner
        self.stuck = stuck
        self.config = config
        self._stuck_warned: set[str] = set()
        self.on_agent_done: Callable[[str, AgentType], Coroutine[Any, Any, None]] | None = None
        self.on_cascade: Callable[[str], None] | None = None
        self.on_finish_agent: Callable[[str], int | None] | None = None

    def on_log_activity(self, plan_name: str) -> None:
        if plan_name in self._stuck_warned:
            self._stuck_warned.discard(plan_name)
            self.db.set_blocked_reason(plan_name, None)

    async def watchdog_loop(self, shutdown: asyncio.Event, schedule_event: asyncio.Event) -> None:
        self._schedule_event = schedule_event
        while not shutdown.is_set():
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=WATCHDOG_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            await self._reconcile_orphaned_plans(schedule_event)

    async def _reconcile_orphaned_plans(self, schedule_event: asyncio.Event) -> None:
        running = self.db.get_plans_by_status(PlanStatus.RUNNING) + self.db.get_plans_by_status(PlanStatus.REVIEWING)
        reconciled = False

        for plan_data in running:
            plan_name = plan_data["plan"]
            agent_type = self.db.get_active_agent_type(plan_name)
            if not agent_type:
                continue

            if await self.spawner.is_agent_alive(plan_name, agent_type):
                continue

            log.warning("Orphaned plan %s — agent process gone, processing completion", plan_name, extra={"plan": plan_name})
            self.stuck.cancel(plan_name)

            sentinel_name = f"{plan_name}{AGENT_TYPE_SEP}{agent_type.value}"
            sentinel_file = self.config.repo_root / ".foreman" / "done" / sentinel_name

            agent_id = self.on_finish_agent(plan_name) if self.on_finish_agent else None

            if sentinel_file.exists():
                exit_code = read_exit_code(self.config.repo_root / ".foreman" / "done", sentinel_name)
                sentinel_file.unlink(missing_ok=True)
                if agent_id is not None:
                    self.db.finish_agent(agent_id, exit_code)
                if exit_code == 0:
                    if self.on_agent_done:
                        await self.on_agent_done(plan_name, agent_type)
                else:
                    self.db.set_plan_status(plan_name, PlanStatus.FAILED)
                    if self.on_cascade:
                        self.on_cascade(plan_name)
            else:
                if agent_id is not None:
                    self.db.finish_agent(agent_id, exit_code=-1)
                worktree_path = plan_data.get("worktree_path")
                if worktree_path:
                    if self.on_agent_done:
                        await self.on_agent_done(plan_name, agent_type)
                else:
                    self.db.set_plan_status(plan_name, PlanStatus.FAILED)
                    if self.on_cascade:
                        self.on_cascade(plan_name)

            reconciled = True

        if reconciled:
            schedule_event.set()

    async def try_restart(
        self,
        innovator_running: bool,
        request_shutdown: Callable[[], None],
    ) -> None:
        active = self.db.get_active_plan_names()
        if active:
            for plan_name in active:
                agent_type = self.db.get_active_agent_type(plan_name)
                if agent_type and await self.spawner.is_agent_alive(plan_name, agent_type):
                    log.info("Restart pending — waiting for %d active agents to finish", len(active))
                    return

        if innovator_running:
            log.info("Restart pending — waiting for innovator to finish current phase")
            return

        log.info("All agent processes done — restarting to apply self-improvements")
        request_shutdown()

    async def on_agent_stuck(self, plan_name: str) -> None:
        reason = f"Agent appears stuck (no activity for {self.config.timeouts.stuck_threshold}s)"
        self.db.set_blocked_reason(plan_name, reason)
        self._stuck_warned.add(plan_name)
        log.warning("Agent %s is stuck — surfacing in dashboard", plan_name, extra={"plan": plan_name})

        if self.config.agents.stuck_action != StuckAction.KILL:
            return

        log.warning("Killing stuck agent %s", plan_name, extra={"plan": plan_name})
        agent_type = self.db.get_active_agent_type(plan_name)
        if agent_type:
            await self.spawner.kill_agent(plan_name, agent_type)
        self.stuck.cancel(plan_name)
        self._stuck_warned.discard(plan_name)
        self.db.set_plan_status(plan_name, PlanStatus.FAILED, reason=reason)
        if self.on_cascade:
            self.on_cascade(plan_name)

    async def on_agent_timeout(self, plan_name: str) -> None:
        log.warning("Hard timeout fired for %s", plan_name, extra={"plan": plan_name})
        agent_type = self.db.get_active_agent_type(plan_name)
        if agent_type:
            await self.spawner.kill_agent(plan_name, agent_type)
        self.stuck.cancel(plan_name)
        self.db.set_plan_status(plan_name, PlanStatus.FAILED, reason="Agent exceeded hard timeout")
        if self.on_cascade:
            self.on_cascade(plan_name)
        if hasattr(self, '_schedule_event'):
            self._schedule_event.set()
