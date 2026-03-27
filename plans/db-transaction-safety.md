# Database Transaction Safety

Multiple `CoordinationDB` mutations execute outside the `_tx()` transaction wrapper, creating race windows where concurrent async tasks can observe or overwrite inconsistent state.

## Problem

The following methods execute bare SQL in autocommit mode (`isolation_level=None`):
- `upsert_plan()` — INSERT/UPDATE without transaction
- `set_plan_status()` — UPDATE without transaction
- `add_agent()` — INSERT + `lastrowid` read without transaction
- `update_agent_pid()` — UPDATE without transaction
- `finish_agent()` — UPDATE without transaction

Only `mark_all_running_as_interrupted()` uses the `_tx()` wrapper.

### Race Condition: Spawn vs. Done

1. `_spawn_implementation()` calls `upsert_plan(RUNNING)`, then `add_agent()`
2. Between these two calls, `_done_watcher` fires for a different plan and calls `set_plan_status()`
3. Both execute concurrently — SQLite serializes at the statement level, but the spawn sequence is not atomic
4. If spawn fails after `upsert_plan(RUNNING)` but before `add_agent()`, the plan is stuck RUNNING with no agent record

### Race Condition: Status Transitions

Multiple callbacks (`_on_implementation_done`, `_on_review_done`, `_on_fix_done`) call `set_plan_status()`. Under fast agent completion, two transitions can interleave, and the last writer wins regardless of logical ordering.

### Missing: Atomic Spawn Recording

`_spawn_implementation()` in `loop.py` performs 3 sequential DB writes (`upsert_plan`, `add_agent`, `stuck.track`) — if any middle step fails, the DB is left inconsistent. The worktree is created but never cleaned up.

## Fix

### Phase 1: Wrap all mutations in `_tx()`

Every method that writes to the DB should use the existing `_tx()` context manager:
