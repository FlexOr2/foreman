<!-- foreman:innovator -->
# Recovery doesn't restore _active_agent_ids, leaving dangling DB records

> **Depends on:**

## Problem

In `_recover_running_plans` (loop.py:166-172), when a live agent window is found during startup recovery, the plan is re-registered in stuck and completion detectors but NOT in `_active_agent_ids`:

```python
if agent_type:
    terminal = self.spawner.terminal_name(plan_name, agent_type)
    if await self.spawner.has_window(terminal):
        log.info("Plan %s still has live agent in %s, re-registering", plan_name, terminal)
        self.stuck.track(plan_name, terminal)
        self.completion.track(plan_name, terminal)
        continue  # _active_agent_ids NOT populated
```

When that agent later finishes and `_handle_agent_done` runs (loop.py:244-246):

```python
agent_id = self._active_agent_ids.pop(plan_name, None)  # returns None
if agent_id is not None:
    self.db.finish_agent(agent_id, exit_code)  # never called
```

The agent's DB record in the `agents` table is never finalized — `finished_at` stays NULL and `exit_code` stays NULL. Over time, the `agents` table accumulates unfinished records. This also means `get_active_agent_type` (coordination.py:221-226) can return stale agent types for plans that have already progressed to a later stage.

## Solution

During recovery, look up the agent_id from the DB and populate `_active_agent_ids`:

```python
if agent_type:
    terminal = self.spawner.terminal_name(plan_name, agent_type)
    if await self.spawner.has_window(terminal):
        log.info("Plan %s still has live agent in %s, re-registering", plan_name, terminal)
        self.stuck.track(plan_name, terminal)
        self.completion.track(plan_name, terminal)
        agents = self.db.get_agents_for_plan(plan_name)
        active = [a for a in agents if a["finished_at"] is None]
        if active:
            self._active_agent_ids[plan_name] = active[-1]["id"]
        continue
```

`get_agents_for_plan` already exists and returns agents ordered by `started_at`. Take the last unfinished one.

## Scope

- `foreman/loop.py` — restore `_active_agent_ids` entry during recovery in `_recover_running_plans`

## Risk Assessment

Low risk. The fix reads existing data from the DB and populates an in-memory dict. The only subtle case is if multiple unfinished agents exist for the same plan (from a previous crash during agent handoff) — taking the last one by `started_at` is the correct choice.
