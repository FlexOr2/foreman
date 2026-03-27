# Agent Exit Mechanism

Agents don't auto-exit after completing their task. The `/exit` command is unreliable — claude sometimes interprets it as `Skill(exit)` instead of the CLI exit command.

## Problem

1. Interactive claude sessions stay open after work is done
2. The prompt template says "run /exit" but claude treats it inconsistently
3. Without exit, the done sentinel never fires, blocking the review/merge cycle
4. Currently requires manual babysitting to send `/exit` via tmux

## Possible Approaches

### A: Use `--print` mode with streaming
Run agents in non-interactive mode (`claude -p "initial message" --output-format stream-json`). The process exits naturally when done. Downside: user can't intervene mid-execution.

### B: Hybrid — interactive with watchdog
Keep interactive mode but add a watchdog that detects task completion:
- Poll `tmux capture-pane` for idle prompt (`❯` with no activity for N seconds)
- If the agent has been idle at the prompt for 30+ seconds after producing output, assume done
- Send `/exit` via `send-keys` automatically
- The stuck detector already tracks log activity — extend it to detect "done but not exited"

### C: Use `--max-turns` or similar flag
Check if claude CLI has a flag to limit conversation turns. One turn = one task = auto exit.

### D: Pipe initial message via stdin
Instead of `send-keys`, pipe the initial message via stdin and close stdin after. Claude might exit when stdin closes.

## Recommended: Approach B (watchdog)

Most pragmatic. Keeps interactive mode (user can intervene), handles the `/exit` flakiness, and builds on existing infrastructure (StuckDetector, capture-pane).

## Implementation

### Extend StuckDetector or add CompletionDetector

New detector that polls `capture-pane` for the `❯` prompt. If the prompt is visible and no tool use is in progress for `completion_idle_seconds` (default 30), the agent is done:

```python
async def _check_agent_idle(self, plan_name: str, agent_type: AgentType) -> None:
    terminal = self.spawner._terminal_name(plan_name, agent_type)
    content = await self.spawner._capture_pane(terminal)
    if content and "❯" in content and "esc to interrupt" not in content:
        # Agent is at idle prompt, not processing
        await self.spawner.backend.send_text(terminal, "/exit")
```

Run this check periodically (every 10s) for all active agents.

### Also fix: long message paste detection

When `send-keys` sends a long initial message, claude shows "[Pasted text]" and waits for confirmation. Fix by sending the message in shorter chunks or using tmux's `send-keys -l` flag for literal text.

## Files to modify

- `foreman/monitor.py` or `foreman/loop.py` — add idle detection + auto-exit
- `foreman/spawner.py` — expose `_capture_pane` as public, fix paste detection for long messages
