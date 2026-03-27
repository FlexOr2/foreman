# Foreman

AI agent orchestrator for parallel Claude Code execution. You write plans, Foreman spawns workers in git worktrees, reviews results, resolves merge conflicts, and commits — all in visible tmux windows you can watch and intervene in.

## Install

```bash
# System dependencies
sudo apt install tmux git           # Ubuntu/Debian
# sudo pacman -S tmux git           # Arch
# brew install tmux git             # macOS

# Foreman
uv pip install -e .
```

Claude Code CLI is auto-discovered from the VS Code extension, `~/.claude/local/`, or PATH.

## Quickstart

```bash
cd your-repo
foreman init              # Creates plans/, .foreman/ (prompts, config)

cat > plans/add-logging.md << 'EOF'
# Add Logging
Add structured logging to all API endpoints.
EOF

foreman plan              # Dry run — shows execution order
foreman start             # Spawns agents, shows live dashboard
```

Watch agents: `tmux attach -t foreman`

## Commands

```
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

`.foreman/config.toml` (created by `foreman init`):

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
# implementation = 1800        # 30 min per plan
# review = 600                 # 10 min per review
# stuck_threshold = 300        # 5 min no activity = stuck
```

## Plan Format

```markdown
# My Feature

> **Depends on: other-plan, setup-plan**

Description of what to implement...
```

- Filename = plan name (`add-logging.md` → plan `add-logging`)
- `draft-*.md` files are ignored — rename to activate
- Dependencies reference other plan names (filename without `.md`)
- No dependencies = runs immediately

## How It Works

1. Plans are parsed and dependencies resolved into a DAG
2. Each ready plan gets a git worktree on its own branch
3. A Claude Code agent is spawned in a tmux window per plan
4. On completion: auto-review via a separate review agent
5. Review passes → merge to main; findings → fix agent → re-review
6. Merge conflicts → brain (persistent Claude session) resolves or escalates
7. New plans can be dropped in at any time — Foreman picks them up via inotify

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.
