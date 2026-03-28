<!-- foreman:innovator -->
# Observer uses bare strings for agent types instead of AgentType enum

> **Depends on:**

## Problem

In `observer.py:174`, the observer iterates agent type suffixes as hardcoded strings:

```python
for suffix in ("implementation", "review", "fix"):
    terminal = f"{plan_name}{AGENT_TYPE_SEP}{suffix}"
```

The canonical source of truth for agent types is `AgentType` enum in `coordination.py`. If a new agent type is added (or an existing one renamed), the observer won't check for it, silently leaving those windows undetected during orphan checks. This violates the CLAUDE.md rule: "AgentType enum is the canonical way to refer to agent types. No bare strings."

The same pattern appears at observer.py:183 where `active_plans` is queried but the window-to-plan mapping uses the string-split approach that assumes the agent type separator format.

## Solution

Use `AgentType` enum values:

```python
for agent_type in AgentType:
    terminal = f"{plan_name}{AGENT_TYPE_SEP}{agent_type.value}"
    if await _tmux_has_window(terminal):
        has_live_window = True
        break
```

Import `AgentType` from `foreman.coordination` (the module is already imported for `CoordinationDB` and `PlanStatus`).

## Scope

- `foreman/observer.py` — replace bare strings with `AgentType` enum iteration, add `AgentType` to the import from `coordination`

## Risk Assessment

Trivially safe. `AgentType` values are `"implementation"`, `"review"`, `"fix"` — identical to the current hardcoded strings. The change just routes through the enum.
