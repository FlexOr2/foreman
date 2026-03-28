# Rate Limit Detection and Backoff

## Problem

When Claude Max rate limits hit, agents spawn but crash immediately — the tmux window disappears within seconds. The watchdog detects the orphan and re-spawns, creating a rapid spawn-crash loop that wastes resources and blocks all progress.

Nobody detects this pattern. The observer just restarts foreman if it's dead, but foreman IS running — it's just spinning.

## Solution

### 1. Observer detects rapid failure pattern

The observer should track spawn failures over a time window. If N plans fail within M minutes, it's a rate limit:

- Read the structured log file (`.foreman/foreman.log`)
- Count "Failed to spawn" entries in the last 5 minutes
- If >= 3 failures in 5 minutes: rate limit detected

### 2. Observer takes action

When rate limit detected:
- Write a backoff marker file: `.foreman/backoff_until` containing a timestamp (now + backoff_duration)
- Log the event
- Optionally: reduce max_parallel_workers in config.toml temporarily

### 3. Foreman respects backoff

In the scheduler, before spawning:
- Check if `.foreman/backoff_until` exists
- If the timestamp hasn't passed, skip spawning this cycle
- If it has passed, delete the file and resume normal operation

### 4. Exponential backoff

First backoff: 5 minutes. Second consecutive: 10 minutes. Third: 20 minutes. Reset after a successful spawn.

### 5. Model fallback

If backoff has been triggered N times, the observer can switch the model in config.toml from opus to sonnet (cheaper, less likely to hit limits). Log the change. Switch back after a period of successful operations.

## Files to modify

- `foreman/observer.py` — add rate limit detection (log scanning), write backoff marker
- `foreman/scheduler.py` — check backoff marker before spawning
- `foreman/config.py` — add `backoff_duration` and `max_backoff` settings
