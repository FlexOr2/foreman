<!-- foreman:innovator -->
# Web innovate endpoint runs concurrently with loop's innovator, corrupting shared brain session

> **Depends on:**

## Problem

The web dashboard's `/innovate` endpoint (`web.py:611-621`) triggers `innovate(config)` as a FastAPI background task. The main loop's `_innovator_loop` (`loop.py:319-341`) also calls `innovate(config)` on a periodic schedule.

Both calls create a `ForemanBrain` instance pointing to the same directory (`innovate.py:596-600`):

```python
brain = ForemanBrain(
    foreman_dir=foreman_dir / "innovator",
    ...
)
```

The brain stores session state in `.foreman/innovator/session_id` and `.foreman/innovator/context.md`. The brain's `_lock` (`brain.py:32`) is an `asyncio.Lock` — it only protects within a single instance. Two brain instances in different processes (web server vs main loop) have independent locks, so concurrent runs corrupt the shared session files.

Additionally, the web endpoint has no concurrency guard — multiple rapid clicks spawn multiple concurrent background tasks within the web process.

## Solution

Add a file-based lock in `innovate()` to coordinate across processes, and an asyncio lock in the web endpoint to prevent concurrent clicks:

In `web.py`, guard against concurrent web invocations:

```python
_innovate_lock = asyncio.Lock()

async def _run_innovate_background(config: Config) -> None:
    if _innovate_lock.locked():
        log.info("Innovate already running, skipping")
        return
    async with _innovate_lock:
        try:
            from foreman.innovate import innovate
            await innovate(config)
        except Exception:
            log.error("Background innovate failed", exc_info=True)
```

In `innovate.py`, add a PID-based file lock at the start of `innovate()`:

```python
lock_path = foreman_dir / "innovator" / "innovate.lock"
if lock_path.exists():
    try:
        pid = int(lock_path.read_text().strip())
        if _is_pid_alive(pid):
            log.info("Another innovate is running (PID %d), skipping", pid)
            return []
    except (ValueError, FileNotFoundError):
        pass
lock_path.parent.mkdir(parents=True, exist_ok=True)
lock_path.write_text(str(os.getpid()))
try:
    # ... existing innovate logic ...
finally:
    lock_path.unlink(missing_ok=True)
```

## Scope

- `foreman/web.py` — add `_innovate_lock` and error handling to `_run_innovate_background`
- `foreman/innovate.py` — add PID-based file lock at the start of `innovate()`

## Risk Assessment

Medium risk. The file lock can leave stale lock files if the process is killed (mitigated by PID-alive check). The web endpoint also gains error handling — exceptions were previously silently swallowed by FastAPI's background task runner.
