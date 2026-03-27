# Foreman — Architecture

## What it does

Foreman automates the workflow a human developer does manually when orchestrating multiple Claude Code agents across a codebase:

```
Plans → Review → Order → Parallel Execution → Self-Review → Commit → Repeat
```

You point Foreman at a repo with plan files. It figures out what to do, spawns Claude Code agents to do it, coordinates their file access, reviews their work, and commits the results. You watch in VS Code and intervene when needed.

## Core Loop

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  1. PLAN REVIEW                                              │
│     Agent reads all plans/ files                             │
│     Checks for gaps, contradictions, missing steps           │
│     Updates plans in-place                                   │
│                                                              │
│  2. DEPENDENCY ANALYSIS                                      │
│     Reads plan metadata (depends_on, files_owned)            │
│     Builds DAG of plan dependencies                          │
│     Determines which plans can run in parallel               │
│                                                              │
│  3. EXECUTION (parallel where possible)                      │
│     For each ready plan:                                     │
│       a. Claim files via coordination DB                     │
│       b. Create feature branch                               │
│       c. Spawn claude CLI with implementation prompt          │
│       d. Monitor progress (poll git branch for commits)      │
│                                                              │
│  4. SELF-REVIEW                                              │
│     When agent finishes:                                     │
│       a. Spawn review agent on the diff                      │
│       b. If findings: agent fixes and re-reviews             │
│       c. If clean: mark plan as done                         │
│                                                              │
│  5. MERGE                                                    │
│     Release file claims                                      │
│     Create PR or merge to main (configurable)                │
│     Notify user                                              │
│                                                              │
│  6. REPEAT                                                   │
│     Check if new plans are now unblocked                     │
│     Go to step 3 with newly eligible plans                   │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## Components

### 1. Plan Parser (`foreman/plan_parser.py`)

Reads plan files and extracts structured metadata.

**Input**: A directory of markdown plan files (e.g., `plans/migration-*.md`).

**Extracts**:
- Plan name and status (from `> **Status:**` lines)
- Dependencies (from `> **Depends on:**` lines)
- File ownership (from the plan body — files the plan will modify)
- Phases/steps (from `### Phase N:` headings)
- Effort estimate (from overview table)

**Output**: `list[Plan]` with dependency graph.

**Convention**: Plans must include this frontmatter-style metadata:
```markdown
> **Status: NOT STARTED**
> **Depends on: Phase 0, Redis migration**
> **Files owned: middleware.py, server.py:130-156**
```

Plans that don't have this metadata are skipped with a warning.

### 2. Dependency Resolver (`foreman/resolver.py`)

Builds a DAG from plan dependencies and determines execution waves.

```python
@dataclass
class ExecutionWave:
    plans: list[Plan]       # can run in parallel
    depends_on: list[str]   # must complete before this wave starts

# Example output for songmaker migrations:
# Wave 0: [Phase 0]
# Wave 1: [Redis, PostgreSQL]  (parallel)
# Wave 2: [Celery]
# Wave 3: [Object Storage, Sessions/Auth]  (parallel)
```

Handles:
- Circular dependency detection (error)
- Plans with `status: DONE` are skipped
- Plans with `status: BLOCKED` are skipped with warning
- Human-only plans (flagged in metadata) are skipped with notification

### 3. Coordination DB (`foreman/coordination.py`)

SQLite database for file-level ownership tracking. Same as `scripts/coordinate.py` in songmaker but as a Python module.

**Schema**:
```sql
claims(plan TEXT, file TEXT, claimed_at TEXT, PRIMARY KEY(plan, file))
plans(plan TEXT PK, status TEXT, branch TEXT, started_at TEXT, updated_at TEXT)
agents(plan TEXT PK, pid INTEGER, log_file TEXT, started_at TEXT)
```

The `agents` table is new — tracks running Claude CLI processes so Foreman can monitor them.

**Location**: `{target_repo}/.foreman/coordination.db` (gitignored).

### 4. Agent Spawner (`foreman/spawner.py`)

Launches Claude Code CLI instances with the right prompts.

**For implementation**:
```bash
claude --print \
  --system-prompt "$(cat plans/prompt-implementation.md)" \
  --prompt "Implement plans/migration-redis.md" \
  --allowedTools "Read,Write,Edit,Glob,Grep,Bash" \
  2>&1 | tee .foreman/logs/migration-redis.log
```

**For review**:
```bash
claude --print \
  --system-prompt "$(cat plans/prompt-rereview-implementation.md)" \
  --prompt "Review the diff on branch feat/migration-redis" \
  --allowedTools "Read,Glob,Grep,Bash,Edit,Write" \
  2>&1 | tee .foreman/logs/migration-redis-review.log
```

**VS Code integration**: Instead of `claude --print`, spawn `claude` in interactive mode inside VS Code terminals so the user can watch and intervene:
```python
subprocess.Popen(
    ["code", "--command", "workbench.action.terminal.new"],
    # Then send the claude command to that terminal
)
```

Alternative: use VS Code tasks (`tasks.json`) to define named terminals per agent, then launch them programmatically.

**Process management**:
- Track PIDs in coordination DB
- Monitor via `os.waitpid(pid, os.WNOHANG)` polling
- Detect completion by: process exit + branch has new commits
- Timeout: configurable per plan (default 30 min)

### 5. Progress Monitor (`foreman/monitor.py`)

