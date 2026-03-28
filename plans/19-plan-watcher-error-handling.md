<!-- foreman:innovator -->
# _plan_watcher crashes the entire event loop on unexpected parse errors

> **Depends on:**

## Problem

In `loop.py:192-213`, the `on_plan_event` callback calls `self._scan_plans()` which catches only three specific exception types (line 140: `InvalidPlanNameError, CircularDependencyError, UnresolvedDependencyError`). Any other exception — `PermissionError` from a file the user doesn't own, `UnicodeDecodeError` from a binary file accidentally placed in `plans/`, `OSError` from a full disk — propagates up through `on_plan_event`, crashes `watch_plans`, and brings down the entire `TaskGroup` at line 85.

```python
async def on_plan_event(file_path: Path, mask: Mask) -> None:
    if mask & (Mask.CREATE | Mask.MOVED_TO):
        await self._scan_plans()  # No try/except for unexpected errors
        self.scheduler.schedule_event.set()
    elif mask & Mask.MODIFY:
        ...
        await self._scan_plans()  # Same risk here (line 211)
```

A single corrupted file in the plans directory kills foreman entirely.

## Solution

Wrap the `on_plan_event` callback body in a broad exception handler that logs and continues:

```python
async def on_plan_event(file_path: Path, mask: Mask) -> None:
    try:
        name = file_path.stem
        if mask & (Mask.CREATE | Mask.MOVED_TO):
            ...
        elif mask & Mask.MODIFY:
            ...
    except Exception:
        log.error("Error processing plan event for %s", file_path, exc_info=True)
```

This ensures the plan watcher survives individual file errors. The error is logged so the user can investigate.

## Scope

- `foreman/loop.py` — wrap `on_plan_event` body in try/except

## Risk Assessment

Very low. The catch-all only prevents the watcher from crashing. Errors are logged, not silenced. The only trade-off is that a persistent parse error won't halt foreman — but that's the desired behavior (one bad file shouldn't kill the orchestrator).
