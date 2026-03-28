# Foreman Observer

A separate lightweight process that watches foreman and intervenes when things go wrong. Foreman spawns the observer on startup, but the observer is independent — it survives foreman crashes and restarts.

## Why

Foreman can get stuck, crash, or corrupt its own code during self-improvement. The watchdog inside foreman only helps when foreman is running. An external observer is needed for:
- Restarting foreman after crashes
- Fixing orphaned plans when the watchdog fails
- Merging branches when foreman's merge logic breaks
- Cleaning stale tmux windows
- Detecting infinite loops (plan keeps getting re-implemented)

## Design

### The observer is a simple Python script

`foreman/observer.py` — NOT part of the main event loop. A separate `asyncio.run()` process. Deliberately simple — no brain, no innovator, no complex state. Just a health-check loop.

### Lifecycle

1. `foreman start` spawns the observer as a background process before entering the event loop
2. Observer writes its PID to `.foreman/observer.pid`
3. On subsequent `foreman start`, check if an observer is already running (read PID, check process). If alive, don't spawn another. If dead, spawn new one.
4. Observer runs until the user explicitly stops it (`foreman stop` or kills it)
5. If foreman self-restarts (exit code 75), the observer stays alive — it's a separate process

### What the observer does (every 30 seconds)

```python
async def observe_loop():
    while True:
        await asyncio.sleep(30)

        # 1. Is foreman alive?
        if not is_foreman_running():
            log.warning("Foreman is not running — restarting")
            start_foreman()
            continue

        # 2. Are there stuck plans?
        db = CoordinationDB(config.coordination_db)
        for plan in db.get_plans_by_status(PlanStatus.RUNNING) + db.get_plans_by_status(PlanStatus.REVIEWING):
            age_minutes = minutes_since(plan["updated_at"])
            terminal = f"{plan['plan']}__{agent_type}"
            has_window = tmux_has_window(terminal)

            if age_minutes > 20 and not has_window:
                # Orphaned — agent is gone, plan is stuck
                branch = plan.get("branch")
                if branch and branch_has_commits(branch):
                    # Work exists — try to merge
                    merge_result = git_merge(branch)
                    if merge_result.success:
                        db.set_plan_status(plan["plan"], PlanStatus.DONE)
                        remove_plan_file(plan["plan"])
                        log.info("Observer merged orphaned plan %s", plan["plan"])
                    else:
                        git_merge_abort()
                        db.set_plan_status(plan["plan"], PlanStatus.BLOCKED, reason="Observer: merge conflict")
                else:
                    db.set_plan_status(plan["plan"], PlanStatus.QUEUED)
                    log.info("Observer reset stuck plan %s to QUEUED", plan["plan"])

        # 3. Stale tmux windows?
        windows = tmux_list_windows()
        active_plans = {p["plan"] for p in db.get_plans_by_status(PlanStatus.RUNNING)}
        for window in windows:
            if window == "dashboard":
                continue
            plan_name = window.split("__")[0]
            if plan_name not in active_plans:
                tmux_kill_window(window)
                log.info("Observer killed stale window %s", window)

        db.close()
```

### How to start foreman

The observer uses `subprocess.Popen` to start foreman — NOT `os.execv`, NOT replacing itself:

```python
def start_foreman():
    subprocess.Popen(
        [sys.executable, "-m", "foreman.cli", "start"],
        cwd=config.repo_root,
        start_new_session=True,  # detach from observer's process group
    )
```

### Observer is NOT smart

The observer should NOT:
- Run the brain
- Make architectural decisions
- Generate plans
- Resolve merge conflicts (just abort and mark BLOCKED)

It's a janitor, not an architect. Keep it under 200 lines. It should be too simple to break.

### CLI integration

```bash
foreman start          # starts observer + foreman
foreman stop           # stops both
foreman observer       # run observer standalone (for debugging)
```

### PID management

```
.foreman/observer.pid  — observer PID
.foreman/foreman.pid   — foreman PID (written by foreman on start)
```

Observer checks foreman PID. Foreman checks observer PID on startup.

## Files to create

- `foreman/observer.py` — the observer loop (~150 lines)

## Files to modify

- `foreman/cli.py` — spawn observer on `foreman start`, add `foreman stop` and `foreman observer` commands
- `foreman/loop.py` — write `.foreman/foreman.pid` on startup
