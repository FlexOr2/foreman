# Reliability Fixes

Collection of issues discovered during real-world testing that prevent foreman from running fully autonomously.

## Issues

### 1. Agent Exit (Critical)
Agents don't auto-exit after completing tasks. `/exit` is unreliable — claude interprets it as `Skill(exit)`. The done sentinel never fires without manual intervention.

**Fix**: Add idle detection. Poll `tmux capture-pane` every 10 seconds for active agents. If `❯` prompt is visible and `esc to interrupt` is NOT visible (meaning no tool is running) for 30+ consecutive seconds, the agent is done — send `/exit` via `send-keys`. Extend `StuckDetector` or add a `CompletionDetector` class in `monitor.py`.

### 2. Long Message Paste Detection (Medium)
When `send-keys` sends a long initial message (especially fix agent messages with JSON issues), claude shows "[Pasted text #1 +N lines]" and waits for Enter confirmation. The agent sits idle.

**Fix**: After sending text via `send-keys`, wait 2 seconds and `capture-pane`. If "[Pasted text" is visible, send `Enter` to confirm. Do this in `Spawner.spawn_agent()` right after sending the initial message.

### 3. Completed Plans Not Archived (Low)
After a plan is merged to main, the plan `.md` file stays in `plans/`. On restart, foreman would try to re-execute it (the DB is cleared by reset, but the file persists).

**Fix**: After `_finalize_merge()`, rename the plan file to `draft-{name}.md` (foreman ignores draft- prefixed files). Or move to `plans/done/`. Prefer rename — simpler, keeps the file visible.

### 4. Merge Conflict Brain Can't Run (Low)
The brain uses `CLAUDE_BIN` which is set by preflight. During merge conflict resolution, if preflight hasn't run in the current process (e.g., brain invoked during shutdown), `CLAUDE_BIN` is still `"claude"` and fails with `FileNotFoundError`.

**Fix**: `brain.py` already uses `_config.CLAUDE_BIN` via module reference. The issue is that `_graceful_shutdown` calls `brain.summarize_and_reset()` which invokes claude. Ensure preflight runs before any brain usage — move the `check_prerequisites` call into `ForemanLoop.__init__` or `run()` before the event loop starts, and have it set `CLAUDE_BIN` as a side effect.

## Files to modify

- `foreman/monitor.py` — idle detection / completion detector
- `foreman/spawner.py` — paste confirmation handling, expose `_capture_pane`
- `foreman/loop.py` — integrate idle detector, archive plans after merge, ensure preflight before brain
