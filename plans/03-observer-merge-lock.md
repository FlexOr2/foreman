<!-- foreman:innovator -->
# Observer merges orphaned plans without merge lock, racing with PlanMerger

> **Depends on:**

## Problem

The main loop's `PlanMerger` serializes all merges through `self._merge_lock` (merge.py:33-37) to prevent concurrent git operations on the main branch. But the observer runs as a separate process (observer.py:104-110, launched via `subprocess.Popen`) and calls `merge_branch` directly (observer.py:118) without any coordination.

This creates two independent problems:

1. **Race condition:** If the observer and main loop merge simultaneously, git's index lock causes one to fail. The observer's failure path only does `abort_merge` + mark BLOCKED — it can't distinguish "real conflict" from "lock contention."

2. **Incomplete merge logic:** Even if the race didn't exist, the observer's merge path is missing `remove_worktree` (the worktree and branch are never cleaned up after merge) and `merge_touched_self` (self-modifications to foreman/ go undetected, so the auto-restart mechanism is bypassed). These are handled by `PlanMerger._finalize_merge` but duplicating them in the observer would create a second divergent merge implementation.

## Solution

Remove direct merging from the observer entirely. For orphaned plans with commits, signal the running main loop to re-process them rather than silently changing DB status and hoping foreman notices.

The main loop's `_plan_watcher` already monitors the plans directory and triggers `_scan_plans` + `schedule_event.set()` on file changes. The observer can exploit this by touching the plan file, which wakes the scheduler. Combined with a status reset, this ensures the main loop picks up the orphaned plan promptly:

```python
async def _handle_orphaned_plan(db: CoordinationDB, plan: dict, config) -> None:
    plan_name = plan["plan"]
    branch = plan.get("branch")

    if branch and await branch_has_commits(branch, config.repo_root):
        db.set_plan_status(plan_name, PlanStatus.RUNNING)
        plan_file = config.plans_dir / f"{plan_name}.md"
        if plan_file.exists():
            plan_file.touch()
        log.info("Reset orphaned plan %s to RUNNING, touched plan file to wake scheduler", plan_name)
    else:
        db.set_plan_status(plan_name, PlanStatus.QUEUED)
        plan_file = config.plans_dir / f"{plan_name}.md"
        if plan_file.exists():
            plan_file.touch()
        log.info("Reset stuck plan %s to QUEUED, touched plan file to wake scheduler", plan_name)
```

The plan file touch triggers an inotify MODIFY event, which `_plan_watcher` (loop.py:193) handles. For RUNNING plans, it notifies the active agent — but since there is no active agent (the window is gone), this is harmless. The scheduler then runs `try_spawn_ready` on the next cycle. For plans with commits, the recovery path at loop.py:174-177 routes them through `on_implementation_done` into the standard review → merge pipeline via PlanMerger.

If foreman is dead (observer already detected this at line 153 and restarted it), the touched plan file will be picked up during `_recover_running_plans` on startup.

This also removes the need to import `merge_branch` and `abort_merge` in observer.py.

## Scope

- `foreman/observer.py` — replace merge logic in `_handle_orphaned_plan` with status transition + plan file touch, remove `merge_branch` and `abort_merge` imports

## Risk Assessment

Low risk. Orphaned plans with commits are routed through the same merge pipeline as normal plans, getting worktree cleanup, self-modification detection, and merge lock serialization for free. The plan file touch is a reliable signaling mechanism because the main loop already uses inotify on the plans directory. The only behavioral change is that orphaned plans go through review (if `auto_review` is enabled) before merging, which is arguably more correct than the observer's fire-and-forget merge.
