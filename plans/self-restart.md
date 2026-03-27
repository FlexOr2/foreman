# Graceful Self-Restart After Self-Improvement

When foreman merges a plan that modifies its own code (`foreman/`), it should automatically restart to pick up the improvements.

## Design

### Detection
After `_finalize_merge()`, check if any merged files are under `foreman/`. If so, set `self._restart_pending = True`.

Check via: `git diff --name-only HEAD~1 HEAD` after merge — if any path starts with `foreman/`, restart is needed.

### Drain Phase
Once restart is pending:
1. Stop scheduling new plans (skip `_try_spawn_ready`)
2. Stop the innovator loop
3. Wait for all active agents to finish their current cycle:
   - Implementation agent → wait for commit + done sentinel
   - Review agent → wait for verdict + done sentinel
   - Fix agent → wait for commit + done sentinel
4. Process all pending merges/reviews normally
5. Once no agents are active, proceed to restart

### State Preservation
Before restart:
1. Mark all non-terminal plans with their current state in DB (already there)
2. Plans that were QUEUED stay QUEUED — they'll be picked up after restart
3. Plans that were mid-cycle (RUNNING/REVIEWING but agents already exited) get processed normally before restart
4. Save brain context via `summarize_and_reset()`
5. Log the restart reason

### Restart
```python
import os
import sys
log.info("Restarting foreman to apply self-improvements")
os.execv(sys.executable, [sys.executable, "-m", "foreman.cli", "start"] + sys.argv[1:])
```

### Resume After Restart
The new process starts normally:
- Reads DB — sees completed plans as DONE, queued as QUEUED
- INTERRUPTED plans with existing worktrees can be resumed
- New code is active

### Config
```toml
[foreman]
auto_restart = true    # default true — restart after self-modifying merges
```

## Files to modify

- `foreman/loop.py` — restart detection in `_finalize_merge`, drain logic, `os.execv` restart
- `foreman/config.py` — `auto_restart` config option
- `foreman/worktree.py` — helper to check if merge touched `foreman/` files

## Edge Cases

- Multiple self-modifying plans merge in sequence — only restart once after the last one
- Restart during merge conflict resolution — complete the resolution first, then restart
- Brain summarization fails during restart — catch and continue (already handled)
