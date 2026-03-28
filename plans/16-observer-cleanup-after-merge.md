<!-- foreman:innovator -->
# Observer _handle_orphaned_plan merges but never removes worktree or branch

> **Depends on:** observer-merges-orphaned-plans-without-merge-lock-racing-with-planmerger

## Problem

In `observer.py:117-123`, when the observer successfully merges an orphaned plan's branch, it sets the plan to DONE and deletes the plan file, but does NOT call `remove_worktree`:

```python
if branch and await branch_has_commits(branch, config.repo_root):
    success, _, _ = await merge_branch(branch, config.repo_root)
    if success:
        db.set_plan_status(plan_name, PlanStatus.DONE)
        plan_file = config.plans_dir / f"{plan_name}.md"
        plan_file.unlink(missing_ok=True)
        # Missing: remove_worktree(plan_name, config)
```

Compare with the main loop's `_finalize_merge` (merge.py:65-69) which correctly calls `await remove_worktree(plan_name, self.config)` after setting DONE.

Every observer-merged plan leaves behind a git worktree directory in `.foreman/worktrees/{plan_name}/` and a feature branch `feat/{plan_name}`. Over time, these accumulate, consuming disk space and cluttering `git worktree list` / `git branch` output. Git worktrees also hold locks on the repository that can interfere with certain operations.

## Solution

Add `remove_worktree` call after successful observer merge:

```python
from foreman.worktree import abort_merge, branch_has_commits, merge_branch, remove_worktree

async def _handle_orphaned_plan(db: CoordinationDB, plan: dict, config) -> None:
    plan_name = plan["plan"]
    branch = plan.get("branch")

    if branch and await branch_has_commits(branch, config.repo_root):
        success, _, _ = await merge_branch(branch, config.repo_root)
        if success:
            db.set_plan_status(plan_name, PlanStatus.DONE)
            await remove_worktree(plan_name, config)
            plan_file = config.plans_dir / f"{plan_name}.md"
            plan_file.unlink(missing_ok=True)
            log.info("Merged orphaned plan %s", plan_name)
```

## Scope

- `foreman/observer.py` — add `remove_worktree` import and call in `_handle_orphaned_plan`

## Risk Assessment

Very low risk. `remove_worktree` (worktree.py:60-77) already handles missing worktrees gracefully (logs warnings but doesn't raise). Adding it after merge aligns the observer's cleanup behavior with the main loop's `_finalize_merge`.
