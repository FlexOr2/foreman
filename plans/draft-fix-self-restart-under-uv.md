# Fix Self-Restart Under uv run

## Problem

`os.execv(sys.executable, [sys.executable, "-m", "foreman.cli", "start"])` fails when foreman is launched via `uv run foreman start`. The `os.execv` replaces the process with raw `python` which doesn't have the uv-managed venv activated. The restarted process either crashes (missing dependencies) or exits silently.

## Solution

Instead of `os.execv`, use a marker file + exit approach:

1. When restart is needed, write a marker file `.foreman/restart_pending`
2. Exit cleanly with a special exit code (e.g., 75)
3. Wrap the `foreman start` invocation in a restart loop — either in the CLI layer or via a shell wrapper

### Implementation in `cli.py`

```python
@app.command
def start(repo: Path = Path("."), debug: bool = False) -> None:
    while True:
        config = load_config(repo.resolve())
        exit_code = asyncio.run(ForemanLoop(config).run())
        if exit_code != RESTART_EXIT_CODE:
            break
        log.info("Restarting foreman...")
```

In `loop.py`, `_try_restart` returns `RESTART_EXIT_CODE` instead of calling `os.execv`. The `run()` method returns the exit code.

This works with `uv run`, Docker, systemd, or any other process wrapper — no `os.execv` needed.

## Files to modify

- `foreman/loop.py` — replace `os.execv` with clean exit returning restart code
- `foreman/cli.py` — wrap `ForemanLoop.run()` in a restart loop
- `foreman/config.py` — add `RESTART_EXIT_CODE` constant

## Risk

None. Strictly better than `os.execv` — works in all environments, no process replacement.
