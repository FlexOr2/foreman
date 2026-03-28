# Smart Resume from Web UI

When a plan fails (review timeout, max retries, merge conflict), the user should be able to resume it from exactly where it stopped — with different settings if needed.

## What the user wants

From the web UI, on a FAILED or BLOCKED plan:
- "Resume" button that shows options:
  - **Resume review** — skip re-implementation, just re-run the review (code is already committed)
  - **Resume with opus** — re-run the failed step with opus instead of sonnet (for complex reviews)
  - **Resume from scratch** — clean worktree, re-implement
  - **Force merge** — skip review, merge the branch directly (user trusts the implementation)
- Each option shows what will happen before confirming

## Implementation

### 1. Web UI resume panel

On the plan detail view (or as a dropdown on the plan row), show resume options based on current state:
- FAILED with implementation done → offer "resume review", "resume with opus", "force merge"
- FAILED with no implementation → offer "resume from scratch"
- BLOCKED (max retries) → offer "resume review with opus", "force merge"
- BLOCKED (merge conflict) → offer "resume from scratch" (new worktree from updated main)

### 2. Per-plan model override

Add an endpoint that accepts plan name + model override. The scheduler respects per-plan model when spawning the next agent. Store in `plan_overrides` in config or as a column in the DB.

### 3. Force merge endpoint

New endpoint: POST /plans/{name}/force-merge
- Merges the branch directly, skipping review
- Marks DONE, cleans up
- Logs that it was force-merged (for audit)

### 4. Resume review endpoint

New endpoint: POST /plans/{name}/resume-review
- Sets plan status back to RUNNING (triggers review on next cycle)
- Optionally accepts model parameter
- The scheduler sees RUNNING with implementation done → spawns review

## Files to modify

- `foreman/web.py` — add resume panel, force-merge endpoint, resume-review endpoint
- `foreman/scheduler.py` — support per-plan model override when spawning
- `foreman/coordination.py` — optionally add model_override column or use plan_overrides
