# Innovator Restart Safety

## Problem

When `_restart_pending` is set, the drain phase waits for worker agents to finish but the innovator loop keeps running. `os.execv()` fires while the brain is mid-innovation-cycle, killing the brain call. Any ideas shaped but not yet written as drafts are lost.

## Solution

### 1. Check `_restart_pending` in the innovator loop

The innovator loop in `loop.py` should check `self._restart_pending` at each phase boundary and exit early:

- Before starting a new cycle
- After explore/provoke (before shaping)
- After shaping (before review rounds)
- Between individual plan reviews

When `_restart_pending` is true, the loop should log "Innovator pausing for restart" and return, allowing the drain phase to complete.

### 2. Drain phase should wait for innovator

In `_try_restart`, after checking that no agents are active, also check that the innovator loop has exited. Add `self._innovator_running: bool` flag set to True when the loop starts a cycle and False when it finishes or exits early. `_try_restart` waits for this flag to be False.

## Files to modify

- `foreman/loop.py` — add `_restart_pending` checks in `_innovator_loop`, add `_innovator_running` flag, update `_try_restart` to wait for it
