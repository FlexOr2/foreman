"""Event-driven agent monitoring using asyncinotify and per-agent timers."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Coroutine

from asyncinotify import Inotify, Mask

log = logging.getLogger(__name__)


class StuckDetector:
    """Tracks log file activity per agent. Fires callback when no activity for threshold."""

    def __init__(
        self,
        threshold_seconds: int,
        on_stuck: Callable[[str], Coroutine],
    ) -> None:
        self.threshold = threshold_seconds
        self._on_stuck = on_stuck
        self._timers: dict[str, asyncio.TimerHandle] = {}

    def on_log_activity(self, plan_name: str) -> None:
        if plan_name in self._timers:
            self._timers[plan_name].cancel()

        loop = asyncio.get_event_loop()
        self._timers[plan_name] = loop.call_later(
            self.threshold, self._fire_stuck, plan_name,
        )

    def _fire_stuck(self, plan_name: str) -> None:
        log.warning("Agent %s appears stuck (no log activity for %ds)", plan_name, self.threshold)
        asyncio.ensure_future(self._on_stuck(plan_name))

    def cancel(self, plan_name: str) -> None:
        timer = self._timers.pop(plan_name, None)
        if timer:
            timer.cancel()

    def cancel_all(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()


async def watch_plans(
    plans_dir: Path,
    on_event: Callable[[Path, Mask], Coroutine],
) -> None:
    """Watch the plans directory for new, renamed, or modified files."""
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
            if not str(event.name).endswith(".md"):
                continue
            log.debug("Plan event: %s on %s", event.mask, file_path)
            await on_event(file_path, event.mask)


async def watch_logs(
    log_dir: Path,
    on_activity: Callable[[str], None],
) -> None:
    """Watch log files for modifications (stuck detection)."""
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
            # Extract plan name from log filename (e.g., "redis-implementation.log" -> "redis")
            plan_name = filename.rsplit("-", 1)[0] if "-" in filename else filename.removesuffix(".log")
            on_activity(plan_name)


async def wait_for_process(pid: int) -> int:
    """Wait for a process to exit. Returns exit code.

    Uses polling with os.waitpid since the PID is not our direct child
    (it's spawned inside tmux). Falls back to checking /proc/{pid}.
    """
    import os

    while True:
        try:
            result_pid, status = os.waitpid(pid, os.WNOHANG)
            if result_pid != 0:
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                return -1
        except ChildProcessError:
            # Not our child — check if process still exists via /proc
            if not Path(f"/proc/{pid}").exists():
                return -1
        await asyncio.sleep(2)
