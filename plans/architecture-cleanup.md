# Architecture Cleanup

The codebase has grown from ~1400 lines to ~2800+ lines through 30+ agent-implemented plans. Each agent optimized locally without considering the overall architecture. The result: leaked responsibilities, god files, duplicated logic, dead code.

## What to do

Read every file in `foreman/`. Read `CLAUDE.md`. Then refactor with these goals:

### 1. Split loop.py

`loop.py` is a god file. It contains scheduling, spawning, merging, watchdog, innovator loop, restart logic, brain conflict resolution, plan archival, and event handling. Extract into focused modules:

- `loop.py` — just the event loop, TaskGroup, signal handlers, shutdown. Orchestrator only.
- `scheduler.py` — `_try_spawn_ready`, worker/review slot management, `_pending_reviews`
- `merge.py` — `_merge_plan`, `_finalize_merge`, `_brain_resolve_conflict`, merge serialization
- `watchdog.py` — `_watchdog_loop`, `_reconcile_orphaned_plans`, `_try_restart`

Each module should be a class or set of functions that `loop.py` composes.

### 2. Remove dead code

- Check if old analyzer code paths still exist
- Check for unused imports, unreachable branches
- Check if `_SKIP_PREFIXES` still includes `prompt-` (prompts moved to `.foreman/prompts/`)
- Remove any backwards-compatibility code

### 3. Consolidate overlapping features

- `unblock` and `retry` commands — are both needed? If not, merge them.
- Check for duplicated status checks, DB queries that could be shared

### 4. Verify CLAUDE.md compliance

- Every function should do one thing
- No module should have leaked responsibilities
- No hardcoded values
- Enums over magic strings everywhere

### 5. Update ARCHITECTURE.md

Rewrite to match the refactored codebase. No stale descriptions.

## Constraints

- Do NOT change behavior. This is a pure refactor — all existing functionality must work the same.
- Run `python3 -m py_compile` on every changed file.
- Commit with a clear message explaining what was restructured.
