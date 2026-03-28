<!-- foreman:innovator -->
Confirmed: `on_plan_event` handles `CREATE | MOVED_TO` and `MODIFY` but not `DELETE`. When a plan file is deleted (either by user or by `_archive_plan`), the event is received by `watch_plans` (which watches for DELETE at monitor.py:167), passed to `on_plan_event`, but silently ignored since it doesn't match either condition.

Now I have my genuinely new findings. Let me write the plans.

# validate_dag rejects plans whose dependencies were already completed and archived

> **Depends on:**

## Problem

When a plan completes, `_archive_plan` (merge.py:75-80) deletes its `.md` file. If another plan depends on the completed one, `validate_dag` (resolver.py:27-58) raises `UnresolvedDependencyError` because it only checks dependencies against currently loaded plan files — not against plans that are DONE in the DB.

This manifests on foreman restart. Consider: plan B completes and is archived (file deleted). Plan A (with `depends_on: B`) hasn't started yet. On restart, `_scan_plans` (loop.py:136-142) calls `load_plans` which loads A.md but not B.md (deleted). `validate_dag` sees A depends on "B" which isn't in the loaded set, raises `UnresolvedDependencyError`. The error is caught and logged, but `_plans` stays empty — the entire plan scanning fails. No plans are loaded. QUEUED plans can never be discovered or spawned.

The same issue triggers during normal operation: when `_archive_plan` deletes B.md, `watch_plans` fires a CREATE event (from the next scan) or the next plan event triggers `_scan_plans`, which fails the same way.

## Solution

Pass the set of completed plan names to `validate_dag` so it can treat DONE plans as valid (but absent) dependencies. In `resolver.py`, add an optional `known_completed` parameter:

```python
def validate_dag(plans: list[Plan], known_completed: set[str] | None = None) -> None:
    plan_names = {p.name for p in plans}
    all_known = plan_names | (known_completed or set())
    
    unresolved = {
        p.name: [d for d in p.depends_on if d not in all_known]
        for p in plans
        if any(d not in all_known for d in p.depends_on)
    }
```

In `loop.py:_scan_plans`, pass completed plan names:

```python
completed = self.db.get_completed_plan_names()
validate_dag(plans, known_completed=completed)
```

Also update `compute_waves` (resolver.py:74) to pass completed names through.

## Scope

- `foreman/resolver.py` — add `known_completed` parameter to `validate_dag`
- `foreman/loop.py` — pass completed plan names from DB to `validate_dag`
- `tests/test_validation.py` — add test: plan with dependency on a name in `known_completed` should not raise

## Risk Assessment

Low risk. The change is additive — `known_completed` defaults to `None` (empty set), preserving existing behavior for callers that don't pass it. The only behavioral change is that `_scan_plans` no longer fails when archived plan files are missing. Circular dependency detection is unaffected since DONE plans can't form cycles (they're not in the active plan set).
