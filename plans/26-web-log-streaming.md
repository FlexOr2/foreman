<!-- foreman:innovator -->
# Web _render_logs reads entire log file into memory on every 2-second poll

> **Depends on:**

## Problem

In `web.py:296-334`, `_render_logs` reads the entire log file into memory on every call:

```python
lines_raw = config.log_file.read_text(encoding="utf-8").splitlines()
```

This runs every 2 seconds via the HTMX `/state` poll. The log file uses `RotatingFileHandler` with `LOG_FILE_MAX_BYTES = 5 * 1024 * 1024` (5MB, cli.py:53). Reading, splitting into lines (~50K+ lines), then reversing and JSON-parsing each line to find the last 25 warnings/errors is extremely wasteful for a 2-second polling interval.

## Solution

Read only the tail of the file instead of the entire contents. Use `seek` to read the last N bytes (enough to contain 25 warning/error entries — 64KB is generous):

```python
def _render_logs(config: Config, n: int = 25) -> str:
    if not config.log_file.exists():
        return '<div class="card"><div class="empty">No log file yet.</div></div>'

    tail_bytes = 65536
    with open(config.log_file, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - tail_bytes))
        raw = f.read().decode("utf-8", errors="replace")

    entries = []
    for line in reversed(raw.splitlines()):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("level") in ("ERROR", "WARNING"):
            entries.append(e)
        if len(entries) >= n:
            break
    ...
```

This reads at most 64KB regardless of log file size, down from up to 5MB.

## Scope

- `foreman/web.py` — replace `read_text().splitlines()` with seek-based tail read in `_render_logs`

## Risk Assessment

Very low risk. The only behavioral difference is that if the last 64KB of the log doesn't contain 25 warnings/errors, fewer entries are shown. This is acceptable — users care about recent errors, not ones from megabytes ago. If needed, `tail_bytes` can be increased.
