# Multi-Repo Innovation

The innovator should be able to propose entirely new tools and projects — not just improvements to foreman.

## What to do

### 1. Add a "create" category to the innovator

New category alongside risk/performance/etc:

- `"create"` — "What new tool, library, or project would solve a problem you see in this codebase's ecosystem? What's missing from the developer's workflow? What tool would you build if you could start from scratch? Think beyond this project."

### 2. New project plans are self-contained blueprints

When the innovator generates a "create" idea, it writes a plan file that contains everything needed to bootstrap a new repo:

- Project name and description
- Why it should exist (problem it solves)
- Tech stack
- File structure
- Implementation plan with phases
- A CLAUDE.md for the new project

These plans are written to `plans/draft-create-{name}.md` (always draft, never auto-activated — new projects need human review).

The human then:
1. Reviews the plan
2. Creates a new repo: `mkdir ~/git/new-tool && cd ~/git/new-tool && git init`
3. Copies the plan: `cp ~/git/foreman/plans/draft-create-new-tool.md plans/bootstrap.md`
4. Runs foreman there: `uv run foreman init && uv run foreman start`

Foreman in the new repo implements the blueprint from scratch.

### 3. Config

```toml
[foreman.innovate]
categories = ["risk", "performance", "architecture", "create"]
```

No `project_dir` needed — create plans stay as draft files in the current repo's plans/.

## Files to modify

- `foreman/innovate.py` — add "create" category with provocation questions, force draft- prefix for create plans regardless of auto_activate setting
- `foreman/config.py` — no changes needed (categories already configurable)
