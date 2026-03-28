# Plan Priority from Web UI

The scheduler picks plans in arbitrary order (alphabetical/insertion order). The user should be able to reorder the queue from the web dashboard.

## What to add

### 1. Priority field in DB

Add a `priority` INTEGER column to the plans table (default 0, higher = runs first). The scheduler orders QUEUED plans by priority DESC, then by name.

### 2. Web UI controls

On each QUEUED plan row:
- Up/down arrows to change priority
- Or a "Run next" button that sets priority to max+1

### 3. Drag and drop (optional)

If simple to implement with HTMX, allow drag-and-drop reordering of the QUEUED plans list. Each reorder updates priorities via a POST endpoint.

## Files to modify

- `foreman/coordination.py` — add priority column, update get_all_plans to order by priority
- `foreman/scheduler.py` — respect priority when picking next plan
- `foreman/web.py` — add priority controls to plan rows, endpoint to update priority
