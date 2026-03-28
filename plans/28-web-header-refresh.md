<!-- foreman:innovator -->
# Web dashboard header goes stale — process status never updates after page load

> **Depends on:**

## Problem

In `web.py:414-415`, the HTMX polling replaces only the `#main-content` div:

```python
'<div id="main-content" hx-get="/state" hx-trigger="every 2s" hx-swap="outerHTML">'
```

The `_render_header` (lines 365-397) — which shows foreman alive/dead, observer alive/dead, worker/review counts, and draft count — is rendered only in `_page()` (line 445) during full page load. The `/state` endpoint (lines 496-498) returns `_render_main(config)` which does NOT include the header.

This means:
- If foreman crashes, the green "running" dot stays green indefinitely
- Worker/review counts freeze at the values when the page loaded
- Draft count never updates

The header is the primary status indicator, yet it's the only part of the page that doesn't auto-refresh.

## Solution

Add HTMX polling to the header. Give the header a dedicated endpoint and an `hx-get` trigger:

In `_render_header`, wrap the output in a div with HTMX attributes:

```python
return (
    f'<div id="header-content" hx-get="/header" hx-trigger="every 3s" hx-swap="outerHTML">'
    f'<header>...'
    f'</header>'
    f'</div>'
)
```

Add a `/header` endpoint to the FastAPI app:

```python
@app.get("/header", response_class=HTMLResponse)
async def header() -> str:
    db = CoordinationDB(config.coordination_db) if config.coordination_db.exists() else None
    try:
        return _render_header(config, db)
    finally:
        if db:
            db.close()
```

Use a slightly longer interval (3s) than the main content (2s) to stagger DB access.

## Scope

- `foreman/web.py` — add HTMX `hx-get`/`hx-trigger` to header output, add `/header` endpoint

## Risk Assessment

Low risk. Adds one more lightweight endpoint that reads 2 DB queries (RUNNING count, REVIEWING count) and 2 PID file checks. The staggered 3s interval prevents both polls from hitting simultaneously. No change to existing functionality.
