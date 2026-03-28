<!-- foreman:innovator -->
# Web resume endpoint spawns agent outside main loop's monitoring subsystem

> **Depends on:**

## Problem

The web dashboard's `/plans/{plan_name}/resume` endpoint (web.py:522-551) spawns an agent in a separate FastAPI process:

```python
spawner = Spawner(config)
await spawner.setup()
pid = await spawner.spawn_agent(plan_obj, Path(worktree_path), AgentType.IMPLEMENTATION, msg)
db2 = CoordinationDB(config.coordination_db)
db2.set_plan_status(plan_name, PlanStatus.RUNNING)
db2.add_agent(plan_name, AgentType.IMPLEMENTATION, pid=pid)
db2.close()
```

The agent is recorded in the DB and runs in a tmux window, but it's NOT registered with the main loop's `StuckDetector`, `CompletionDetector`, or `scheduler.active_agent_ids`. This means:

1. No stuck detection — the agent can hang indefinitely without warning
2. No hard timeout enforcement — the agent ignores the `timeouts.implementation` limit
3. No completion detection — if the agent goes idle, no `/exit` is sent
4. The `finish_agent` path won't finalize the DB record (missing from `active_agent_ids`)

The agent runs unmonitored until either: (a) it writes a sentinel file and the done watcher catches it, or (b) the watchdog's 30s reconciliation discovers the window exists and re-registers it. But (b) doesn't register the agent_id, so the dangling-DB-records issue from the existing plan still applies.

## Solution

Don't spawn agents from the web process. Instead, change the resume endpoint to only update the DB status, then signal the main loop to handle the actual spawn:

```python
@app.post("/plans/{plan_name}/resume")
async def resume_plan(plan_name: str) -> RedirectResponse:
    if not config.coordination_db.exists():
        return RedirectResponse("/", status_code=303)
    db = CoordinationDB(config.coordination_db)
    try:
        plan_data = db.get_plan(plan_name)
        if not plan_data or PlanStatus(plan_data["status"]) != PlanStatus.INTERRUPTED:
            return RedirectResponse("/", status_code=303)
        db.set_plan_status(plan_name, PlanStatus.QUEUED)
    finally:
        db.close()
    plan_file = config.plans_dir / f"{plan_name}.md"
    if plan_file.exists():
        plan_file.touch()
    return RedirectResponse("/", status_code=303)
```

Setting the status to QUEUED and touching the plan file wakes the main loop's scheduler via inotify, which then handles the spawn through the normal code path with full monitoring. The existing worktree is preserved since `spawn_implementation` reuses existing worktrees (`create_worktree` returns early if the worktree exists at worktree.py:37-39).

Apply the same fix to `cli.py`'s `resume` command (lines 381-431) which has the same issue.

## Scope

- `foreman/web.py` — replace agent spawn in `resume_plan` with status transition to QUEUED + plan file touch
- `foreman/cli.py` — same change for the `resume` CLI command

## Risk Assessment

Medium risk. The main behavioral change is that resumed agents go through the normal scheduling path instead of being spawned directly. This means they respect parallelism limits and get the resume-specific initial message from `_recover_running_plans`. The tradeoff: the agent might not restart immediately if worker slots are full. But this is correct behavior — spawning beyond the parallelism limit was a bug in the current resume implementation.
