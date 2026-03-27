# Foreman — Architecture

## Overview

Foreman is a Python async process that orchestrates multiple Claude Code agents across a codebase. It reads plan files, resolves dependencies into a DAG, spawns agents in parallel git worktrees, runs self-review cycles, resolves merge conflicts via a persistent "brain" session, and merges results into main.

```
plans/ → DAG resolution → spawn in worktrees → review → merge → next wave
```

All agents run in visible tmux windows. Foreman is event-driven (inotify + asyncio), not polling.

## Project Layout

```
repo/
  plans/                              # Plan files (checked into git)
    add-logging.md
    migrate-redis.md
    draft-future-idea.md              # draft- prefix = ignored
  .foreman/                           # Everything else (gitignored)
    config.toml                       # Configuration
    prompts/                          # Agent prompt templates
      prompt-implementation.md
      prompt-review.md
      prompt-fix.md
    coordination.db                   # SQLite — plan status, agent PIDs
    logs/                             # Agent log files (for stuck detection)
    worktrees/                        # Git worktrees (one per plan)
    scripts/                          # Launcher scripts (one per agent)
    done/                             # Sentinel files (agent exit codes)
    session_id                        # Brain session persistence
    context.md                        # Brain context summary across restarts
```

## Components

### Brain (`brain.py`)
Persistent Claude CLI session via `claude -p --resume`. Serialized via `asyncio.Lock`. Used for merge conflict resolution — receives the diff with conflict markers and uses Read/Edit/Bash tools to resolve. Falls back to fresh session on failure, carrying context from `.foreman/context.md`. Summarizes session on shutdown.

### Event Loop (`loop.py`)
`asyncio.TaskGroup` running five concurrent tasks: plan watcher, log watcher, done watcher, scheduler, and dashboard. Signal handlers (SIGINT/SIGTERM) trigger graceful shutdown.

### Plan Parser (`plan_parser.py`)
Reads `*.md` files from `plans/`, ignores `draft-*` and `prompt-*` prefixes. Extracts dependencies from `> **Depends on: plan-a, plan-b**` lines and phase headers.

### Dependency Resolver (`resolver.py`)
DFS-based cycle detection, topological wave computation. `get_ready_plans()` returns plans whose dependencies are satisfied and that aren't already running.

### Coordination DB (`coordination.py`)
SQLite with WAL mode and busy timeout. Two tables: `plans` (status, branch, worktree path) and `agents` (type, PID, log file, exit code). Source of truth for all runtime state. Enums: `PlanStatus`, `AgentType`, `ReviewVerdict`.

### Worktree Manager (`worktree.py`)
Async git operations: create/remove worktrees, merge branches, get conflict files, complete merges. All via `asyncio.create_subprocess_exec`.

### Agent Spawner (`spawner.py`)
Writes launcher scripts (`.foreman/scripts/`) that invoke `claude` with `--append-system-prompt`, `--permission-mode`, `--model`, `--name`, `--add-dir`. Spawns via tmux `new-window` with `pipe-pane` for log capture. Auto-detects VS Code extension backend (`.foreman/extension.sock`).

Terminal and sentinel names include agent type (e.g., `redis__implementation`, `redis__review`) to avoid collisions during the review/fix cycle.

### Progress Monitor (`monitor.py`)
`asyncinotify` watches on three directories:
- `plans/` — new/modified plan files
- `.foreman/logs/` — agent activity (resets stuck timer on each write)
- `.foreman/done/` — sentinel files (agent completion)

`StuckDetector` uses per-agent `asyncio.TimerHandle` — fires callback when no log activity for `stuck_threshold` seconds.

### Dashboard (`dashboard.py`)
Rich Live display: worker/review slot counts, plan status table with active agent type and time-ago timestamps, summary line. Refreshes every 2 seconds.

### Config (`config.py`)
Loads `.foreman/config.toml`, provides typed dataclass defaults. `Config.get_prompt_path()` resolves prompt template locations. All paths relative to repo root, resolved on load.

### Preflight (`preflight.py`)
Checks for `git`, `tmux`, and `claude` before `init`/`start`. Auto-discovers claude from VS Code extension bundles, `~/.claude/local/`, and PATH.

### CLI (`cli.py`)
Built with cyclopts:

```
foreman init                    # Set up repo: plans/, .foreman/, prompts, config
foreman start                   # Event loop + dashboard
foreman plan                    # Dry run — show execution order
foreman status                  # One-shot status table

foreman pause <plan>            # Kill agent, mark INTERRUPTED, keep worktree
foreman resume <plan>           # Re-spawn in existing worktree
foreman guide <plan> "message"  # Send text to running agent

foreman kill <plan>             # Hard kill all agent types
foreman reset                   # Drop DB, remove worktrees, clean up
```

## Agent Lifecycle

```
QUEUED → spawn worktree + implementation agent → RUNNING
  → agent exits 0 → spawn review agent → REVIEWING
    → verdict "clean" → merge to main → DONE
    → verdict "findings" → spawn fix agent → RUNNING → re-review
    → verdict "architectural" → BLOCKED
    → max retries exceeded → BLOCKED
  → agent exits non-0 → FAILED
  → merge conflict → brain resolves or → BLOCKED
  → user pauses → INTERRUPTED → user resumes → RUNNING
```

## Key Design Decisions

**`--append-system-prompt`** adds to Claude Code's default prompt instead of replacing it. Agents keep all built-in capabilities while getting Foreman-specific instructions.

**Launcher scripts** avoid shell-in-shell escaping. Each agent gets a `.sh` script with `$(cat prompt.md)` for clean prompt loading.

**`--add-dir`** grants worktree agents access to `plans/` in the main repo.

**Git worktrees** provide full filesystem isolation. One branch per plan, conflicts handled at merge time.

**Event-driven** via `asyncinotify` (Linux inotify) and `asyncio`. No threads, no polling.

**Persistent brain session** via `--resume` gives conversational memory across merge conflicts. Summarized on shutdown, context carried to next session.

**Separate worker/review limits** prevent reviews from being starved when all worker slots are taken.

## Limitations

- Linux only (asyncinotify requires inotify)
- No cross-repo orchestration
- No remote execution
- No plan generation (plans authored manually)
- Brain session grows over many waves (summarization on shutdown mitigates)

## Tech Stack

- Python 3.12, asyncio, asyncinotify
- Claude Code CLI v2.1+ (`--append-system-prompt`, `--permission-mode`, `--resume`)
- SQLite (WAL mode), git worktrees, tmux
- rich (dashboard), cyclopts (CLI), tomllib (config)
- TypeScript (optional VS Code extension)
