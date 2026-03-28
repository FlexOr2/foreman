"""Async event loop — orchestrates all subsystems via TaskGroup."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from asyncinotify import Mask

from foreman.brain import ForemanBrain
from foreman.config import RELOAD_CONFIG_MARKER, RESTART_EXIT_CODE, Config, apply_config_update, load_config
from foreman.coordination import AgentType, CoordinationDB, PlanStatus
from foreman.dashboard import run_dashboard
from foreman.innovate import innovate
from foreman.merge import PlanMerger
from foreman.monitor import StuckDetector, watch_done, watch_logs, watch_plans
from foreman.plan_parser import InvalidPlanNameError, Plan, load_plans
from foreman.preflight import check_prerequisites
from foreman.resolver import CircularDependencyError, UnresolvedDependencyError, validate_dag
from foreman.scheduler import AgentScheduler
from foreman.spawner import AGENT_TYPE_SEP, Spawner
from foreman.observer import PID_FILE_FOREMAN, remove_pid, write_pid
from foreman.watchdog import AgentWatchdog
from foreman.worktree import branch_has_commits

log = logging.getLogger(__name__)


class ForemanLoop:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = CoordinationDB(config.coordination_db)
        self.brain = ForemanBrain(
            config.coordination_db.parent,
            allowed_tools=config.allowed_tools.get("brain", "Read,Edit,Bash,Glob,Grep"),
            permission_mode=config.agents.permission_mode,
            timeout=config.timeouts.brain_timeout,
        )
        self.spawner = Spawner(config)

        self.stuck = StuckDetector(
            config.timeouts.stuck_threshold,
            on_stuck=self._on_agent_stuck,
            on_timeout=self._on_agent_timeout,
        )

        self.watchdog = AgentWatchdog(self.db, self.spawner, self.stuck, config)

        self._plans: dict[str, Plan] = {}
        self._innovator_running = False
        self._shutdown = asyncio.Event()

        self.scheduler = AgentScheduler(
            self.db, self.spawner, config, self.stuck,
        )
        self.merger = PlanMerger(self.db, config)

        self.scheduler.on_merge = self.merger.merge_plan
        self.merger.on_failure = self.scheduler.cascade_failure
        self.merger.on_rebase_needed = self.scheduler.spawn_rebase
        self.watchdog.on_cascade = self.scheduler.cascade_failure
        self.watchdog.on_agent_done = self._dispatch_agent_done
        self.watchdog.on_finish_agent = self.scheduler.finish_agent

    async def _on_agent_stuck(self, plan_name: str) -> None:
        await self.watchdog.on_agent_stuck(plan_name)

    async def _on_agent_timeout(self, plan_name: str) -> None:
        await self.watchdog.on_agent_timeout(plan_name)

    async def run(self) -> int:
        check_prerequisites()
        self.config.ensure_dirs()
        await self.spawner.setup()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_shutdown)

        await self._scan_plans()
        await self._process_stale_sentinels()
        await self._recover_running_plans()

        try:
            write_pid(self.config.repo_root, PID_FILE_FOREMAN)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._plan_watcher())
                tg.create_task(self._log_watcher())
                tg.create_task(self._done_watcher())
                tg.create_task(self._scheduler_loop())
                tg.create_task(self.watchdog.watchdog_loop(self._shutdown, self.scheduler.schedule_event))
                tg.create_task(self._innovator_loop())
                tg.create_task(self._config_reload_loop())
                tg.create_task(run_dashboard(self.config, self.db, self._shutdown))
                tg.create_task(self._shutdown_waiter())
        except* KeyboardInterrupt:
            pass
        finally:
            await self._graceful_shutdown()

        return RESTART_EXIT_CODE if self.merger.restart_pending else 0

    def _request_shutdown(self) -> None:
        log.info("Shutdown requested")
        self._shutdown.set()

    async def _shutdown_waiter(self) -> None:
        await self._shutdown.wait()
        raise KeyboardInterrupt

    async def _graceful_shutdown(self) -> None:
        log.info("Shutting down...")
        self.stuck.cancel_all()

        for plan_name, agent_id in self.scheduler.active_agent_ids.items():
            self.db.finish_agent(agent_id, exit_code=-1)
        self.scheduler.active_agent_ids.clear()

        count = self.db.mark_all_running_as_interrupted()
        if count:
            log.info("Marked %d plans as INTERRUPTED", count)

        try:
            await self.brain.summarize_and_reset()
        except Exception:
            log.warning("Brain summarization failed", exc_info=True)
            self.brain.save()

        self.db.close()
        remove_pid(self.config.repo_root, PID_FILE_FOREMAN)
        await self.spawner.teardown()
        log.info("Shutdown complete.")

    # --- Plan scanning ---

    async def _scan_plans(self) -> None:
        try:
            plans = load_plans(self.config.plans_dir)
            validate_dag(plans)
        except (InvalidPlanNameError, CircularDependencyError, UnresolvedDependencyError) as e:
            log.error("%s", e)
            return

        self._plans = {p.name: p for p in plans}
        self.scheduler.plans = self._plans
        self.merger.plans = self._plans

        known_plans = {p["plan"]: p for p in self.db.get_all_plans()}
        for plan in plans:
            existing = known_plans.get(plan.name)
            if existing and existing["status"] == PlanStatus.DONE:
                plan.file_path.unlink(missing_ok=True)
                log.info("Removed already-completed plan file: %s", plan.name)
            elif plan.name not in known_plans:
                self.db.upsert_plan(plan.name, PlanStatus.QUEUED)
                log.info("New plan detected: %s", plan.name)

    async def _recover_running_plans(self) -> None:
        interrupted = (
            self.db.get_plans_by_status(PlanStatus.RUNNING)
            + self.db.get_plans_by_status(PlanStatus.REVIEWING)
            + self.db.get_plans_by_status(PlanStatus.INTERRUPTED)
        )
        if not interrupted:
            return

        log.info("Recovering %d interrupted plans", len(interrupted))

        for plan_data in interrupted:
            plan_name = plan_data["plan"]
            branch = plan_data["branch"]
            agent_type = self.db.get_active_agent_type(plan_name)

            if agent_type and await self.spawner.is_agent_alive(plan_name, agent_type):
                log.info("Plan %s still has live agent process, re-registering", plan_name)
                self.stuck.track(plan_name)
                agents = self.db.get_agents_for_plan(plan_name)
                active = [a for a in agents if a["finished_at"] is None]
                if active:
                    self.scheduler.active_agent_ids[plan_name] = active[-1]["id"]
                continue

            if branch and await branch_has_commits(branch, self.config.repo_root):
                log.info("Plan %s has commits on %s, treating as implementation done", plan_name, branch)
                self.db.set_plan_status(plan_name, PlanStatus.RUNNING)
                await self.scheduler.on_implementation_done(plan_name)
            else:
                log.info("Plan %s has no commits, re-queuing for spawn", plan_name)
                self.db.set_plan_status(plan_name, PlanStatus.QUEUED)

    # --- Event watchers ---

    async def _plan_watcher(self) -> None:
        async def on_plan_event(file_path: Path, mask: Mask) -> None:
            name = file_path.stem

            if mask & (Mask.CREATE | Mask.MOVED_TO):
                log.info("New plan file: %s", file_path.name)
                await self._scan_plans()
                self.scheduler.schedule_event.set()

            elif mask & Mask.MODIFY:
                status = self.db.get_plan_status(name)
                if status not in (PlanStatus.RUNNING, PlanStatus.REVIEWING):
                    await self._scan_plans()

        await watch_plans(self.config.plans_dir, on_plan_event)

    async def _log_watcher(self) -> None:
        def on_activity(plan_name: str) -> None:
            self.stuck.on_log_activity(plan_name)
            self.watchdog.on_log_activity(plan_name)

        await watch_logs(self.config.log_dir, on_activity)

    async def _process_stale_sentinels(self) -> None:
        done_dir = self.config.repo_root / ".foreman" / "done"
        if not done_dir.exists():
            return
        for sentinel in sorted(done_dir.iterdir()):
            if sentinel.name.endswith(".tmp"):
                sentinel.unlink(missing_ok=True)
                continue
            if sentinel.is_file():
                log.info("Processing stale sentinel: %s", sentinel.name)
                await self._handle_agent_done(sentinel.name)

    async def _handle_agent_done(self, sentinel_name: str) -> None:
        if AGENT_TYPE_SEP in sentinel_name:
            plan_name, type_str = sentinel_name.split(AGENT_TYPE_SEP, 1)
            agent_type = AgentType(type_str)
        else:
            plan_name = sentinel_name
            agent_type = AgentType.IMPLEMENTATION

        current_status = self.db.get_plan_status(plan_name)
        if current_status in (PlanStatus.DONE, PlanStatus.FAILED, PlanStatus.BLOCKED):
            log.info("Ignoring sentinel for %s (status already %s)", plan_name, current_status)
            done_file = self.config.repo_root / ".foreman" / "done" / sentinel_name
            done_file.unlink(missing_ok=True)
            return

        exit_code = self._read_exit_code(sentinel_name)
        log.info("Agent %s/%s finished (exit code: %s)", plan_name, agent_type.value, exit_code)

        agent_id = self.scheduler.finish_agent(plan_name)
        if agent_id is not None:
            self.db.finish_agent(agent_id, exit_code)

        self.stuck.cancel(plan_name)

        sentinel_file = self.config.repo_root / ".foreman" / "done" / sentinel_name
        sentinel_file.unlink(missing_ok=True)

        if exit_code != 0:
            if agent_type == AgentType.REVIEW and await self.scheduler.on_review_failure(plan_name):
                log.warning("Review agent failed for %s (exit %s), retrying review", plan_name, exit_code)
                return
            self.db.set_plan_status(plan_name, PlanStatus.FAILED)
            log.error("Agent %s/%s failed (exit code %s)", plan_name, agent_type.value, exit_code)
            self.scheduler.cascade_failure(plan_name)
        else:
            await self._dispatch_agent_done(plan_name, agent_type)

        self.scheduler.schedule_event.set()

    async def _dispatch_agent_done(self, plan_name: str, agent_type: AgentType) -> None:
        if agent_type == AgentType.IMPLEMENTATION:
            await self.scheduler.on_implementation_done(plan_name)
        elif agent_type == AgentType.REVIEW:
            await self.scheduler.on_review_done(plan_name)
        elif agent_type == AgentType.FIX:
            await self.scheduler.on_fix_done(plan_name)
        elif agent_type == AgentType.REBASE:
            await self.scheduler.on_rebase_done(plan_name)

    async def _done_watcher(self) -> None:
        done_dir = self.config.repo_root / ".foreman" / "done"
        await watch_done(done_dir, self._handle_agent_done)

    def _read_exit_code(self, sentinel_name: str) -> int:
        done_file = self.config.repo_root / ".foreman" / "done" / sentinel_name
        try:
            return int(done_file.read_text().strip())
        except (ValueError, FileNotFoundError):
            log.warning("Sentinel file missing or unreadable for %s, treating as crash", sentinel_name)
            return 1

    # --- Scheduler ---

    async def _scheduler_loop(self) -> None:
        while not self._shutdown.is_set():
            self.scheduler.schedule_event.clear()
            if self.merger.restart_pending:
                await self.watchdog.try_restart(
                    self._innovator_running,
                    self._request_shutdown,
                )
            else:
                await self.scheduler.try_spawn_ready()
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(self.scheduler.schedule_event.wait()),
                    asyncio.create_task(self._shutdown.wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    # --- Background innovation ---

    def _count_innovator_plans(self) -> int:
        return self.db.count_pending_plans()

    async def _innovator_loop(self) -> None:
        if not self.config.innovate.enabled:
            return

        while not self._shutdown.is_set():
            if self.merger.restart_pending:
                log.info("Innovator pausing for restart")
                return

            if self._count_innovator_plans() < self.config.innovate.max_drafts:
                self._innovator_running = True
                try:
                    await innovate(
                        self.config,
                        skip_review=self.config.innovate.skip_review,
                        should_stop=lambda: self.merger.restart_pending,
                    )
                finally:
                    self._innovator_running = False
                    if self.merger.restart_pending:
                        self.scheduler.schedule_event.set()

            await self._wait_for_interval(self.config.innovate.interval)

    async def _config_reload_loop(self) -> None:
        marker = self.config.repo_root / RELOAD_CONFIG_MARKER
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=5)
                return
            except asyncio.TimeoutError:
                pass
            if marker.exists():
                try:
                    marker.unlink()
                    updated = load_config(self.config.repo_root)
                    apply_config_update(self.config, updated)
                    log.info("Config reloaded from disk")
                    self.scheduler.schedule_event.set()
                except Exception:
                    log.warning("Failed to reload config", exc_info=True)

    async def _wait_for_interval(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
