<!-- foreman:innovator -->
# Web/CLI pause leaves stuck detector armed — paused plan transitions to FAILED

> **Depends on:**

## Problem

When a plan is paused via the web dashboard (`web.py:508-520`) or CLI (`cli.py:376-395`), the agent is killed and plan status is set to INTERRUPTED. Both operate as separate processes from the main foreman loop, so neither cancels the main loop's `StuckDetector` timer for that plan.

The stuck timer (armed in `monitor.py:41-47` via `call_later`) continues ticking. After `stuck_threshold` seconds (default 300s) of no log activity, `_fire_stuck` at `monitor.py:61-66` fires. The plan is still in `_active_plans`, so `on_agent_stuck` in `watchdog.py:147-171` executes.

If `stuck_action` is `KILL`: the watchdog calls `set_plan_status(plan_name, PlanStatus.FAILED)` at line 169, overwriting the user's intentional INTERRUPTED status. `cascade_failure` is also invoked, blocking dependent plans.

If `stuck_action` is `WARN`: a misleading "Agent appears stuck" blocked_reason is written to the DB for a plan the user deliberately paused.

The same issue affects hard timeout timers (`track_timeout`).

## Solution

Have the watchdog detect externally-paused plans during its reconciliation cycle. In `_reconcile_orphaned_plans` (or a new method called from `watchdog_loop`), check for INTERRUPTED plans that are still tracked in the stuck detector and cancel their timers:

```python
interrupted = self.db.get_plans_by_status(PlanStatus.INTERRUPTED)
for plan_data in interrupted:
    plan_name = plan_data["plan"]
    if plan_name in self.stuck._active_plans:
        self.stuck.cancel(plan_name)
        self.completion.cancel(plan_name)
        log.info("Cancelled monitoring for externally paused plan %s", plan_name)
```

This runs every 30 seconds (the watchdog interval), which is well within the 300s stuck threshold. The timers are cancelled before they can fire.

## Scope

- `foreman/watchdog.py` — add INTERRUPTED plan scan to `watchdog_loop` or `_reconcile_orphaned_plans`

## Risk Assessment

Low risk. Only cancels timers for plans already in INTERRUPTED status. The 30-second latency is acceptable since stuck_threshold defaults to 300s. The additional DB query (one per watchdog cycle) is negligible.
