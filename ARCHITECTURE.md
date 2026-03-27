# Foreman — Architecture

## What it does

Foreman is a **Python process that uses Claude Code as its brain** to orchestrate multiple Claude Code worker agents across a codebase. It reads plan files from a directory, determines execution order, spawns worker agents in parallel (each in its own git worktree), monitors their progress, handles merge conflicts intelligently, and picks up new plans as they appear.

```
You write/approve plans → Foreman detects them → spawns workers → merges results → repeats
```

All agents (workers and reviewers) run in visible tmux windows (MVP) or VS Code terminal tabs (with extension), so you can watch and intervene. Foreman runs as a background Python process — event-driven, not polling.

Foreman is **not a plan creator**. You author plans (with Claude's help in a separate session). Foreman executes them.

## Architecture Overview

```
Foreman (Python async event loop)
  │
  ├── Foreman Brain (persistent Claude CLI session via --resume)
  │     └── accumulates context: plans seen, merges done, conflicts resolved
  │     └── session ID saved to .foreman/session_id for restart recovery
  │     └── invoked on events, not running continuously
  │     └── all invocations serialized via asyncio.Lock
  │     └── error handling: falls back to fresh session on failure
  │
  ├── Event sources (no polling — native async inotify)
  │     ├── asyncinotify  → watches plans/ dir for new/renamed/modified files
  │     ├── asyncio       → awaits worker/review process exits
  │     └── asyncinotify  → watches log files + per-agent timers for stuck detection
  │
  ├── Worker agents (interactive claude CLI sessions)
  │     └── one per plan, each in its own git worktree
  │     └── --append-system-prompt for agent instructions
  │     └── --permission-mode for auto-approval of tool use
  │     └── visible in tmux windows (MVP) or VS Code tabs (extension)
  │     └── user can intervene directly
  │     └── notified via tmux send-keys if plan changes mid-execution
  │
  ├── Review agents (interactive claude CLI sessions)
  │     └── spawned after worker finishes, in same worktree
  │     └── visible in own tmux window — user can watch the review
  │     └── writes REVIEW_VERDICT.json in worktree root
  │     └── does NOT count against max_parallel_workers
  │
  └── Dashboard (rich TUI in its own tmux window)
```

## Verified Claude CLI Flags (v2.1.85)

All flags verified against the actual CLI. These are the flags Foreman uses:

| Flag | Purpose | Used by |
|---|---|---|
| `--append-system-prompt <text>` | Add instructions without replacing defaults | All agents |
| `--permission-mode <mode>` | Auto-approve tool use (`acceptEdits`, `dontAsk`) | All agents |
| `--allowed-tools <tools>` | Restrict which tools an agent can use | Review agents |
| `--add-dir <dirs>` | Grant access to additional directories | All agents (for plans/) |
| `--model <alias>` | Model selection (e.g., `opus`, `sonnet`) | All agents |
| `--name <name>` | Named session (shows in `/resume`) | All agents |
| `-p` / `--print` | Non-interactive mode, output to stdout | Brain only |
| `-r` / `--resume <id>` | Resume conversation by session ID | Brain only |
| `--output-format json` | Structured JSON output | Brain only |
| `--json-schema <schema>` | Validate structured output | Brain only |
| `-w` / `--worktree [name]` | Built-in worktree creation | Could simplify worktree.py |

**Key discovery**: `--append-system-prompt` adds to the default system prompt instead of replacing it. This means agents keep all of Claude Code's built-in capabilities while also getting Foreman-specific instructions. No CLAUDE.md manipulation needed.

**Key discovery**: `--permission-mode dontAsk` auto-approves all tool use. Perfect for autonomous agents.

## Brain Implementation

```python
class ForemanBrain:
    def __init__(self):
        self.session_id = self.load_session_id()  # from .foreman/session_id
        self._lock = asyncio.Lock()  # serialize all brain invocations

    async def think(self, prompt: str) -> str:
        """Send a prompt to the persistent Claude session."""
        async with self._lock:
            cmd = [
                "claude", "-p", prompt,
                "--output-format", "json",
                "--allowed-tools", "Read,Edit,Bash,Glob,Grep",
                "--permission-mode", "dontAsk",
            ]
            if self.session_id:
                cmd += ["--resume", self.session_id]

            try:
                result = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE
                )
                response = json.loads(await result.stdout.read())

                self.session_id = response["session_id"]
                self.save_session_id()
                return response["result"]
            except Exception:
                # Session corrupted or CLI failure — start fresh
                self.session_id = None
                context = self.load_context_summary()  # .foreman/context.md
                return await self._fresh_session(prompt, context)
```

Works with Max subscription — no API key needed.

## Plan Convention

Plans live in a configurable directory (default: `plans/`).

**Naming convention**:
- `draft-*.md` — Foreman ignores these. Work in progress, not ready.
- Anything else (`*.md`) — Foreman treats as ready for execution.

When you're done drafting, rename `draft-migration-redis.md` to `migration-redis.md`. Foreman detects the filesystem change via `asyncinotify` and picks it up immediately. You can drop new plans in while agents are running — Foreman rebuilds the DAG and slots them in.

**Plan changes during execution**: If you modify a plan file while its agent is running, Foreman detects the change and sends the agent a message via `tmux send-keys`:

```bash
tmux send-keys -t foreman:redis \
  "The plan has been updated. Re-read plans/migration-redis.md and adapt your approach." Enter
```

The agent sees this as a user message and adapts. Plans that are not currently RUNNING are simply re-parsed into the DAG.

**Required metadata** in plan files:
```markdown
> **Depends on: Phase 0, Redis migration**
```

Plans without a `Depends on` line are treated as having no dependencies (can run immediately). Dependencies reference other plan names (filename without `.md`).

No status field needed in the file itself — Foreman tracks status in its coordination DB. The filesystem (draft- prefix vs not) is the only input signal.

## Event-Driven Core Loop

Foreman does **not poll**. It uses `asyncinotify` (native async inotify on Linux) for filesystem events and `asyncio` for process lifecycle — no threads, no polling, pure async.

| Event | Source | Foreman's response |
|---|---|---|
| New plan file appears | `asyncinotify` on `plans/` | Parse plan, rebuild DAG, schedule if ready |
| Plan file renamed (draft- removed) | `asyncinotify` on `plans/` | Same as above |
| Plan file modified (RUNNING plan) | `asyncinotify` on `plans/` | Notify agent via `tmux send-keys` |
| Plan file modified (not RUNNING) | `asyncinotify` on `plans/` | Re-parse, rebuild DAG |
| Worker agent exits | `asyncio` process wait | Trigger review → merge → spawn next |
| Review agent exits | `asyncio` process wait | Read verdict file → merge or re-spawn or block |
| Log file goes silent | `asyncinotify` + per-agent timer | Mark agent as stuck, notify user |
| Worker hits timeout | `asyncio` timer | Notify user in dashboard |
| Merge conflict | `git merge` exit code | Invoke brain to resolve or escalate |

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  EVENT: new plan detected (asyncinotify)                     │
│    → Parse plan, extract dependencies                        │
│    → Add to DAG, check if ready                              │
│    → If ready + worker slots available: SPAWN                │
│                                                              │
│  EVENT: plan modified while agent RUNNING (asyncinotify)     │
│    → Send updated-plan notification to agent via tmux        │
│    → Agent re-reads plan and adapts                          │
│                                                              │
│  EVENT: worker process exited (asyncio)                      │
│    → Check exit code                                         │
│    → If success: spawn review agent (interactive, same       │
│      worktree, own tmux window — does not use worker slot)   │
│    → If failure: mark FAILED, notify user                    │
│                                                              │
│  EVENT: review agent exited (asyncio)                        │
│    → Read REVIEW_VERDICT.json from worktree root             │
│    → If clean: MERGE branch into main (first-finished-first) │
│    → If findings: re-spawn NEW worker with fixes prompt      │
│    → If architectural problem: mark BLOCKED, notify user     │
│                                                              │
│  EVENT: merge completed                                      │
│    → Remove worktree                                         │
│    → Mark plan DONE                                          │
│    → Check DAG: any plans now unblocked? → SPAWN             │
│                                                              │
│  EVENT: merge conflict                                       │
│    → Invoke Foreman brain (--resume, serialized via Lock)    │
│    → Brain reads both diffs + plans, resolves or escalates   │
│    → If resolved: complete merge                             │
│    → If escalated: mark BLOCKED, notify user                 │
│                                                              │
│  EVENT: log file inactive (per-agent timer expires)          │
│    → Mark agent as stuck in dashboard                        │
│    → User intervenes in the agent's tmux window              │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## Agent Display: tmux (MVP) + VS Code Extension

All agents (workers and reviewers) are interactive `claude` sessions the user can watch. Two display backends:

### tmux (MVP)

Foreman creates a tmux session with named windows per agent. Each agent is launched via a **launcher script** to avoid shell escaping issues:

```bash
# Foreman creates tmux windows automatically
tmux new-session -d -s foreman -n "dashboard"

# Foreman writes a launcher script per agent (avoids shell-in-shell escaping)
# .foreman/scripts/redis.sh:
#   #!/bin/bash
#   cd /abs/path/.foreman/worktrees/redis
#   exec claude \
#     --append-system-prompt "$(cat /abs/path/plans/prompt-implementation.md)" \
#     --permission-mode dontAsk \
#     --model opus \
#     --name foreman:redis \
#     --add-dir /abs/path/plans \
#     "Read and implement the plan at /abs/path/plans/migration-redis.md. Branch: feat/redis."

tmux new-window -t foreman -n "redis" "bash /abs/path/.foreman/scripts/redis.sh"

# Get claude PID directly from tmux (exec made claude the pane process)
# Query immediately after window creation to avoid race with early exit
tmux list-panes -t foreman:redis -F '#{pane_pid}'

# Log capture for stuck detection
tmux pipe-pane -t foreman:redis -o "cat >> /abs/path/.foreman/logs/redis.log"
```

```
┌─ tmux: foreman ──────────────────────────────────┐
│                                                    │
│  [dashboard] [redis] [postgresql] [redis-review]   │
│                                                    │
│  (currently viewing: redis)                        │
│                                                    │
│  claude> Reading migration-redis.md...             │
│  claude> Modifying cache.py...                     │
│  claude> ...                                       │
│                                                    │
└────────────────────────────────────────────────────┘
```

User switches between agents with `Ctrl+B, n` (next window) or `Ctrl+B, 1/2/3` (by number). Can also split panes to see multiple agents at once.

**Startup**: Foreman checks for existing tmux session (`tmux has-session -t foreman`). If found from a previous crash, it handles recovery (reattach to existing agents or clean up).

### VS Code Extension (foreman-vscode)

A small VS Code extension (~100 lines TypeScript) that gives agents their own VS Code terminal tabs:

```
┌─ VS Code ─────────────────────────────────────────┐
│ Explorer │ Source Control │ ...                     │
├───────────────────────────────────────────────────┤
│                                                    │
│  (editor area)                                     │
│                                                    │
├───────────────────────────────────────────────────┤
│ Terminal: foreman ▾ │ redis │ postgresql │ review  │
│                                                    │
│  claude> Reading migration-redis.md...             │
│  claude> Modifying cache.py...                     │
│                                                    │
└────────────────────────────────────────────────────┘
```

**How it works**:
1. Extension starts a local IPC server (Unix socket at `.foreman/extension.sock`)
2. Foreman Python process sends commands:
   ```json
   {"action": "create_terminal", "name": "redis", "cwd": "...", "command": "claude --append-system-prompt '...' ..."}
   {"action": "kill_terminal", "name": "redis"}
   {"action": "send_text", "name": "redis", "text": "Plan updated. Re-read plans/migration-redis.md"}
   ```
3. Extension calls `vscode.window.createTerminal(...)` — terminals appear as clickable tabs

**Foreman auto-detects which backend to use**: if the extension socket exists, use VS Code terminals. Otherwise, fall back to tmux.

## Agent Spawning

All agents are spawned via **launcher scripts** to avoid shell escaping issues. Each script uses `--append-system-prompt` with `$(cat ...)` for safe prompt loading, `--permission-mode dontAsk` for autonomous operation, and `--add-dir` to grant access to the plans directory.

```python
class Spawner:
    def __init__(self, config: Config):
        if Path(".foreman/extension.sock").exists():
            self.backend = VSCodeBackend()
        else:
            self.backend = TmuxBackend()

    async def spawn_agent(self, name: str, worktree_path: Path,
                          agent_type: str, plan: Plan,
                          config: Config) -> int:
        """Write launcher script, spawn agent, return PID."""
        prompt_path = Path(config.prompts[agent_type]).resolve()
        plans_dir = Path(config.plans_dir).resolve()
        plan_file = plan.file_path.resolve()
        tools = config.allowed_tools.get(agent_type, "")

        # Write launcher script (avoids shell-in-shell escaping)
        script_path = config.scripts_dir / f"{name}.sh"
        script = f"""#!/bin/bash
cd {shlex.quote(str(worktree_path.resolve()))}
exec claude \\
  --append-system-prompt "$(cat {shlex.quote(str(prompt_path))})" \\
  --permission-mode dontAsk \\
  --model {shlex.quote(config.model)} \\
  --name {shlex.quote(f'foreman:{name}')} \\
  --add-dir {shlex.quote(str(plans_dir))} \\
{f'  --allowed-tools {shlex.quote(tools)} \\' + chr(10) if tools else ''}\
  {shlex.quote(f'Read and implement the plan at {plan_file}. Branch: {config.branch_prefix}{plan.name}.')}
"""
        script_path.write_text(script)
        script_path.chmod(0o755)

        await self.backend.create_terminal(
            name=name,
            command=f"bash {shlex.quote(str(script_path))}",
            log_file=config.log_dir / f"{name}.log",
        )
        return await self.backend.get_pid(name)

    async def notify_agent(self, name: str, message: str):
        """Send a message to a running agent (e.g., plan changed)."""
        await self.backend.send_text(name, message)
```

**Why launcher scripts?** Passing `--append-system-prompt` with multi-paragraph text through tmux's `new-window` command creates shell-in-shell escaping hell. A launcher script has one layer of shell interpretation. The `$(cat ...)` loads the prompt cleanly. The initial message is short (just a path reference), not the entire plan content.

**Why `--add-dir`?** Agents run in worktrees (`.foreman/worktrees/redis/`). Plan files live in the main repo (`plans/`). `--add-dir` grants the agent read access to the plans directory so it can read the plan itself. The initial message just points to the plan file path — the agent reads the latest version.

### Implementation Agent

**`--append-system-prompt`** (from `plans/prompt-implementation.md`):
- You are working in a git worktree — commit your work before finishing
- Only modify files within this project directory
- If you hit a problem you can't solve, stop and explain clearly what's blocking you
- When done, ensure all changes are committed to your branch

**Initial message** (short, points to plan file):
- `"Read and implement the plan at /abs/path/plans/migration-redis.md. Branch: feat/redis."`
- Agent reads the plan itself via `--add-dir` access
- Brain can inject context from prior waves into the launcher script

### Review Agent

**`--append-system-prompt`** (from `plans/prompt-review.md`):
- Review the changes on this branch against main
- Check for correctness, bugs, security issues, missed requirements
- When done, write your verdict to `REVIEW_VERDICT.json` in the project root
- Verdict format: `{"verdict": "clean"}`, `{"verdict": "findings", "issues": [...]}`, or `{"verdict": "architectural", "reason": "..."}`

**`--allowed-tools`**: `Read,Glob,Grep,Bash,Write` (Write for the verdict file)

**Initial message**: `"Review the changes on this branch against main. The original plan is at /abs/path/plans/migration-redis.md."`

### Fix Agent

**`--append-system-prompt`** (from `plans/prompt-fix.md`):
- You are fixing issues found during code review
- The existing code on this branch is your starting point
- Commit your fixes before finishing

**Initial message**: `"Fix the issues found during review. Original plan: /abs/path/plans/migration-redis.md. Review findings: [written to launcher script from REVIEW_VERDICT.json]"`

## Review Agents

Reviews are **interactive** — they run in their own tmux window so you can watch the review happen and intervene if needed.

Foreman reads `{worktree_path}/REVIEW_VERDICT.json` after the review process exits.

Review agents do **not** count against `max_parallel_workers`. They have their own limit (`max_parallel_reviews`).

If review finds fixable issues → Foreman spawns a **new** worker in the same worktree with a fix prompt. Max retries (default 2) before escalating to user.

## Isolation Model: Git Worktrees

Parallel agents can't share a single checkout — git only has one branch checked out at a time. Foreman uses **git worktrees** to give each agent its own working directory on its own branch, backed by the same repo.

```bash
git worktree add .foreman/worktrees/redis -b feat/redis
git worktree add .foreman/worktrees/postgresql -b feat/postgresql
```

```
/repo/                                    ← main (user's working copy)
/repo/.foreman/worktrees/redis/           ← feat/redis (agent 1)
/repo/.foreman/worktrees/postgresql/      ← feat/postgresql (agent 2)
```

**Note**: Claude CLI has a built-in `--worktree` flag that creates worktrees automatically. We may be able to use this to simplify `worktree.py`, but we still need custom control over branch names and worktree locations for merge management. To be evaluated during implementation.

**Lifecycle**:
1. Plan is ready → Foreman creates worktree (branching from current main)
2. Agent spawned with `--append-system-prompt` and `--permission-mode dontAsk`
3. Agent works in its worktree directory
4. Agent finishes + review passes → Foreman merges branch into main
5. Worktree is removed (`git worktree remove`)
6. Next agents branch from updated main — they see all prior work in the code

**Merge ordering**: First-finished-first-merged. If a later merge conflicts, the brain handles it.

## Stuck Detection

```python
class StuckDetector:
    """Tracks log file activity per agent. Fires when no activity for threshold."""

    def __init__(self, threshold_seconds: int):
        self.threshold = threshold_seconds
        self.timers: dict[str, asyncio.TimerHandle] = {}

    def on_log_activity(self, plan_name: str):
        """Called on every inotify IN_MODIFY event for the agent's log file."""
        if plan_name in self.timers:
            self.timers[plan_name].cancel()
        loop = asyncio.get_event_loop()
        self.timers[plan_name] = loop.call_later(
            self.threshold, self._mark_stuck, plan_name
        )

    def _mark_stuck(self, plan_name: str):
        """Timer fired — no log activity for threshold seconds."""
        # Update DB, notify dashboard
        ...
```

## Graceful Shutdown

When Foreman receives SIGINT/SIGTERM:

1. Stop accepting new plans
2. Mark all RUNNING plans as INTERRUPTED in coordination DB
3. Send SIGTERM to worker processes (they get a chance to commit)
4. Wait up to 30s for workers to exit
5. Leave worktrees on disk (work may be salvageable)
6. Save brain session ID
7. Leave tmux session alive (user can still interact with agents)

On restart:
- Foreman checks for existing tmux session (`tmux has-session -t foreman`)
- Detects INTERRUPTED plans in the DB
- Detects orphaned worktrees with uncommitted work
- Offers to resume (re-spawn worker in existing worktree) or clean up

## Components

### 1. Foreman Brain (`foreman/brain.py`)
Persistent Claude CLI session via `-p --resume`. Serialized via `asyncio.Lock`. Falls back to fresh session on failure. Handles merge conflicts, understands blocked agents.

### 2. Event Loop (`foreman/loop.py`)
Async Python event loop. `asyncinotify` for filesystem events, `asyncio.TaskGroup` for concurrent watchers.

### 3. Plan Parser (`foreman/plan_parser.py`)
Reads markdown plans, ignores `draft-*`, extracts dependencies from `> **Depends on:**` lines.

### 4. Dependency Resolver (`foreman/resolver.py`)
Builds DAG, computes ready plans, detects circular dependencies.

### 5. Coordination DB (`foreman/coordination.py`)
SQLite (WAL mode). Tracks plan status (QUEUED/RUNNING/REVIEWING/DONE/BLOCKED/FAILED/INTERRUPTED) and agent PIDs.

### 6. Worktree Manager (`foreman/worktree.py`)
Creates/removes git worktrees. May leverage Claude CLI's `--worktree` flag.

### 7. Agent Spawner (`foreman/spawner.py`)
Auto-detects tmux vs VS Code extension. Writes launcher scripts per agent to avoid shell escaping. Uses `--append-system-prompt` (via `$(cat ...)`), `--permission-mode dontAsk`, `--model`, `--name`, `--add-dir`. Uses `exec` for PID tracking, `pipe-pane` for log capture. Queries PID immediately after window creation.

### 8. Progress Monitor (`foreman/monitor.py`)
`asyncinotify` on log files + per-agent `asyncio` timers for stuck detection.

### 9. Config (`foreman/config.py` + `foreman.toml`)

```toml
[foreman]
plans_dir = "plans"
coordination_db = ".foreman/coordination.db"
log_dir = ".foreman/logs"
worktree_dir = ".foreman/worktrees"
scripts_dir = ".foreman/scripts"
branch_prefix = "feat/"

[foreman.prompts]
implementation = "plans/prompt-implementation.md"
review = "plans/prompt-review.md"
fix = "plans/prompt-fix.md"

[foreman.timeouts]
implementation = 1800   # 30 min per plan
review = 600            # 10 min per review
stuck_threshold = 300   # 5 min no log activity = stuck

[foreman.agents]
max_parallel_workers = 3
max_parallel_reviews = 2
model = "opus"
auto_review = true
max_review_retries = 2
permission_mode = "dontAsk"    # auto-approve tool use for agents

[foreman.allowed_tools]
# Empty = all tools allowed (default for implementation/fix)
review = "Read,Glob,Grep,Bash,Write"

[foreman.plans]
# Override per-plan settings
[foreman.plans.migration-celery]
timeout = 3600
```

## CLI Interface

```bash
foreman start              # Start Foreman (enters the event loop)
foreman plan               # Dry run — show execution order
foreman status             # Show running/completed agents
foreman kill migration-redis  # Kill a stuck agent
foreman reset              # Reset DB and clean up worktrees
```

Built with **cyclopts**.

## Implementation Plan

### Phase 1: Core (MVP) ✓
- [x] `plan_parser.py` — parse markdown plans, ignore `draft-*` files
- [x] `resolver.py` — build DAG, determine ready plans
- [x] `coordination.py` — SQLite status tracking + PID management
- [x] `worktree.py` — create/remove git worktrees
- [x] `spawner.py` — launcher script generation + tmux backend (`pipe-pane`, `--add-dir`)
- [x] `monitor.py` — asyncinotify on log files + per-agent stuck timers
- [x] `brain.py` — persistent Claude CLI session via `--resume`, asyncio.Lock, error recovery
- [x] `loop.py` — async event loop tying everything together
- [x] `cli.py` — `foreman start`, `foreman plan`, `foreman status`, `foreman kill`, `foreman reset`
- [x] `config.py` — `foreman.toml` loading
- [x] Agent prompt files (implementation, review, fix)
- [x] Graceful shutdown (startup recovery not yet implemented)

### Phase 2: Self-Review Loop ✓
- [x] Review agent (own tmux window, writes REVIEW_VERDICT.json)
- [x] Fix agent re-spawn on findings
- [x] BLOCKED escalation on architectural problems
- [x] Max retry count (default 2)
- [x] Review slot management (`max_parallel_reviews` enforced separately from workers)
- [x] ReviewVerdict enum for verdict parsing

### Phase 3: Intelligent Merge ✓
- [x] Brain reads diffs + plans for conflict resolution
- [x] First-finished-first-merged ordering (inherent — first done triggers merge)
- [x] Escalation to user for complex conflicts (BLOCKED + reason)

### Phase 4: VS Code Extension (foreman-vscode) ✓
- [x] TypeScript extension with IPC server (Unix socket at `.foreman/extension.sock`)
- [x] `create_terminal` / `kill_terminal` / `send_text` commands
- [x] Spawner auto-detects extension socket, falls back to tmux (implemented in Phase 1)
- [x] Auto-activates when `.foreman/` directory exists

### Phase 5: Dashboard & Polish
- [ ] Rich TUI dashboard with separate worker/review slot counts
- [ ] Brain session summarization

### Phase 6: Interactive Mode
- [ ] Pause/resume agents
- [ ] Inject guidance mid-execution

## Tech Stack

- **Python 3.12** — async event loop, type hints
- **Claude Code CLI v2.1+** — brain (`-p --resume`), agents (`--append-system-prompt --permission-mode dontAsk`)
- **SQLite** — coordination DB (WAL mode)
- **git worktrees** — filesystem isolation
- **asyncio** — event loop, subprocess management, timers
- **asyncinotify** — native async inotify (no threads)
- **tmux** — agent display (MVP)
- **tomllib** — config parsing (stdlib)
- **rich** — dashboard TUI
- **cyclopts** — CLI framework
- **TypeScript** — VS Code extension (Phase 4)

No API keys needed. Max subscription via Claude Code CLI.

## Key Design Decisions

### Why `--append-system-prompt` instead of CLAUDE.md or `--system-prompt`?
`--append-system-prompt` **adds** to Claude Code's default system prompt instead of replacing it. Agents keep all built-in capabilities (file editing, code understanding, etc.) while also getting Foreman-specific instructions. No file manipulation needed. Verified working in interactive mode.

### Why `--permission-mode dontAsk`?
Agents need to work autonomously — they can't stop and ask for permission on every file edit. `--permission-mode dontAsk` auto-approves all tool use. Note: this does not enforce directory restrictions — the agent *could* edit files outside its worktree. The system prompt instructs it not to, and the worktree CWD naturally scopes most operations. For additional safety, `acceptEdits` is an alternative that auto-approves file edits but may prompt for some Bash commands.

### Why launcher scripts instead of inline commands?
Passing `--append-system-prompt` with multi-paragraph text through tmux `new-window` creates shell-in-shell escaping that will break on quotes, backticks, and `$` in the prompt. Launcher scripts have one layer of shell interpretation. `$(cat prompt.md)` loads prompts cleanly. The initial message is short (just a file path), not the entire plan content.

### Why `--add-dir` for plan access?
Agents run in worktrees (`.foreman/worktrees/redis/`). Plan files live in the main repo (`plans/`). Without `--add-dir`, the agent can't read its own plan. `--add-dir plans/` grants access. The initial message points to the plan file — the agent reads the latest version itself.

### Why event-driven, not polling?
`asyncinotify` (native async inotify) for filesystem events, `asyncio` for process lifecycle. No threads, no polling. Brain invoked only when reasoning is needed.

### Why a persistent Claude session for the brain?
`--resume` with a saved session ID gives the brain full conversational memory across invocations and restarts. All invocations serialized via `asyncio.Lock`. Error handling falls back to fresh session.

### Why tmux for MVP, VS Code extension later?
tmux gives full programmatic control: create/kill windows, `send-keys` for notifications, `pipe-pane` for log capture, `exec` for PID tracking. VS Code extension (Phase 4) adds native tab integration.

### Why separate limits for workers and reviews?
Prevents reviews from being starved when all worker slots are taken.

### Why interactive reviews?
User can watch the review and intervene. Agent writes `REVIEW_VERDICT.json` in worktree root for Foreman to parse.

### Why git worktrees?
Full filesystem isolation. No file locking needed. Conflicts handled at merge time by the brain.

### How agents learn about prior work
Foreman merges completed work into main before spawning new agents. New agents branch from updated main.

## Limitations (v1)

- No cross-repo orchestration (one repo at a time)
- No remote execution (agents run locally)
- No plan generation (plans are authored manually, with Claude's help)
- Plans must include dependency metadata or they're treated as having no dependencies
- Brain session can grow large over many waves (periodic summarization mitigates)
- Linux only (asyncinotify requires inotify; tmux required for MVP)
