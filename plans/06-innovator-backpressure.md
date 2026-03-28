# Innovator Backpressure

The innovator generates plans faster than foreman can implement them. With `auto_activate = true`, `max_drafts` only counts draft-prefixed files, but activated plans go straight to QUEUED. The backlog grows indefinitely.

## Fix

The innovator should pause when there are too many unfinished plans, not just too many draft files.

Change `_count_innovator_plans()` (or equivalent) to count ALL non-DONE plans in the DB, not just draft files on disk. If `total_plans - done_plans >= max_drafts`, skip the innovator cycle.

This way the innovator pauses when the backlog is full and resumes when foreman catches up.

## Files to modify

- `foreman/loop.py` or wherever the innovator backpressure check lives — count QUEUED + RUNNING + REVIEWING plans from DB instead of counting files
