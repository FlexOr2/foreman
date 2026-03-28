<!-- foreman:innovator -->
Good. Dashboard was fixed (uses glob, no `read_text`). Web still reads content. Here are the findings:

# Observer restart loop has no backoff — crashes foreman indefinitely if startup is broken

> **Depends on:**

## Problem

In `observer.py:150-156`, when foreman is detected as not running, the observer calls `_start_foreman` which spawns a new process with `stdout=DEVNULL, stderr=DEVNULL` (lines 103-110). If foreman crashes immediately after starting (config error, missing dependency, port conflict), the observer sees "not running" on the next 30s check and restarts it again, indefinitely.

```python
if not is_process_running(repo_root, PID_FILE_FOREMAN):
    log.warning("Foreman is not running — restarting")
    _start_foreman(repo_root)
    continue
```

There is no:
- Backoff between restart attempts
- Maximum retry count
- Check that the previous restart actually succeeded
- Logging of the foreman process's exit reason

A broken foreman config would cause the observer to spawn a new process every 30 seconds forever, filling the process table with short-lived zombies.

## Solution

Add restart tracking and exponential backoff:

```python
RESTART_BACKOFF_BASE = 30
RESTART_BACKOFF_MAX = 600
RESTART_MAX_FAST_FAILURES = 5
RESTART_FAST_WINDOW = 120

# In observe_loop:
recent_restarts: list[float] = []
```

Before calling `_start_foreman`, check how many recent restarts occurred within the fast window. If `RESTART_MAX_FAST_FAILURES` restarts happened in `RESTART_FAST_WINDOW` seconds, increase the check interval to `min(base * 2^n, max)` and log a critical warning. Reset the backoff when foreman stays alive for longer than the fast window.

Also have `_start_foreman` return the `Popen` object so the observer can check `process.poll()` on the next cycle to detect immediate crashes.

## Scope

- `foreman/observer.py` — add restart tracking, backoff logic, and immediate crash detection in `observe_loop` and `_start_foreman`

## Risk Assessment

Low risk. The observer is a safety net — adding backoff makes it more resilient, not less. The only concern is choosing the right backoff parameters: too aggressive means slow recovery from transient failures, too lenient means continued spam restarts.
