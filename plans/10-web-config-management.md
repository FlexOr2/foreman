# Web UI Config Management

Allow editing foreman's configuration via the web dashboard. Changes should take effect without manually restarting foreman.

## What to add

### Config panel in web UI

A settings section in the web dashboard showing all current config values with editable fields:

- Workers: number input (max_parallel_workers)
- Reviews: number input (max_parallel_reviews)
- Model: dropdown (opus/sonnet/haiku)
- Innovator: toggle (enabled), interval, max_drafts, categories checkboxes
- Timeouts: number inputs for implementation/review/stuck_threshold
- Skip review: toggle
- Auto activate: toggle
- Auto restart: toggle

### Save and apply

When user clicks "Save":
1. Write updated values to `.foreman/config.toml`
2. Signal foreman to reload config (write a marker file `.foreman/reload_config`)
3. Foreman's event loop checks for the marker, reloads config, applies changes

### Hot reload in foreman

Add a file watcher on `.foreman/config.toml` or check for `.foreman/reload_config` marker in the watchdog loop. On change:
- Reload config from disk
- Update scheduler limits (worker/review counts take effect on next cycle)
- Update innovator settings (interval, categories, enabled)
- Update timeouts
- Log the config change

No restart needed for most settings. Model changes take effect on next agent spawn.

## Files to modify

- `foreman/web.py` — add config panel with form, save endpoint
- `foreman/watchdog.py` or `foreman/loop.py` — add config hot-reload detection
- `foreman/config.py` — add `save_config()` function that writes back to TOML
