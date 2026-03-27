"""Live Rich dashboard for Foreman status."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from foreman.config import Config
from foreman.coordination import AgentType, CoordinationDB, PlanStatus
from foreman.innovate import INNOVATOR_MARKER

REFRESH_INTERVAL = 2
INNOVATOR_LOG_LINES = 10


class RecentLogs(logging.Handler):

    def __init__(self, maxlen: int = INNOVATOR_LOG_LINES):
        super().__init__()
        self._records: deque[logging.LogRecord] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)

    def recent(self) -> list[logging.LogRecord]:
        return list(self._records)


_innovator_logs = RecentLogs()
logging.getLogger("foreman.innovate").addHandler(_innovator_logs)

STATUS_STYLES = {
    PlanStatus.QUEUED: "white",
    PlanStatus.RUNNING: "cyan",
    PlanStatus.REVIEWING: "blue",
    PlanStatus.DONE: "green",
    PlanStatus.BLOCKED: "yellow",
    PlanStatus.FAILED: "red",
    PlanStatus.INTERRUPTED: "magenta",
}


def _time_ago(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    try:
        ts = datetime.fromisoformat(iso_str)
        delta = datetime.now(timezone.utc) - ts
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    except (ValueError, TypeError):
        return ""


def _innovator_status(config: Config) -> str:
    if not config.innovate.enabled:
        return "disabled"
    plan_count = sum(
        1 for f in config.plans_dir.glob("*.md")
        if INNOVATOR_MARKER in f.read_text(encoding="utf-8")[:100]
    )
    if plan_count >= config.innovate.max_drafts:
        return f"paused ({plan_count}/{config.innovate.max_drafts} drafts)"
    return f"running ({plan_count}/{config.innovate.max_drafts} drafts)"


def _build_slots_panel(config: Config, db: CoordinationDB) -> Panel:
    running = len(db.get_plans_by_status(PlanStatus.RUNNING))
    reviewing = len(db.get_plans_by_status(PlanStatus.REVIEWING))

    worker_text = Text()
    worker_text.append(f" Workers: {running}/{config.agents.max_parallel_workers} ", style="cyan")
    worker_text.append("  ")
    worker_text.append(f" Reviews: {reviewing}/{config.agents.max_parallel_reviews} ", style="blue")

    if config.innovate.enabled:
        status = _innovator_status(config)
        style = "yellow" if "paused" in status else "green"
        worker_text.append("  ")
        worker_text.append(f" Innovator: {status} ", style=style)

    return Panel(worker_text, title="Slots", border_style="dim")


def _build_plans_table(db: CoordinationDB) -> Table:
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column("Plan", style="bold", ratio=2)
    table.add_column("Status", ratio=1)
    table.add_column("Agent", ratio=1, style="dim")
    table.add_column("Branch", ratio=2, style="dim")
    table.add_column("Updated", ratio=1)
    table.add_column("Info", ratio=3, style="dim")

    for plan_data in db.get_all_plans():
        status = PlanStatus(plan_data["status"])
        style = STATUS_STYLES.get(status, "white")

        agent_type = db.get_active_agent_type(plan_data["plan"])
        agent_str = agent_type.value if agent_type else ""

        reason = plan_data.get("blocked_reason") or ""

        table.add_row(
            plan_data["plan"],
            Text(status.value, style=style),
            agent_str,
            plan_data.get("branch") or "",
            _time_ago(plan_data.get("updated_at")),
            reason[:60],
        )

    return table


def _build_summary(db: CoordinationDB) -> Text:
    all_plans = db.get_all_plans()
    counts = {}
    for p in all_plans:
        s = p["status"]
        counts[s] = counts.get(s, 0) + 1

    text = Text()
    total = len(all_plans)
    done = counts.get(PlanStatus.DONE, 0)
    text.append(f" {done}/{total} done", style="green" if done == total and total > 0 else "white")

    for status in (PlanStatus.RUNNING, PlanStatus.REVIEWING, PlanStatus.QUEUED,
                   PlanStatus.BLOCKED, PlanStatus.FAILED):
        count = counts.get(status, 0)
        if count:
            style = STATUS_STYLES.get(status, "white")
            text.append(f"  {count} {status.value.lower()}", style=style)

    return text


def _build_innovator_panel() -> Panel | None:
    records = _innovator_logs.recent()
    if not records:
        return None

    lines = Text()
    for i, record in enumerate(records):
        if i > 0:
            lines.append("\n")
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        lines.append(f"  {ts}  ", style="dim")
        lines.append(record.getMessage())

    return Panel(lines, title="Innovator Activity", border_style="dim yellow")


def build_display(config: Config, db: CoordinationDB) -> Group:
    parts: list[Panel | Table | Text] = [
        _build_slots_panel(config, db),
        Panel(_build_plans_table(db), title="Plans", border_style="dim"),
    ]

    innovator_panel = _build_innovator_panel()
    if innovator_panel:
        parts.append(innovator_panel)

    parts.append(_build_summary(db))
    return Group(*parts)


async def run_dashboard(config: Config, db: CoordinationDB, shutdown: asyncio.Event) -> None:
    with Live(build_display(config, db), refresh_per_second=1, screen=False) as live:
        while not shutdown.is_set():
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=REFRESH_INTERVAL)
            except asyncio.TimeoutError:
                pass
            live.update(build_display(config, db))
