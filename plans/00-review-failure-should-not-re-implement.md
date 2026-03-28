# Review Failure Should Not Re-Implement

## Problem

When a review agent exits with non-zero code (crash, timeout, CLI error), the done watcher treats it like any failed agent and the scheduler re-spawns an implementation agent. This throws away the already-committed implementation and starts over.

The correct behavior: if implementation is done (committed) and review fails, retry the review — don't re-implement.

## Fix

In the done watcher or scheduler, when a REVIEW agent exits non-zero:
1. Check if the implementation was already committed (branch has commits beyond main)
2. If yes: set status back to RUNNING and add to pending_reviews (retry review)
3. If no: mark FAILED (something went wrong before implementation)

Do NOT re-spawn implementation when review fails and code exists on the branch.

## Files to modify

- `foreman/loop.py` or `foreman/scheduler.py` — fix the review failure handler