Watches running agents and reports status.

**Signals an agent is done**:
- Claude CLI process exits with code 0
- Feature branch has commits newer than spawn time
- Coordination DB status updated to "done"

**Signals an agent is stuck**:
- No git activity for N minutes (configurable)
- Process still running past timeout
- Action: notify user, optionally kill and retry

**Dashboard** (terminal UI):
```
┌─ Foreman ──────────────────────────────────────┐
│ Wave 1 (2/2 active)                            │
│  ✓ migration-redis     [done]     3m 42s       │
│  ⟳ migration-postgresql [phase-2]  2m 15s      │
│                                                 │
│ Wave 2 (blocked — waiting on Wave 1)            │
│  ◯ migration-celery    [queued]                 │
│                                                 │
│ Reviews                                         │
│  ⟳ migration-redis     [reviewing]  0m 30s     │
└─────────────────────────────────────────────────┘
```

### 6. Config (`foreman.toml`)

Per-project config file in the target repo root:

```toml
[foreman]
plans_dir = "plans"
prompts = {
    implement = "plans/prompt-implementation.md",
    review = "plans/prompt-rereview-implementation.md",
    architecture_review = "plans/prompt-architecture-review.md",
}
coordination_db = ".foreman/coordination.db"
log_dir = ".foreman/logs"
branch_prefix = "feat/"

[foreman.timeouts]
implementation = 1800   # 30 min per plan
review = 600            # 10 min per review
stuck_threshold = 300   # 5 min no activity = stuck

[foreman.agents]
max_parallel = 3
model = "opus"          # claude model for agents
auto_merge = false      # true = merge to main automatically
auto_review = true      # true = spawn review agent after implementation

[foreman.plans]
# Override per-plan settings
[foreman.plans.migration-celery]
human_only = true       # skip automatic execution, notify user
timeout = 3600          # 60 min (complex plan)
```

## CLI Interface

```bash
# Run the full pipeline
foreman run

# Run specific plans only
foreman run migration-redis migration-postgresql

# Just analyze plans and show execution order (dry run)
foreman plan

# Show status of running/completed agents
foreman status

# Review a specific branch
foreman review feat/migration-redis

# Kill a stuck agent
foreman kill migration-redis

# Reset coordination DB (clear all claims)
foreman reset
```

## Implementation Plan

### Phase 1: Core (MVP)
- [ ] `plan_parser.py` — extract metadata from markdown plans
- [ ] `resolver.py` — build DAG, compute execution waves
- [ ] `coordination.py` — SQLite claim/release (port from songmaker)
- [ ] `spawner.py` — launch `claude --print` with prompts
- [ ] `monitor.py` — poll processes, detect completion
- [ ] `cli.py` — `foreman run` and `foreman plan` commands
- [ ] `foreman.toml` — config loading

### Phase 2: Self-Review Loop
- [ ] Auto-spawn review agent when implementation agent finishes
- [ ] Parse review output for findings
- [ ] If findings: re-spawn implementation agent with "fix these issues" prompt
- [ ] Max retry count (default 2) before escalating to user

### Phase 3: VS Code Integration
- [ ] Spawn agents in VS Code terminal tabs (named per plan)
- [ ] VS Code task definitions for common operations
- [ ] Status bar extension showing Foreman progress (optional, stretch goal)

### Phase 4: Interactive Mode
- [ ] User can pause/resume agents
- [ ] User can inject guidance mid-execution ("focus on X first")
- [ ] User can add new plans while agents are running
- [ ] Chat interface for discussing progress with Foreman

## Tech Stack

- **Python 3.12** — same as songmaker, no new runtime
- **SQLite** — coordination DB (already proven in songmaker)
- **subprocess** — spawn Claude CLI instances
- **tomllib** — config parsing (stdlib in 3.11+)
- **rich** — terminal dashboard (optional, for Phase 3)
- **Click or cyclopts** — CLI framework

No external AI SDKs needed. Foreman is a process orchestrator that happens to spawn Claude CLI instances. The intelligence is in Claude, not in Foreman.

## Key Design Decisions

### Why not the Claude Agent SDK?
The Agent SDK is for building agents that call tools inside a single process. Foreman needs to spawn **separate Claude CLI processes** that the user can see and interact with in VS Code. The SDK would hide the agent conversations from the user, which defeats the purpose.

### Why Claude CLI, not the API?
- User can watch agents work in real time (VS Code terminals)
- User can intervene, type messages, redirect agents
- Claude CLI handles tool execution, permissions, context management
- No API key management in Foreman itself
- Agents get full Claude Code capabilities (file editing, bash, etc.)

### Why SQLite for coordination?
- Already proven in the songmaker coordination script
- WAL mode handles concurrent reads/writes from multiple processes
- No external dependencies (Redis, etc.)
- Survives process crashes (file-based)
- Inspectable with any SQLite client

### Why not git-based coordination?
Git branches provide isolation but not mutual exclusion. Two agents can both create commits on different branches that touch the same file — the conflict only surfaces at merge time. The coordination DB prevents this upfront by refusing to claim already-owned files.

## Limitations (v1)

- No cross-repo orchestration (one repo at a time)
- No remote execution (agents run locally)
- No automatic conflict resolution (escalates to user)
- No cost tracking (Claude CLI doesn't expose token usage easily)
- Plans must follow the metadata convention or they're skipped
