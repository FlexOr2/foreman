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

### Event Loop (`loop.py`)
Thin orchestrator composing all subsystems. Runs an `asyncio.TaskGroup` with concurrent tasks: plan watcher, log watcher, done watcher, scheduler loop, watchdog loop, innovator loop, dashboard, and completion poller. Signal handlers (SIGINT/SIGTERM) trigger graceful shutdown. Owns plan scanning, sentinel processing, and event dispatch — delegates scheduling to `AgentScheduler`, merging to `PlanMerger`, and watchdog duties to `AgentWatchdog`.

### Agent Scheduler (`scheduler.py`)
Manages spawn decisions, slot limits, and the agent lifecycle. Tracks pending reviews, active agent IDs, and a schedule event. Spawns implementation, review, and fix agents, handles their completion callbacks, and cascades failures to dependent plans. Communicates with `PlanMerger` via a callback when a plan is ready to merge.

### Plan Merger (`merge.py`)
Serializes merges via `asyncio.Lock`. Merges branches into main, invokes the brain for conflict resolution, finalizes successful merges (archive plan, remove worktree), and sets `restart_pending` when a merge modifies foreman's own code.

### Agent Watchdog (`watchdog.py`)
Periodic orphan reconciliation: detects agent windows that disappeared without writing a sentinel and processes their completion. Handles stuck-agent detection callbacks (warn or kill based on config). Manages restart draining — waits for all agents and pending reviews to finish before signaling shutdown.

### Brain (`brain.py`)
Persistent Claude CLI session via `claude -p --resume`. Serialized via `asyncio.Lock`. Used for merge conflict resolution — receives the diff with conflict markers and uses Read/Edit/Bash tools to resolve. Falls back to fresh session on failure, carrying context from `.foreman/context.md`. Summarizes session on shutdown.

### Plan Parser (`plan_parser.py`)
Reads `*.md` files from `plans/`, ignores `draft-*` prefix. Extracts dependencies from `> **Depends on: plan-a, plan-b**` lines.

### Dependency Resolver (`resolver.py`)
DFS-based cycle detection, topological wave computation. `get_ready_plans()` returns plans whose dependencies are satisfied and that aren't already running.

### Coordination DB (`coordination.py`)
SQLite with WAL mode and busy timeout. Two tables: `plans` (status, branch, worktree path) and `agents` (type, PID, log file, exit code). Source of truth for all runtime state. Enums: `PlanStatus`, `AgentType`, `ReviewVerdict`, `StuckAction`.

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

`StuckDetector` uses per-agent `asyncio.TimerHandle` — fires callback when no log activity for `stuck_threshold` seconds. `CompletionDetector` polls agent terminals for idle prompts and sends `/exit` when detected.

### Innovation Engine (`innovate.py`)
Autonomous improvement discovery with adversarial review pipeline. Uses a separate brain session to explore the codebase, generate improvement plans, and run them through devil's advocate / pragmatist / architect reviewers. Plans that survive review are written as draft files.

### Dashboard (`dashboard.py`)
Rich Live display: worker/review slot counts, plan status table with active agent type and time-ago timestamps, innovator activity log, summary line. Refreshes every 2 seconds.

### Config (`config.py`)
Loads `.foreman/config.toml`, provides typed dataclass defaults. `Config.get_prompt_path()` resolves prompt template locations. Per-plan timeout overrides. All paths relative to repo root, resolved on load.

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
foreman unblock <plan>          # Re-queue BLOCKED/FAILED (--clean to remove worktree)

foreman kill <plan>             # Hard kill all agent types
foreman reset                   # Drop DB, remove worktrees, clean up

foreman innovate                # Run autonomous improvement discovery
foreman logs                    # View structured logs with filters
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

## Module Dependency Graph

```
cli.py
  └─ loop.py (orchestrator)
       ├─ scheduler.py (spawn decisions, agent lifecycle)
       ├─ merge.py (branch merging, conflict resolution)
       ├─ watchdog.py (orphan detection, stuck handling, restart)
       ├─ monitor.py (inotify watchers, stuck/completion detectors)
       ├─ spawner.py (tmux/vscode backend, launcher scripts)
       ├─ brain.py (persistent Claude session)
       ├─ dashboard.py (Rich live display)
       └─ innovate.py (autonomous improvement discovery)

plan_parser.py ← resolver.py (DAG resolution)
coordination.py (shared enums + SQLite DB)
config.py (configuration loading)
worktree.py (git operations)
preflight.py (prerequisite checks)
```

## Key Design Decisions

**`--append-system-prompt`** adds to Claude Code's default prompt instead of replacing it. Agents keep all built-in capabilities while getting Foreman-specific instructions.

**Launcher scripts** avoid shell-in-shell escaping. Each agent gets a `.sh` script with `$(cat prompt.md)` for clean prompt loading.

**`--add-dir`** grants worktree agents access to `plans/` in the main repo.

**Git worktrees** provide full filesystem isolation. One branch per plan, conflicts handled at merge time.

**Event-driven** via `asyncinotify` (Linux inotify) and `asyncio`. No threads, no polling.

**Persistent brain session** via `--resume` gives conversational memory across merge conflicts. Summarized on shutdown, context carried to next session.

**Separate worker/review limits** prevent reviews from being starved when all worker slots are taken.

**Callback-based composition** — `loop.py` wires `AgentScheduler`, `PlanMerger`, and `AgentWatchdog` together via callbacks, keeping each module import-free of the others.

## Limitations

- Linux only (asyncinotify requires inotify)
- No cross-repo orchestration
- No remote execution
- Brain session grows over many waves (summarization on shutdown mitigates)

## Tech Stack

- Python 3.12, asyncio, asyncinotify
- Claude Code CLI v2.1+ (`--append-system-prompt`, `--permission-mode`, `--resume`)
- SQLite (WAL mode), git worktrees, tmux
- rich (dashboard), cyclopts (CLI), tomllib (config)
- TypeScript (optional VS Code extension)
