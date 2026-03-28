<!-- foreman:innovator -->
# _read_exit_code duplicated between loop.py and watchdog.py

> **Depends on:**

## Problem

The refactoring that split loop.py into scheduler/merge/watchdog created an exact copy of `_read_exit_code` in two places:

`loop.py:280-286`:
```python
def _read_exit_code(self, sentinel_name: str) -> int:
    done_file = self.config.repo_root / ".foreman" / "done" / sentinel_name
    try:
        return int(done_file.read_text().strip())
    except (ValueError, FileNotFoundError):
        log.warning("Sentinel file missing or unreadable for %s, treating as crash", sentinel_name)
        return 1
```

`watchdog.py:106-112`: identical implementation.

Both access `self.config.repo_root` to construct the done directory path. This violates the CLAUDE.md rule "What's duplicated across the codebase that should be unified?"

## Solution

Extract `_read_exit_code` as a free function in a shared location. Since both loop.py and watchdog.py already import from `spawner.py` (which owns the sentinel/done directory concepts via `AGENT_TYPE_SEP` and `log_filename`), place it there:

```python
def read_exit_code(done_dir: Path, sentinel_name: str) -> int:
    done_file = done_dir / sentinel_name
    try:
        return int(done_file.read_text().strip())
    except (ValueError, FileNotFoundError):
        log.warning("Sentinel file missing or unreadable for %s, treating as crash", sentinel_name)
        return 1
```

Update both callers to use `read_exit_code(self.config.repo_root / ".foreman" / "done", sentinel_name)`.

## Scope

- `foreman/spawner.py` — add `read_exit_code` free function
- `foreman/loop.py` — replace `_read_exit_code` method with call to shared function
- `foreman/watchdog.py` — replace `_read_exit_code` method with call to shared function

## Risk Assessment

Trivially safe. Pure extraction of identical code into a shared function with no behavioral change.
