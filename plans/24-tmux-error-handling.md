<!-- foreman:innovator -->
Now I have all the evidence I need. Here are the findings:

# TmuxBackend.create_terminal silently swallows errors

> **Depends on:**

## Problem

In `spawner.py:71-83`, `TmuxBackend.create_terminal` calls `tmux new-window` and `tmux pipe-pane` but never checks return codes:

```python
async def create_terminal(self, name: str, command: str, log_file: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "new-window", "-t", TMUX_SESSION, "-n", name, command,
    )
    await proc.wait()  # return code ignored

    proc = await asyncio.create_subprocess_exec(
        "tmux", "pipe-pane", "-t", f"{TMUX_SESSION}:{name}",
        "-o", f"cat >> {log_file}",
    )
    await proc.wait()  # return code ignored

    log.info("Spawned agent %s in tmux window", name)  # logged even on failure
```

If `tmux new-window` fails (e.g., duplicate window name from a previous crash that wasn't cleaned up), the error is silently swallowed. The spawner logs "Spawned agent" regardless, `_active_agent_ids` is populated, and stuck/completion detectors are armed. But no agent is actually running. The plan stays RUNNING until the watchdog catches the orphan ~30s later, then goes through unnecessary orphan recovery.

If `pipe-pane` fails (new-window succeeded but pipe-pane didn't), the agent runs but produces no log file output. The stuck detector relies on log activity via inotify — no log writes means the agent appears stuck immediately after `stuck_threshold` seconds, even though it's working normally.

## Solution

Check return codes and raise on failure. The caller (`Spawner.spawn_agent` at line 270) is already inside a try/except that handles spawn failures by setting the plan to FAILED.

```python
async def create_terminal(self, name: str, command: str, log_file: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "new-window", "-t", TMUX_SESSION, "-n", name, command,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"tmux new-window failed for {name} (rc={proc.returncode})")

    proc = await asyncio.create_subprocess_exec(
        "tmux", "pipe-pane", "-t", f"{TMUX_SESSION}:{name}",
        "-o", f"cat >> {log_file}",
    )
    await proc.wait()
    if proc.returncode != 0:
        log.warning("tmux pipe-pane failed for %s, agent will run without log capture", name)
```

Raise on `new-window` failure (no point continuing). Warn on `pipe-pane` failure (agent can still run, just without log capture).

## Scope

- `foreman/spawner.py` — add return code checks in `TmuxBackend.create_terminal`

## Risk Assessment

Low risk. The caller already handles spawn exceptions. The only behavioral change is failing fast instead of silently creating a ghost agent.
