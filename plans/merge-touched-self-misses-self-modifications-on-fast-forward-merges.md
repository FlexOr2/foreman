<!-- foreman:innovator -->
# merge_touched_self misses self-modifications on fast-forward merges

> **Depends on:**

## Problem

In `worktree.py:118-122`, `merge_touched_self` checks only the diff between the last commit and its parent:

```python
async def merge_touched_self(repo_root: Path) -> bool:
    rc, stdout, _ = await _run_git("diff", "--name-only", "HEAD~1", "HEAD", cwd=repo_root)
    if rc != 0:
        return False
    return any(line.strip().startswith("foreman/") for line in stdout.splitlines())
```

In `merge_branch` (worktree.py:89-91), `git merge branch` performs a fast-forward when possible. For a branch with N commits that is fast-forwarded, `HEAD~1..HEAD` only examines the last commit on that branch.

If commit 2 of 5 modified `foreman/config.py` but commit 5 (the tip) only touched `app/views.py`, `merge_touched_self` returns False. Foreman doesn't restart, and runs with stale code — potentially missing bug fixes, config changes, or new features applied to itself.

This is called from `_finalize_merge` (loop.py:656) after every successful merge.

## Solution

Record the pre-merge HEAD before `merge_branch`, then diff between pre-merge and post-merge HEAD:

```python
async def merge_branch(branch: str, repo_root: Path) -> tuple[bool, str, str]:
    rc_head, pre_merge_head, _ = await _run_git("rev-parse", "HEAD", cwd=repo_root)
    rc, stdout, stderr = await _run_git("merge", branch, cwd=repo_root)
    pre_head = pre_merge_head.strip() if rc_head == 0 else ""
    return rc == 0, stderr if rc != 0 else stdout, pre_head
```

Then `merge_touched_self` takes the pre-merge ref:

```python
async def merge_touched_self(repo_root: Path, pre_merge_ref: str) -> bool:
    if not pre_merge_ref:
        return False
    rc, stdout, _ = await _run_git("diff", "--name-only", pre_merge_ref, "HEAD", cwd=repo_root)
    if rc != 0:
        return False
    return any(line.strip().startswith("foreman/") for line in stdout.splitlines())
```

Alternatively, use `--no-ff` in `merge_branch` to always create a merge commit, making `HEAD~1..HEAD` capture all changes. But `--no-ff` creates unnecessary merge commits for single-commit branches, and the pre-merge ref approach is cleaner and more accurate.

## Scope

- `foreman/worktree.py` — return pre-merge HEAD from `merge_branch`, update `merge_touched_self` to diff against it
- `foreman/loop.py` — pass pre-merge ref through `_merge_plan` → `_finalize_merge` → `merge_touched_self`

## Risk Assessment

Low-medium risk. Changes the return type of `merge_branch` (adding a third tuple element), which requires updating callers. The core git operations are standard and well-understood. The main risk is that `_brain_resolve_conflict` also needs the pre-merge ref threaded through, but since it calls `_finalize_merge` which calls `merge_touched_self`, the ref just needs to be stored as local state in `_merge_plan`.
