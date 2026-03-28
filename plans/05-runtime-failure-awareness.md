# Runtime Failure Awareness

The innovator only analyzes code statically. It never sees runtime failures — the actual bugs that break foreman in production. This is why it generates plans for code style issues while missing critical bugs like "watchdog kills all agents because has_terminal uses a nonexistent tmux command."

## What to do

Before the innovator's exploration phase, scan the structured log file (`.foreman/foreman.log`) for recent runtime failures and include them as context in the brain prompt.

### Implementation

Add a function in `innovate.py` that reads recent log entries and extracts:
- ERROR level entries (last 24 hours)
- WARNING entries with "orphan", "stuck", "timeout", "failed", "crash" keywords
- Patterns: rapid spawn-fail cycles (same plan failing N times in M minutes)
- Plans that went BLOCKED or FAILED with their reasons from the DB

Format as a "Recent Runtime Issues" section appended to the exploration prompt:

```
## Recent Runtime Issues (from logs)

Errors (last 24h):
- 10:11:26 Hard timeout fired for foreman-web-ui
- 09:22:33 Orphaned plan foreman-web-ui — agent window gone
- 09:13:20 Orphaned plan foreman-web-ui — agent window gone (3 times in 10 min = rapid failure)

Failed/Blocked plans:
- foreman-web-ui: FAILED — Agent exceeded hard timeout
- innovator-plan-count: BLOCKED — Max review retries exceeded

These are REAL problems that happened at runtime. Prioritize fixing these over code style issues.
```

### Also include in the exploration prompt

Tell the brain explicitly: "Runtime failures from the logs are higher priority than static code analysis findings. A bug that crashes the system is more important than a private method being called from the wrong module."

## Files to modify

- `foreman/innovate.py` — add `_build_runtime_context()`, include in exploration prompt
