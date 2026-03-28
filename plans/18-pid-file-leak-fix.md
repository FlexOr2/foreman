<!-- foreman:innovator -->
# Foreman run() leaks PID file when startup fails before TaskGroup

> **Depends on:**

## Problem

In `loop.py:70-100`, `write_pid()` is called at line 73, before `spawner.setup()` (line 74), `_scan_plans()` (line 80), and `_recover_running_plans()` (line 82). The `remove_pid()` call is in `_graceful_shutdown()` (line 130), only reachable from the `finally` block at line 97 — inside the `try` that wraps the TaskGroup at line 84.

If any of lines 74-82 raise, the exception exits `run()` before reaching the TaskGroup's try/finally. The PID file persists, pointing to a dead process.

The web dashboard's `_render_header` (`web.py:366`) checks `is_process_running()` which reads this stale PID, calls `is_pid_alive()` (returns False for dead process), and correctly shows "stopped". But if the PID is reused by an unrelated process, `is_pid_alive()` returns True, and the dashboard shows foreman as "running" when it's actually crashed.

## Solution

Move `write_pid` after all fallible startup operations:

```python
async def run(self) -> int:
    check_prerequisites()
    self.config.ensure_dirs()
    await self.spawner.setup()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, self._request_shutdown)

    await self._scan_plans()
    await self._process_stale_sentinels()
    await self._recover_running_plans()

    write_pid(self.config.repo_root, PID_FILE_FOREMAN)
    # ... TaskGroup ...
```

The PID file is written only after startup succeeds. The observer correctly sees foreman as "not running" during startup (no PID file yet), which is accurate — foreman isn't accepting work until the TaskGroup starts.

## Scope

- `foreman/loop.py` — move `write_pid()` from line 73 to after line 82

## Risk Assessment

Very low. The only change is that foreman isn't considered "running" during startup. Since startup takes < 1 second typically, and the observer checks every 30 seconds, this window is invisible in practice.
