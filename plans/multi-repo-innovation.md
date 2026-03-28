# Multi-Repo Innovation

Currently foreman only analyzes and improves its own codebase. Allow the innovator to propose entirely new tools and projects — not just improvements to foreman.

## What to do

### 1. Add a "create" category to the innovator

New category alongside risk/performance/etc:

- `"create"` — "What new tool, library, or project would solve a problem you see in this codebase's ecosystem? What's missing from the developer's workflow? What tool would you build if you could start from scratch?"

### 2. New project plans get their own repo

When the innovator generates a plan for a new project (not a foreman improvement), the plan should include:
- A project name
- A one-paragraph description
- A basic file structure
- The implementation plan

The implementation agent creates a new directory at the repo root (e.g., `projects/new-tool-name/`) with its own structure, or optionally a new git repo if configured.

### 3. Config

```toml
[foreman.innovate]
categories = ["risk", "performance", "architecture", "create"]
project_dir = "projects"    # where new project ideas get created
```

## Files to modify

- `foreman/innovate.py` — add "create" category with appropriate provocation questions
- `foreman/config.py` — add `project_dir` to InnovateConfig
