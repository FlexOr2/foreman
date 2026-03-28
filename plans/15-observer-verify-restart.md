<!-- foreman:innovator -->
# Observer restarts foreman but never verifies the restart succeeded

> **Depends on:** observer-crashes-on-merge-merge-branch-returns-3-tuple-but-observer-unpacks-2

## Problem

In `observer.py:103-110`, `_start_foreman` spawns a subprocess and returns immediately without verifying the process actually started:

```python
def _start_foreman(repo_root: Path) -> None:
    subprocess.Popen(
        [sys.executable, "-m", "foreman.cli", "start"],
        cwd=repo_root,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
```

Then on line 155-156, after starting foreman, the observer calls `continue` which skips all DB checks for that cycle. On the next cycle (30s later), it checks if foreman is running via PID file. But if foreman crashed immediately (before writing PID), `is_process_running` returns False and the observer tries again — indefinitely.

Critically, `stdout=DEVNULL, stderr=DEVNULL` means startup errors (import failures, config errors, missing tmux) are completely lost.

## Solution

Return the `Popen` object and check it on the next cycle. Also redirect stderr to a log file for debugging:

```python
def _start_foreman(repo_root: Path) -> subprocess.Popen:
    stderr_log = repo_root / FOREMAN_DIR / "foreman-start.log"
    return subprocess.Popen(
        [sys.executable, "-m", "foreman.cli", "start"],
        cwd=repo_root,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=stderr_log.open("w"),
    )
```

In `observe_loop`, track the last-started process and check `proc.poll()` before attempting another restart. If the process exited immediately (poll returns non-None within seconds of start), log the error and increase the check interval.

## Scope

- `foreman/observer.py` — return Popen from `_start_foreman`, capture stderr, track process in `observe_loop`, add fast-exit detection

## Risk Assessment

Low risk. Changes are additive — the observer still restarts foreman, just with better diagnostics. The stderr capture to a file means startup errors are finally visible to the user.
