"""Agent watchdog — orphan reconciliation, stuck detection, restart draining."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from foreman.config import Config
from foreman.coordination import AgentType, CoordinationDB, PlanStatus, StuckAction
from foreman.monitor import CompletionDetector, StuckDetector, TOOL_RUNNING_MARKER
from foreman.spawner import AGENT_TYPE_SEP, Spawner

log = logging.getLogger(__name__)

WATCHDOG_INTERVAL = 30
TIMEOUT_GRACE_PERIOD = 60


class AgentWatchdog:
    def __init__(
        self,
        db: CoordinationDB,
        spawner: Spawner,
        stuck: StuckDetector,
        completion: CompletionDetector,
        config: Config,
    ) -> None:
        self.db = db
        self.spawner = spawner
        self.stuck = stuck
        self.completion = completion
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

            terminal = self.spawner.terminal_name(plan_name, agent_type)
            if await self.spawner.has_window(terminal):
                continue

            log.warning("Orphaned plan %s — agent window gone, processing completion", plan_name)
            self.stuck.cancel(plan_name)
            self.completion.cancel(plan_name)

            sentinel_name = f"{plan_name}{AGENT_TYPE_SEP}{agent_type.value}"
            sentinel_file = self.config.repo_root / ".foreman" / "done" / sentinel_name

            agent_id = self.on_finish_agent(plan_name) if self.on_finish_agent else None

            if sentinel_file.exists():
                exit_code = self._read_exit_code(sentinel_name)
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

    def _read_exit_code(self, sentinel_name: str) -> int:
        done_file = self.config.repo_root / ".foreman" / "done" / sentinel_name
        try:
            return int(done_file.read_text().strip())
        except (ValueError, FileNotFoundError):
            log.warning("Sentinel file missing or unreadable for %s, treating as crash", sentinel_name)
            return 1

    async def try_restart(
        self,
        pending_reviews: set[str],
        innovator_running: bool,
        request_shutdown: Callable[[], None],
    ) -> None:
        active = self.db.get_active_plan_names()
        if active:
            truly_active = False
            for plan_name in active:
                agent_type = self.db.get_active_agent_type(plan_name)
                if agent_type:
                    terminal = self.spawner.terminal_name(plan_name, agent_type)
                    if await self.spawner.has_window(terminal):
                        truly_active = True
                        break
            if truly_active:
                log.info("Restart pending — waiting for %d active agents to finish", len(active))
                return
            log.info("All agent windows gone — proceeding with restart")

        if pending_reviews:
            log.info("Restart pending — waiting for %d pending reviews", len(pending_reviews))
            return

        if innovator_running:
            log.info("Restart pending — waiting for innovator to finish current phase")
            return

        log.info("All agents finished — restarting to apply self-improvements")
        request_shutdown()

    async def on_agent_stuck(self, plan_name: str, terminal: str | None) -> None:
        reason = f"Agent appears stuck (no activity for {self.config.timeouts.stuck_threshold}s)"
        self.db.set_blocked_reason(plan_name, reason)
        self._stuck_warned.add(plan_name)
        log.warning("Agent %s is stuck — surfacing in dashboard", plan_name)

        if self.config.agents.stuck_action != StuckAction.KILL or not terminal:
            return

        content = await self.spawner.capture_output(terminal)
        if content and TOOL_RUNNING_MARKER in content.lower():
            log.info("Agent %s is mid-tool-execution, skipping kill — re-arming timer", plan_name)
            self.stuck.on_log_activity(plan_name)
            return

        log.warning("Killing stuck agent %s", plan_name)
        agent_type = self.db.get_active_agent_type(plan_name)
        if agent_type:
            await self.spawner.kill_agent(plan_name, agent_type)
        self.stuck.cancel(plan_name)
        self.completion.cancel(plan_name)
        self._stuck_warned.discard(plan_name)
        self.db.set_plan_status(plan_name, PlanStatus.FAILED, reason=reason)
        if self.on_cascade:
            self.on_cascade(plan_name)

    async def on_agent_timeout(self, plan_name: str, terminal: str | None) -> None:
        log.warning("Hard timeout fired for %s", plan_name)

        if terminal:
            content = await self.spawner.capture_output(terminal)
            if content and TOOL_RUNNING_MARKER in content.lower():
                log.info("Agent %s is mid-tool-execution, granting %ds grace period", plan_name, TIMEOUT_GRACE_PERIOD)
                self.stuck.track_timeout(plan_name, terminal, TIMEOUT_GRACE_PERIOD)
                return

        agent_type = self.db.get_active_agent_type(plan_name)
        if agent_type:
            await self.spawner.kill_agent(plan_name, agent_type)
        self.stuck.cancel(plan_name)
        self.completion.cancel(plan_name)
        self.db.set_plan_status(plan_name, PlanStatus.FAILED, reason="Agent exceeded hard timeout")
        if self.on_cascade:
            self.on_cascade(plan_name)
