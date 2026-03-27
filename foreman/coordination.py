"""SQLite coordination database for status tracking and PID management."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterator


class PlanStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    REVIEWING = "REVIEWING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class AgentType(StrEnum):
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    FIX = "fix"


class ReviewVerdict(StrEnum):
    CLEAN = "clean"
    FINDINGS = "findings"
    ARCHITECTURAL = "architectural"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    branch TEXT,
    worktree_path TEXT,
    started_at TEXT,
    updated_at TEXT,
    blocked_reason TEXT
);

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan TEXT NOT NULL,
    type TEXT NOT NULL,
    pid INTEGER,
    log_file TEXT,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CoordinationDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        from foreman.config import SQLITE_BUSY_TIMEOUT_MS
        self._conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._in_tx = False

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        if self._in_tx:
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        finally:
            self._in_tx = False

    def upsert_plan(
        self,
        plan: str,
        status: PlanStatus = PlanStatus.QUEUED,
        branch: str | None = None,
        worktree_path: str | None = None,
    ) -> None:
        with self.tx():
            now = _now()
            self._conn.execute(
                """INSERT INTO plans (plan, status, branch, worktree_path, started_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(plan) DO UPDATE SET
                     status=excluded.status,
                     branch=COALESCE(excluded.branch, plans.branch),
                     worktree_path=COALESCE(excluded.worktree_path, plans.worktree_path),
                     updated_at=excluded.updated_at""",
                (plan, status, branch, worktree_path, now, now),
            )

    def set_plan_status(
        self, plan: str, status: PlanStatus, reason: str | None = None
    ) -> None:
        with self.tx():
            self._conn.execute(
                "UPDATE plans SET status=?, blocked_reason=?, updated_at=? WHERE plan=?",
                (status, reason, _now(), plan),
            )

    def get_plan_status(self, plan: str) -> PlanStatus | None:
        row = self._conn.execute(
            "SELECT status FROM plans WHERE plan=?", (plan,)
        ).fetchone()
        return PlanStatus(row["status"]) if row else None

    def get_plan(self, plan: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM plans WHERE plan=?", (plan,)).fetchone()
        return dict(row) if row else None

    def get_plans_by_status(self, status: PlanStatus) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM plans WHERE status=?", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_plans(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM plans ORDER BY plan").fetchall()
        return [dict(r) for r in rows]

    def get_completed_plan_names(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT plan FROM plans WHERE status=?", (PlanStatus.DONE,)
        ).fetchall()
        return {r["plan"] for r in rows}

    def get_running_plan_names(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT plan FROM plans WHERE status NOT IN (?, ?)",
            (PlanStatus.QUEUED, PlanStatus.DONE),
        ).fetchall()
        return {r["plan"] for r in rows}

    def mark_all_running_as_interrupted(self) -> int:
        with self.tx():
            cursor = self._conn.execute(
                "UPDATE plans SET status=?, updated_at=? WHERE status IN (?, ?)",
                (PlanStatus.INTERRUPTED, _now(), PlanStatus.RUNNING, PlanStatus.REVIEWING),
            )
            return cursor.rowcount

    def add_agent(
        self,
        plan: str,
        agent_type: AgentType,
        pid: int | None = None,
        log_file: str | None = None,
    ) -> int:
        with self.tx():
            cursor = self._conn.execute(
                """INSERT INTO agents (plan, type, pid, log_file, started_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (plan, agent_type, pid, log_file, _now()),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def update_agent_pid(self, agent_id: int, pid: int) -> None:
        with self.tx():
            self._conn.execute("UPDATE agents SET pid=? WHERE id=?", (pid, agent_id))

    def finish_agent(self, agent_id: int, exit_code: int) -> None:
        with self.tx():
            self._conn.execute(
                "UPDATE agents SET finished_at=?, exit_code=? WHERE id=?",
                (_now(), exit_code, agent_id),
            )

    def get_active_agents(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM agents WHERE finished_at IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_agents_for_plan(self, plan: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM agents WHERE plan=? ORDER BY started_at", (plan,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_agent_type(self, plan: str) -> AgentType | None:
        row = self._conn.execute(
            "SELECT type FROM agents WHERE plan=? AND finished_at IS NULL ORDER BY started_at DESC LIMIT 1",
            (plan,),
        ).fetchone()
        return AgentType(row["type"]) if row else None

    def reset(self) -> None:
        self._conn.executescript("DELETE FROM agents; DELETE FROM plans;")
