<!-- foreman:innovator -->
# Review verdict comparison is case-sensitive against LLM output

> **Depends on:**

## Problem

In `scheduler.py:178-215`, the review verdict from `REVIEW_VERDICT.json` is compared directly against `ReviewVerdict` enum values:

```python
decision = verdict.get("verdict")

if decision == ReviewVerdict.CLEAN:      # "clean"
    ...
elif decision == ReviewVerdict.FINDINGS:  # "findings"
    ...
elif decision == ReviewVerdict.ARCHITECTURAL:  # "architectural"
    ...
else:
    self.db.set_plan_status(plan_name, PlanStatus.BLOCKED,
        reason=f"Unknown review verdict: {decision}")
```

The review prompt (`prompt-review.md:27`) instructs the agent to write `{"verdict": "clean"}` (lowercase). But the review agent is an LLM that may produce `"Clean"`, `"CLEAN"`, or `"findings "` (with trailing whitespace). Any of these fall through to the `else` branch, blocking the plan with "Unknown review verdict" — a confusing error that doesn't surface the actual problem.

## Solution

Normalize the verdict string before comparison. Use `ReviewVerdict(decision)` with a try/except fallback:

```python
raw_decision = verdict.get("verdict", "")
try:
    decision = ReviewVerdict(raw_decision.strip().lower())
except ValueError:
    self.db.set_plan_status(
        plan_name, PlanStatus.BLOCKED,
        reason=f"Unknown review verdict: {raw_decision!r}",
    )
    self.cascade_failure(plan_name)
    return
```

Then use `decision` (already a ReviewVerdict) for the subsequent if/elif chain.

## Scope

- `foreman/scheduler.py` — normalize verdict string in `on_review_done`

## Risk Assessment

Very low risk. This only adds robustness — valid lowercase verdicts still work identically, and invalid verdicts now produce a quoted error message showing exactly what was received.
