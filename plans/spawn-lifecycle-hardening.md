# Spawn Lifecycle Hardening

The agent spawn-to-completion lifecycle has several gaps: no timeout on readiness wait, no cleanup of stale sentinel files, no recovery from orphaned tmux windows on restart.

## Issues

### 1. `_wait_for_ready` silently gives up (Medium-High)

`Spawner._wait_for_ready()` loops for `timeout` seconds checking for the `❯` prompt. If the prompt never appears (agent crash, tmux failure), it silently returns `None` — then `send_text` fires the initial message into a dead or unready pane.

**Fix**: Raise `TimeoutError` if the prompt is not detected. The caller in `loop.py` already catches `Exception` and marks the plan as FAILED.
