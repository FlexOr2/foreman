# Foreman

AI agent orchestrator for parallel Claude Code execution. You write plans, Foreman spawns workers, reviews results, resolves merge conflicts, and commits — all in visible tmux windows you can watch and intervene in.

## Install

```bash
# System dependencies (one-time)
sudo apt install tmux git           # Ubuntu/Debian
# sudo pacman -S tmux git           # Arch
# brew install tmux git             # macOS

# Claude Code CLI (if not using VS Code extension)
npm install -g @anthropic-ai/claude-code

# Foreman itself
pip install -e .
```

Foreman auto-discovers the Claude CLI from the VS Code extension, `~/.claude/local/`, or PATH — no manual PATH setup needed.

## Quickstart

```bash
cd your-repo

# Initialize — creates plans/, prompt templates, foreman.toml
foreman init

# Write a plan
cat > plans/add-logging.md << 'EOF'
# Add Logging

Add structured logging to all API endpoints using the `logging` module.
EOF

# Dry run — see execution order
foreman plan

# Start — spawns agents, shows live dashboard
foreman start
```

Watch agents work: `tmux attach -t foreman`

## How it works

1. You drop markdown plan files into `plans/`
2. Foreman parses dependencies (`> **Depends on: other-plan**`), builds a DAG
3. Each plan gets a git worktree + a Claude Code agent in its own tmux window
4. On completion: auto-review, fix cycle, then merge to main
5. Merge conflicts are resolved by a persistent "brain" Claude session
6. New plans can be added while agents are running — Foreman picks them up via inotify

## Commands

```bash
foreman init                    # Set up repo for Foreman
foreman start                   # Event loop + live dashboard
foreman plan                    # Dry run — show execution order
foreman status                  # One-shot status table

foreman pause <plan>            # Pause agent, keep worktree
foreman resume <plan>           # Resume in existing worktree
foreman guide <plan> "message"  # Send guidance to running agent

foreman kill <plan>             # Hard kill agent
foreman reset                   # Clean up everything
```

## Configuration

`foreman.toml` (created by `foreman init`):

```toml
[foreman]
# plans_dir = "plans"
# branch_prefix = "feat/"

[foreman.agents]
# max_parallel_workers = 3
# max_parallel_reviews = 2
# model = "opus"
# permission_mode = "dontAsk"

[foreman.timeouts]
# implementation = 1800    # 30 min per plan
# review = 600             # 10 min per review
# stuck_threshold = 300    # 5 min no activity = stuck
```

## Plan format

```markdown
# My Feature

> **Depends on: other-plan, setup-plan**

Description of what to implement...

### Phase 1
...

### Phase 2
...
```

- Filename = plan name (e.g., `add-logging.md` -> plan `add-logging`)
- `draft-*.md` files are ignored — rename to activate
- Dependencies reference other plan names (filename without `.md`)
- Plans without dependencies run immediately

## VS Code Extension

For VS Code terminal tabs instead of tmux, install the extension from `foreman-vscode/`:

```bash
cd foreman-vscode && npm install && npm run compile
```

The extension auto-activates when `.foreman/` exists and Foreman automatically detects it.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.
