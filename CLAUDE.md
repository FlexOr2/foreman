# CLAUDE.md — Project Rules

This codebase is in active use. The quality of your work directly affects a real product and a real developer's daily workflow.

## Code Style

- Clean, self-explaining code. If you need a comment, the code isn't clear enough.
- No inline comments unless the logic is genuinely non-obvious (crypto, bit manipulation, etc.).
- No hardcoded values. Constants go in config, enums, or module-level names.
- Use the type system. Enums over magic strings. Dataclasses over dicts. Path over str for paths.
- Small functions that do one thing. If a function needs a comment explaining what it does, split it.
- No defensive coding against impossible states. Trust internal interfaces.
- No premature abstraction. Three concrete cases before you extract a pattern.
- Prefer composition over inheritance.

## Architecture

- **Modules are single-responsibility.** Don't let coordination logic leak into spawner, or config logic into the event loop.
- **Config is the single source of truth** for all tunable values. Nothing hardcoded in module code.
- **AgentType enum** is the canonical way to refer to agent types. No bare strings like `"implementation"`.
- **All I/O is async.** Use `asyncio.create_subprocess_exec`, not `subprocess.run`. Use async file watchers, not polling.
- **The coordination DB is the source of truth** for runtime state (plan status, agent PIDs). Plan files on disk are input only.

## Dependencies

- Python 3.12+
- External: cyclopts, rich, asyncinotify
- Claude Code CLI v2.1+ (verified flags: `--append-system-prompt`, `--permission-mode`, `--allowed-tools`, `--add-dir`, `--resume`, `--name`, `--model`, `--output-format`)

## Testing

- Run `python3 -m pytest tests/` for tests.
- Core logic (parser, resolver, coordination) should be testable without tmux or Claude CLI.

## What NOT to do

- Don't add docstrings to obvious functions. `def close(self)` doesn't need a docstring.
- Don't add type annotations to variables where the type is obvious from the assignment.
- Don't wrap single-use logic in helper functions.
- Don't add backwards-compatibility shims or unused parameters "for the future."
- Don't create `utils.py` or `helpers.py`. If code belongs somewhere, put it there.
