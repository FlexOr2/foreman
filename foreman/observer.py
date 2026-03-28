"""External observer process — restarts foreman, fixes orphaned plans, cleans stale windows."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from foreman.config import FOREMAN_DIR, load_config
from foreman.coordination import AgentType, CoordinationDB, PlanStatus
from foreman.spawner import AGENT_TYPE_SEP, TMUX_SESSION
from foreman.worktree import branch_has_commits

log = logging.getLogger(__name__)

OBSERVER_CHECK_INTERVAL = 30
ORPHAN_AGE_MINUTES = 20
RESTART_BACKOFF_BASE = 30
RESTART_BACKOFF_MAX = 600
RESTART_MAX_FAST_FAILURES = 5
RESTART_FAST_WINDOW = 180

PID_FILE_OBSERVER = "observer.pid"
PID_FILE_FOREMAN = "foreman.pid"


def _pid_path(repo_root: Path, name: str) -> Path:
    return repo_root / FOREMAN_DIR / name


def write_pid(repo_root: Path, name: str) -> None:
    path = _pid_path(repo_root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def read_pid(repo_root: Path, name: str) -> int | None:
    path = _pid_path(repo_root, name)
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_process_running(repo_root: Path, name: str) -> bool:
    pid = read_pid(repo_root, name)
    return pid is not None and is_pid_alive(pid)


def remove_pid(repo_root: Path, name: str) -> None:
    _pid_path(repo_root, name).unlink(missing_ok=True)


async def _tmux_list_windows() -> list[str]:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    return [w.strip() for w in stdout.decode().splitlines() if w.strip()]


async def _tmux_has_window(name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "has-window", "-t", f"{TMUX_SESSION}:{name}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def _tmux_kill_window(name: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "kill-window", "-t", f"{TMUX_SESSION}:{name}",
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


def _minutes_since(iso_timestamp: str | None) -> float:
    if not iso_timestamp:
        return float("inf")
    from datetime import datetime, timezone
    try:
        updated = datetime.fromisoformat(iso_timestamp)
        return (datetime.now(timezone.utc) - updated).total_seconds() / 60
    except ValueError:
        return float("inf")


def _start_foreman(repo_root: Path) -> subprocess.Popen:
    devnull = open(os.devnull, "w")
    return subprocess.Popen(
        [sys.executable, "-c", "from foreman.cli import app; app()", "start"],
        cwd=repo_root,
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=devnull,
        stderr=devnull,
    )


async def _handle_orphaned_plan(db: CoordinationDB, plan: dict, config) -> None:
    plan_name = plan["plan"]
    branch = plan.get("branch")
    plan_file = config.plans_dir / f"{plan_name}.md"

    if branch and await branch_has_commits(branch, config.repo_root):
        db.set_plan_status(plan_name, PlanStatus.RUNNING)
        if plan_file.exists():
            plan_file.touch()
        log.info("Reset orphaned plan %s to RUNNING, touched plan file to wake scheduler", plan_name)
    else:
        db.set_plan_status(plan_name, PlanStatus.QUEUED)
        if plan_file.exists():
            plan_file.touch()
        log.info("Reset stuck plan %s to QUEUED, touched plan file to wake scheduler", plan_name)


async def observe_loop(repo_root: Path) -> None:
    shutdown = asyncio.Event()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    write_pid(repo_root, PID_FILE_OBSERVER)
    log.info("Observer started (PID %d)", os.getpid())

    recent_restarts: list[float] = []
    last_started: subprocess.Popen | None = None
    check_interval = float(OBSERVER_CHECK_INTERVAL)

    try:
        while not shutdown.is_set():
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=check_interval)
                break
            except asyncio.TimeoutError:
                pass

            if last_started is not None and last_started.poll() is not None:
                log.warning("Foreman crashed immediately after restart (exit code %d)", last_started.returncode)
                last_started = None

            config = load_config(repo_root)

            if not is_process_running(repo_root, PID_FILE_FOREMAN):
                now = time.monotonic()
                recent_restarts = [t for t in recent_restarts if now - t < RESTART_FAST_WINDOW]
                failure_count = len(recent_restarts)

                if failure_count >= RESTART_MAX_FAST_FAILURES:
                    exponent = failure_count - RESTART_MAX_FAST_FAILURES + 1
                    backoff = min(RESTART_BACKOFF_BASE * (2 ** exponent), RESTART_BACKOFF_MAX)
                    log.critical(
                        "Foreman has failed %d times in %ds — restarting now, next check in %ds",
                        failure_count, RESTART_FAST_WINDOW, backoff,
                    )
                    check_interval = float(backoff)
                else:
                    log.warning("Foreman is not running — restarting")
                    check_interval = float(OBSERVER_CHECK_INTERVAL)

                recent_restarts.append(now)
                last_started = _start_foreman(repo_root)
                continue
            else:
                now = time.monotonic()
                recent_restarts = [t for t in recent_restarts if now - t < RESTART_FAST_WINDOW]
                if not recent_restarts:
                    check_interval = float(OBSERVER_CHECK_INTERVAL)
                last_started = None

            if not config.coordination_db.exists():
                continue

            db = CoordinationDB(config.coordination_db)
            try:
                stuck_plans = (
                    db.get_plans_by_status(PlanStatus.RUNNING)
                    + db.get_plans_by_status(PlanStatus.REVIEWING)
                )
                for plan in stuck_plans:
                    age = _minutes_since(plan.get("updated_at"))
                    if age <= ORPHAN_AGE_MINUTES:
                        continue

                    plan_name = plan["plan"]
                    has_live_window = False
                    for agent_type in AgentType:
                        terminal = f"{plan_name}{AGENT_TYPE_SEP}{agent_type.value}"
                        if await _tmux_has_window(terminal):
                            has_live_window = True
                            break

                    if not has_live_window:
                        await _handle_orphaned_plan(db, plan, config)

                active_plans = {p["plan"] for p in db.get_plans_by_status(PlanStatus.RUNNING)}
                windows = await _tmux_list_windows()
                for window in windows:
                    if window == "dashboard":
                        continue
                    plan_name = window.split(AGENT_TYPE_SEP)[0]
                    if plan_name not in active_plans:
                        await _tmux_kill_window(window)
                        log.info("Killed stale window %s", window)
            finally:
                db.close()
    finally:
        remove_pid(repo_root, PID_FILE_OBSERVER)
        log.info("Observer stopped")


def run(repo_root: Path) -> None:
    asyncio.run(observe_loop(repo_root))


if __name__ == "__main__":
    from foreman.cli import _setup_logging
    from foreman.config import load_config

    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    config = load_config(repo.resolve())
    _setup_logging(log_file=config.log_file)
    run(repo.resolve())
