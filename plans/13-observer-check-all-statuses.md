<!-- foreman:innovator -->
# Observer kills review agent windows — stale window check only considers RUNNING plans

> **Depends on:**

## Problem

In `observer.py:183-191`, the stale window cleanup builds `active_plans` only from RUNNING status:

```python
active_plans = {p["plan"] for p in db.get_plans_by_status(PlanStatus.RUNNING)}
windows = await _tmux_list_windows()
for window in windows:
    if window == "dashboard":
        continue
    plan_name = window.split(AGENT_TYPE_SEP)[0]
    if plan_name not in active_plans:
        await _tmux_kill_window(window)
```

Plans in REVIEWING status have active review agent windows, but they're not in `active_plans`. The observer kills these windows every 30 seconds, disrupting active reviews. The main loop's watchdog then detects the missing window and treats the plan as orphaned, triggering unwanted state transitions.

## Solution

Use `db.get_active_plan_names()` which already returns plans in both RUNNING and REVIEWING status:

```python
active_plans = db.get_active_plan_names()
```

This is the correct scope for window protection. INTERRUPTED plans should NOT be included — their windows were killed when the plan was paused (the `pause` command kills all agent windows before setting INTERRUPTED status, see `cli.py:369-373` and `web.py:514-519`). If a stale window somehow survives from a crashed pause operation, killing it is the right behavior since the plan is no longer actively executing.

## Scope

- `foreman/observer.py` — replace `get_plans_by_status(PlanStatus.RUNNING)` with `get_active_plan_names()` at line 183

## Risk Assessment

Very low risk. `get_active_plan_names()` returns exactly RUNNING + REVIEWING plans, which matches the set of plans that should have live agent windows. Stale windows from plans that moved past REVIEWING without cleanup will survive until the plan changes status, which is harmless.
