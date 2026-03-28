<!-- foreman:innovator -->
# pipe-pane log path not shell-escaped, breaks on paths with spaces

> **Depends on:**

## Problem

In `spawner.py:78-80`, the `pipe-pane` command passes the log file path unescaped in a shell command string:

```python
proc = await asyncio.create_subprocess_exec(
    "tmux", "pipe-pane", "-t", f"{TMUX_SESSION}:{name}",
    "-o", f"cat >> {log_file}",
)
```

The `-o` argument to `pipe-pane` is interpreted by a shell. If `log_file` resolves to a path containing spaces (inherited from `config.repo_root`), e.g. `/home/user/my projects/.foreman/logs/plan__implementation.log`, the shell splits it into multiple arguments and `cat >>` fails silently.

The consequence: the agent runs, but no output is captured to the log file. Since the stuck detector relies on inotify watching log file modifications (`watch_logs` in monitor.py:181-198), the agent immediately appears stuck — the stuck timer fires after `stuck_threshold` seconds even though the agent is working normally.

Contrast with `_build_launcher_script` (spawner.py:209-227) which correctly uses `shlex.quote()` for all paths.

## Solution

Shell-quote the log path in the pipe-pane command:

```python
proc = await asyncio.create_subprocess_exec(
    "tmux", "pipe-pane", "-t", f"{TMUX_SESSION}:{name}",
    "-o", f"cat >> {shlex.quote(str(log_file))}",
)
```

The `shlex` import is already present in `spawner.py`.

## Scope

- `foreman/spawner.py` — shell-quote `log_file` in `pipe-pane` command (line 79)

## Risk Assessment

Trivially safe. Applies `shlex.quote` to a path that was already being passed to a shell context. No behavioral change for paths without spaces.
