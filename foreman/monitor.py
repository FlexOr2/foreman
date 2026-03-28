"""Event-driven agent monitoring using asyncinotify and per-agent timers."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Coroutine

from asyncinotify import Inotify, Mask

from foreman.plan_parser import is_plan_file
from foreman.spawner import AGENT_TYPE_SEP

log = logging.getLogger(__name__)

DONE_DIR_NAME = "done"


class StuckDetector:
    def __init__(
        self,
        threshold_seconds: int,
        on_stuck: Callable[[str], Coroutine],
        on_timeout: Callable[[str], Coroutine] | None = None,
    ) -> None:
        self.threshold = threshold_seconds
        self._on_stuck = on_stuck
        self._on_timeout = on_timeout
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._timeout_timers: dict[str, asyncio.TimerHandle] = {}
        self._active_plans: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def track(self, plan_name: str) -> None:
        self._active_plans.add(plan_name)
        loop = self._get_loop()
        self._timers[plan_name] = loop.call_later(
            self.threshold, self._fire_stuck, plan_name,
        )

    def on_log_activity(self, plan_name: str) -> None:
        if plan_name not in self._active_plans:
            return

        if plan_name in self._timers:
            self._timers[plan_name].cancel()

        loop = self._get_loop()
        self._timers[plan_name] = loop.call_later(
            self.threshold, self._fire_stuck, plan_name,
        )

    def _fire_stuck(self, plan_name: str) -> None:
        if plan_name not in self._active_plans:
            return
        log.warning("Agent %s appears stuck (no log activity for %ds)", plan_name, self.threshold, extra={"plan": plan_name})
        self._get_loop().create_task(self._on_stuck(plan_name))

    def track_timeout(self, plan_name: str, timeout_seconds: int) -> None:
        if plan_name in self._timeout_timers:
            self._timeout_timers[plan_name].cancel()
        loop = self._get_loop()
        self._timeout_timers[plan_name] = loop.call_later(
            timeout_seconds, self._fire_timeout, plan_name,
        )

    def _fire_timeout(self, plan_name: str) -> None:
        if plan_name not in self._active_plans:
            return
        if not self._on_timeout:
            return
        log.warning("Agent %s hit hard timeout", plan_name, extra={"plan": plan_name})
        self._get_loop().create_task(self._on_timeout(plan_name))

    def cancel(self, plan_name: str) -> None:
        self._active_plans.discard(plan_name)
        timer = self._timers.pop(plan_name, None)
        if timer:
            timer.cancel()
        timeout_timer = self._timeout_timers.pop(plan_name, None)
        if timeout_timer:
            timeout_timer.cancel()

    def cancel_all(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        for timer in self._timeout_timers.values():
            timer.cancel()
        self._timeout_timers.clear()
        self._active_plans.clear()


async def watch_plans(
    plans_dir: Path,
    on_event: Callable[[Path, Mask], Coroutine],
) -> None:
    plans_dir.mkdir(parents=True, exist_ok=True)

    with Inotify() as inotify:
        inotify.add_watch(
            plans_dir,
            Mask.CREATE | Mask.MOVED_TO | Mask.MODIFY | Mask.DELETE,
        )
        log.info("Watching plans directory: %s", plans_dir)

        async for event in inotify:
            if event.name is None:
                continue
            file_path = plans_dir / str(event.name)
            if not is_plan_file(file_path):
                continue
            log.debug("Plan event: %s on %s", event.mask, file_path)
            await on_event(file_path, event.mask)


async def watch_logs(
    log_dir: Path,
    on_activity: Callable[[str], None],
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)

    with Inotify() as inotify:
        inotify.add_watch(log_dir, Mask.MODIFY)
        log.info("Watching log directory for activity: %s", log_dir)

        async for event in inotify:
            if event.name is None:
                continue
            filename = str(event.name)
            if not filename.endswith(".log"):
                continue
            plan_name = filename.split(AGENT_TYPE_SEP, 1)[0] if AGENT_TYPE_SEP in filename else filename.removesuffix(".log")
            on_activity(plan_name)


async def watch_done(
    done_dir: Path,
    on_done: Callable[[str], Coroutine],
) -> None:
    done_dir.mkdir(parents=True, exist_ok=True)

    with Inotify() as inotify:
        inotify.add_watch(done_dir, Mask.MOVED_TO)
        log.info("Watching done directory: %s", done_dir)

        async for event in inotify:
            if event.name is None:
                continue
            plan_name = str(event.name)
            log.info("Agent done sentinel: %s", plan_name)
            await on_done(plan_name)
