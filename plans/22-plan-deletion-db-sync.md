<!-- foreman:innovator -->
# Plan file deletion is silently ignored — phantom QUEUED records persist in DB

> **Depends on:**

## Problem

The `_plan_watcher` in `loop.py:192-212` handles `CREATE | MOVED_TO` and `MODIFY` events from inotify, but has no handler for `DELETE`. The inotify watch at `monitor.py:167` DOES watch for `Mask.DELETE`, and `is_plan_file` lets `.md` files through, so delete events reach `on_plan_event` — but the function's if/elif chain doesn't match DELETE, so the event is silently dropped.

When a user deletes a plan file while it's QUEUED in the DB (e.g., deciding not to run it), the plan remains QUEUED in the database indefinitely. The dashboard shows it as QUEUED with no way to fix it except `foreman reset`. It also means `_scan_plans` never triggers on the DELETE, so `_plans` still contains the deleted plan until the next unrelated file event triggers a rescan.

The same issue affects `_archive_plan` (merge.py:79): when a completed plan's file is deleted, the DELETE event is ignored. While this is harmless for DONE plans, it means `_scan_plans` isn't called, so the `_plans` dict retains a stale reference to the deleted plan (with `file_path` pointing to a nonexistent file) until the next rescan.

## Solution

Handle DELETE events in `on_plan_event` by triggering a rescan:

```python
async def on_plan_event(file_path: Path, mask: Mask) -> None:
    name = file_path.stem

    if mask & (Mask.CREATE | Mask.MOVED_TO):
        log.info("New plan file: %s", file_path.name)
        await self._scan_plans()
        self.scheduler.schedule_event.set()

    elif mask & Mask.DELETE:
        log.info("Plan file removed: %s", file_path.name)
        await self._scan_plans()

    elif mask & Mask.MODIFY:
        ...
```

The rescan will reload `_plans` without the deleted file. Plans already in the DB as QUEUED that no longer have files will naturally be excluded from `_plans` and the scheduler won't try to spawn them.

To also clean up the DB record, add a post-scan step in `_scan_plans` that marks orphaned QUEUED plans (in DB but not on disk) as FAILED with a clear reason:

```python
for plan_name, plan_data in known_plans.items():
    if plan_data["status"] == PlanStatus.QUEUED and plan_name not in self._plans:
        self.db.set_plan_status(plan_name, PlanStatus.FAILED, reason="Plan file removed")
        log.info("Marked %s as FAILED — plan file no longer exists", plan_name)
```

## Scope

- `foreman/loop.py` — add DELETE handler in `on_plan_event`, add orphan cleanup in `_scan_plans`

## Risk Assessment

Low risk. The DELETE handler just triggers a rescan (same as CREATE). The orphan cleanup only affects QUEUED plans whose files are genuinely missing — it won't touch RUNNING/REVIEWING plans that might still be executing. Using FAILED (not deleting the DB record) preserves the audit trail and allows `foreman unblock` if the deletion was accidental.
