# Plan: `foreman analyze` Command

> **Depends on:**

## Summary

Add a `foreman analyze` command that uses Claude to analyze the target codebase with a specific focus area, then generates `draft-*` plan files ready for human review and approval.

## How It Works

```
foreman analyze --focus security
  → Brain reads codebase structure
  → Brain analyzes with security lens
  → Brain generates 0-N draft plans
  → Plans written to plans/draft-*.md
  → User reviews, renames to approve, deletes to reject
  → Foreman orchestrator picks up approved plans automatically
```

## Focus Areas

| Focus | What it looks for |
|---|---|
| `security` | OWASP top 10, auth issues, injection, secrets in code, dependency CVEs |
| `performance` | N+1 queries, blocking I/O, missing indexes, hot path bloat, memory leaks |
| `debt` | Code smells, dead code, duplicated logic, outdated patterns, missing tests |
| `deps` | Outdated dependencies, known CVEs, abandoned packages, license issues |
| `architecture` | Coupling, circular deps, abstraction violations, missing separation of concerns |
| `testing` | Missing test coverage, flaky tests, untested edge cases, integration gaps |

User can also pass a free-form focus: `foreman analyze --focus "error handling and resilience"`

## CLI Interface

```bash
# Analyze with a predefined focus
foreman analyze --focus security

# Free-form focus
foreman analyze --focus "migrate from REST to GraphQL"

# Analyze with web search enabled (for deps, CVEs, best practices)
foreman analyze --focus deps --web

# Limit scope to specific directories
foreman analyze --focus performance --path src/api/

# Dry run — show what would be analyzed, don't generate plans
foreman analyze --focus debt --dry-run
```

## Implementation

### New module: `foreman/analyze.py`

Uses the Foreman brain (persistent Claude session) to:

1. **Gather context**: Read directory structure, key files (README, pyproject.toml, CLAUDE.md), sample source files. For `--path` scope, focus on that subtree.

2. **Analyze**: Send a focused prompt to the brain with the codebase context and the focus area. The prompt instructs the brain to identify concrete, actionable improvements — not vague suggestions.

3. **Generate plans**: For each finding, the brain writes a `draft-*.md` plan file with:
   - Clear problem statement
   - Proposed solution
   - `> **Depends on:**` metadata if relevant
   - Estimated scope (which files would change)

4. **Report**: Print a summary of generated draft plans to the console.

### Brain prompt structure

```
You are analyzing a codebase for {focus} improvements.

Codebase structure:
{tree output}

Key files:
{contents of config files, entry points, etc.}

Rules:
- Only suggest improvements you are confident about
- Each suggestion must be concrete and actionable (not "consider adding tests")
- Each suggestion must be a self-contained plan that an implementation agent can execute
- Prefer small, focused plans over large refactoring plans
- Do not suggest changes that require business context you don't have
- If using --web, search for known CVEs, latest versions, best practices

For each improvement, output a markdown plan file with this format:
[plan template]
```

### Integration with existing Foreman

- Plans are written as `draft-*` files → Foreman's existing `is_plan_file()` ignores them
- User reviews drafts, renames approved ones → watchdog picks them up
- Zero changes needed to the orchestrator loop

### Config

```toml
[foreman.analyze]
max_plans = 10              # max draft plans per analysis run
include_patterns = ["*.py", "*.ts", "*.js"]  # file types to analyze
exclude_patterns = [".venv", "node_modules", "dist"]
```

## Changes Required

- [ ] New `foreman/analyze.py` module
- [ ] New `analyze` command in `cli.py`
- [ ] Analysis prompt templates (per focus area or generic)
- [ ] Config additions for analyze settings
- [ ] Codebase context gathering (tree + file sampling)

## Open Questions

- Should `--web` use Claude's built-in web search, or a separate tool?
- Should analysis be incremental (remember previous findings) or fresh each time?
- Should there be a `foreman analyze --review` that runs the adversarial review on existing draft plans?
