<!-- foreman:innovator -->
Confirmed: no signal trap in the launcher script. The sentinel line after `claude` only runs if bash reaches it after normal process exit. On SIGHUP (from tmux kill-window), bash terminates immediately and the sentinel is never written.

# Launcher script has no EXIT trap — sentinel never written when agent is killed

> **Depends on:**

## Problem

In `spawner.py:207-231`, `_build_launcher_script` generates a bash script where the sentinel file is written AFTER the claude command:

```bash
#!/bin/bash
cd '/path/to/worktree'
claude \
  --append-system-prompt "$(cat ...)" \
  ...
_ec=$?; echo $_ec > /path/to/sentinel.tmp && mv /path/to/sentinel.tmp /path/to/sentinel
```

When tmux `kill-window` is called (by the web kill endpoint, CLI kill/pause, stuck detector kill, or hard timeout), bash receives SIGHUP and terminates immediately. The `_ec=$?; echo ...` line never executes. No sentinel file is created.

The done_watcher (`monitor.py:201-217`) only detects agent completion via sentinel files. Without the sentinel, the plan stays in RUNNING/REVIEWING until the watchdog reconciles it 30 seconds later (`watchdog.py:17`, `WATCHDOG_INTERVAL = 30`). This means every kill operation has a 30-second delay before the system recognizes the agent is gone, during which the scheduler can't reclaim the worker slot and stuck/timeout timers keep running.

## Solution

Add a bash `trap '...' EXIT` at the top of the launcher script so the sentinel is written regardless of how bash exits:

```python
def _build_launcher_script(...) -> str:
    ...
    sentinel_name = f"{plan.name}{AGENT_TYPE_SEP}{agent_type.value}"
    sentinel_path = shlex.quote(str(done_dir / sentinel_name))

    lines = [
        "#!/bin/bash",
        f"_ec=1",
        f"trap 'echo \"$_ec\" > {sentinel_path}.tmp && mv {sentinel_path}.tmp {sentinel_path}' EXIT",
        f"cd {shlex.quote(str(worktree_path.resolve()))}",
    ]
    # ... cmd_parts ...
    lines.append(" \\\n".join(cmd_parts))
    lines.append(f"_ec=$?")
    return "\n".join(lines) + "\n"
```

The `_ec=1` default means a signal-killed agent reports exit code 1 (failure). If claude exits normally, `_ec=$?` overwrites with the actual exit code. The EXIT trap fires in both cases, ensuring the sentinel is always written.

## Scope

- `foreman/spawner.py` — restructure `_build_launcher_script` to use EXIT trap instead of post-command sentinel line

## Risk Assessment

Low risk. The EXIT trap is standard bash behavior. The sentinel is written atomically via the same tmp+mv pattern. The only edge case is if the filesystem is read-only or full — but that's the same failure mode the current code has. Normal exit behavior is unchanged (same sentinel content). Kill behavior improves from 30s-delayed detection to immediate detection.
