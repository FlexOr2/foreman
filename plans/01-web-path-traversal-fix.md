<!-- foreman:innovator -->
# Web dashboard plan file endpoints are vulnerable to path traversal

> **Depends on:**

## Problem

In `web.py:500-506`, the `/plans/{plan_name}/file-content` endpoint constructs a file path directly from user input:

```python
@app.get("/plans/{plan_name}/file-content", response_class=HTMLResponse)
async def plan_file_content(plan_name: str) -> str:
    path = config.plans_dir / f"{plan_name}.md"
    if not path.exists():
        return "<span class=\"dim\">File not found.</span>"
    text = path.read_text(encoding="utf-8")
```

A request to `/plans/..%2F..%2F..%2Fetc%2Fpasswd/file-content` resolves to `plans/../../../etc/passwd.md`. While the `.md` suffix limits the attack surface, any `.md` file on the system is readable. The same issue affects draft endpoints (`/drafts/{name}/activate` at line 598, `/drafts/{name}/reject` at line 606) which can delete or rename arbitrary files.

Similar path traversal exists in the POST endpoints: `unblock-clean` (line 574) calls `remove_worktree(plan_name, config)` which constructs git commands from the unvalidated name, and `guide` (line 594) sends messages to terminals derived from the name.

## Solution

Expose a public validation function from `plan_parser.py`, then validate every endpoint in `web.py`.

In `plan_parser.py`, add a public function next to the existing regex:

```python
def is_valid_plan_name(name: str) -> bool:
    return bool(_VALID_NAME_RE.match(name))
```

In `web.py`, import `is_valid_plan_name` and add a guard to every endpoint that accepts a user-supplied name. The regex rejects `/`, `..`, spaces, and all special characters, which is sufficient to prevent path traversal.

**GET endpoints — return HTML error for invalid names:**

1. `GET /plans/{plan_name}/file-content` (line 500) — guard with `if not is_valid_plan_name(plan_name): return '<span class="dim">Invalid plan name.</span>'`

**POST endpoints — return `RedirectResponse("/", status_code=303)` for invalid names:**

2. `POST /plans/{plan_name}/pause` (line 508)
3. `POST /plans/{plan_name}/resume` (line 522)
4. `POST /plans/{plan_name}/kill` (line 553)
5. `POST /plans/{plan_name}/unblock` (line 560)
6. `POST /plans/{plan_name}/unblock-clean` (line 570)
7. `POST /plans/{plan_name}/guide` (line 582)
8. `POST /drafts/{name}/activate` (line 597) — validate `name`
9. `POST /drafts/{name}/reject` (line 605) — validate `name`

All POST endpoints early-return `RedirectResponse("/", status_code=303)` on invalid names, consistent with the existing error handling pattern (e.g. lines 527-528 return a redirect when preconditions aren't met).

## Scope

- `foreman/plan_parser.py` — add public `is_valid_plan_name()` function
- `foreman/web.py` — import `is_valid_plan_name`, add validation guard to all 9 endpoints listed above

## Risk Assessment

Low risk. The regex is already proven (used in `plan_parser.py` for all plan loading). Adding validation can only reject invalid input. POST endpoints redirect home on rejection, so users see no broken behavior.
