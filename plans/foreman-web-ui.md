# Foreman Web UI

The CLI is hard to use for monitoring and managing foreman. You need to remember commands, can't see everything at once, and logs are buried in files. Build a web dashboard that shows foreman's full state at a glance and lets the user interact.

## What the user needs to see

- Foreman process status (alive/dead, uptime, observer status)
- Innovator activity (what it's exploring, which ideas it shaped, draft count)
- All plans with status, agent type, branch, time since last update, blocked reason
- Recent git log (what foreman merged)
- Recent errors and warnings from structured logs
- Worker/review slot usage

## What the user needs to do

- Activate draft plans (remove draft- prefix)
- Reject/delete draft plans
- Pause/resume/kill/unblock agents
- Send guidance to running agents
- View plan file contents
- Trigger an innovator cycle manually

## Constraints

- `foreman web` CLI command starts the dashboard on a configurable port
- Must work alongside `foreman start` (reads same DB and state files)
- Keep dependencies minimal
- The UI should auto-refresh — no manual page reloads
- Single page — everything visible without navigation

## Implementation

Choose the simplest approach that works well. Consider:
- FastAPI + HTMX (no JS build step, server-rendered, auto-refresh via polling)
- Textual (terminal TUI, no browser needed, Rich already a dependency)
- Any other approach that stays simple

Make it look good. This is what the user stares at all day.
