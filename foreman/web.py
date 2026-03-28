"""Web dashboard for Foreman — FastAPI + HTMX single-page app."""

from __future__ import annotations

import html as _html
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from foreman.config import Config
from foreman.coordination import AgentType, CoordinationDB, PlanStatus
from foreman.innovate import INNOVATOR_MARKER
from foreman.observer import PID_FILE_FOREMAN, PID_FILE_OBSERVER, is_process_running
from foreman.spawner import Spawner

_STATUS_COLOR = {
    PlanStatus.QUEUED: "#a9b1d6",
    PlanStatus.RUNNING: "#73daca",
    PlanStatus.REVIEWING: "#7aa2f7",
    PlanStatus.DONE: "#9ece6a",
    PlanStatus.BLOCKED: "#e0af68",
    PlanStatus.FAILED: "#f7768e",
    PlanStatus.INTERRUPTED: "#bb9af7",
}

_STATUS_BG = {
    PlanStatus.QUEUED: "rgba(169,177,214,.12)",
    PlanStatus.RUNNING: "rgba(115,218,202,.12)",
    PlanStatus.REVIEWING: "rgba(122,162,247,.12)",
    PlanStatus.DONE: "rgba(158,206,106,.12)",
    PlanStatus.BLOCKED: "rgba(224,175,104,.12)",
    PlanStatus.FAILED: "rgba(247,118,142,.12)",
    PlanStatus.INTERRUPTED: "rgba(187,154,247,.12)",
}

_CSS = """
:root {
  --bg: #1a1b26; --surface: #24283b; --border: #2d3148;
  --text: #c0caf5; --muted: #565f89; --accent: #7aa2f7;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font-family: 'SF Mono','Fira Code','Cascadia Code',ui-monospace,monospace;
  font-size: 13px; line-height: 1.5;
}
header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 20px; border-bottom: 1px solid var(--border);
  background: var(--surface); position: sticky; top: 0; z-index: 10;
}
.logo { font-size: 13px; font-weight: bold; letter-spacing: 3px; color: var(--accent); }
.header-meta { display: flex; gap: 16px; align-items: center; }
.stat { font-size: 12px; color: var(--muted); }
.stat b { color: var(--text); font-weight: normal; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 4px; }
.dot-alive { background: #9ece6a; }
.dot-dead { background: #f7768e; }
main { padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; max-width: 1400px; margin: 0 auto; }
.section-label {
  font-size: 10px; letter-spacing: 2px; text-transform: uppercase;
  color: var(--muted); margin-bottom: 6px; font-weight: bold;
}
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
table { width: 100%; border-collapse: collapse; }
th {
  text-align: left; font-size: 10px; letter-spacing: 1px; text-transform: uppercase;
  color: var(--muted); padding: 8px 12px; border-bottom: 1px solid var(--border);
  font-weight: normal;
}
td { padding: 8px 12px; border-bottom: 1px solid rgba(45,49,72,.6); vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(255,255,255,.02); }
.badge {
  display: inline-block; padding: 1px 7px; border-radius: 3px;
  font-size: 11px; font-weight: bold; letter-spacing: .5px; white-space: nowrap;
}
.plan-name { font-weight: bold; white-space: nowrap; }
.dim { color: var(--muted); font-size: 11px; }
.actions { display: flex; gap: 4px; align-items: center; flex-wrap: nowrap; }
.btn {
  padding: 2px 8px; border: 1px solid var(--border); background: transparent;
  color: var(--text); cursor: pointer; border-radius: 3px; font-size: 11px;
  font-family: inherit; text-decoration: none; display: inline-block; white-space: nowrap;
}
.btn:hover { background: rgba(255,255,255,.06); }
.btn-red { color: #f7768e; border-color: rgba(247,118,142,.3); }
.btn-red:hover { background: rgba(247,118,142,.1); }
.btn-green { color: #9ece6a; border-color: rgba(158,206,106,.3); }
.btn-green:hover { background: rgba(158,206,106,.1); }
.btn-blue { color: #7aa2f7; border-color: rgba(122,162,247,.3); }
.btn-blue:hover { background: rgba(122,162,247,.1); }
.btn-purple { color: #bb9af7; border-color: rgba(187,154,247,.3); }
.btn-purple:hover { background: rgba(187,154,247,.1); }
.btn-yellow { color: #e0af68; border-color: rgba(224,175,104,.3); }
.btn-yellow:hover { background: rgba(224,175,104,.1); }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.log-line { padding: 5px 12px; border-bottom: 1px solid rgba(45,49,72,.4); font-size: 12px; display: flex; gap: 10px; }
.log-line:last-child { border-bottom: none; }
.log-ts { color: var(--muted); white-space: nowrap; flex-shrink: 0; }
.log-lvl { width: 50px; flex-shrink: 0; font-weight: bold; }
.log-error { color: #f7768e; }
.log-warning { color: #e0af68; }
.log-msg { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.git-line { padding: 5px 12px; border-bottom: 1px solid rgba(45,49,72,.4); font-size: 12px; }
.git-line:last-child { border-bottom: none; }
.git-hash { color: var(--accent); margin-right: 8px; font-size: 11px; }
.empty { padding: 16px 12px; color: var(--muted); font-size: 12px; text-align: center; }
dialog {
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  color: var(--text); padding: 20px; min-width: 440px; max-width: 90vw;
}
dialog::backdrop { background: rgba(0,0,0,.75); }
dialog h3 { margin-bottom: 14px; font-size: 13px; font-weight: bold; color: var(--text); }
textarea, input[type=text] {
  width: 100%; background: var(--bg); border: 1px solid var(--border);
  color: var(--text); padding: 8px 10px; border-radius: 4px;
  font-family: inherit; font-size: 12px; margin-bottom: 12px; resize: vertical;
}
.dialog-actions { display: flex; gap: 8px; justify-content: flex-end; }
.file-body { padding: 14px; max-height: 65vh; overflow-y: auto; }
pre { white-space: pre-wrap; word-break: break-word; font-family: inherit; font-size: 12px; line-height: 1.6; }
.innovate-running { font-size: 11px; color: #bb9af7; padding: 4px 10px; }
"""

_JS = """
function showGuide(planName) {
  document.getElementById('guide-plan').textContent = planName;
  document.getElementById('guide-form').action = '/plans/' + encodeURIComponent(planName) + '/guide';
  document.getElementById('guide-msg').value = '';
  document.getElementById('guide-dialog').showModal();
  setTimeout(() => document.getElementById('guide-msg').focus(), 30);
}
function showFile(planName) {
  document.getElementById('file-title').textContent = planName + '.md';
  document.getElementById('file-body').innerHTML = '<span style="color:var(--muted)">Loading\u2026</span>';
  document.getElementById('file-dialog').showModal();
  htmx.ajax('GET', '/plans/' + encodeURIComponent(planName) + '/file-content', '#file-body');
}
function confirmDelete(name) {
  if (confirm('Delete draft-' + name + '.md?')) {
    htmx.ajax('POST', '/drafts/' + encodeURIComponent(name) + '/reject', {swap:'none'});
    setTimeout(() => location.reload(), 300);
  }
}
function confirmUnblockClean(name) {
  if (confirm('Unblock ' + name + ' and discard existing worktree?')) {
    document.getElementById('unblock-clean-form-' + name).submit();
  }
}
"""


def _h(text: object) -> str:
    return _html.escape(str(text))


def _time_ago(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    try:
        ts = datetime.fromisoformat(iso_str)
        seconds = int((datetime.now(timezone.utc) - ts).total_seconds())
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    except (ValueError, TypeError):
        return ""


def _badge(status: PlanStatus) -> str:
    color = _STATUS_COLOR[status]
    bg = _STATUS_BG[status]
    return (
        f'<span class="badge" style="color:{color};background:{bg}">'
        f"{_h(status.value)}</span>"
    )


def _form_btn(action: str, label: str, plan_name: str, cls: str = "", extra: str = "") -> str:
    return (
        f'<form method="post" action="/plans/{_h(plan_name)}/{action}" style="display:inline" {extra}>'
        f'<button type="submit" class="btn {cls}">{label}</button></form>'
    )


def _plan_actions(status: PlanStatus, name: str) -> str:
    parts: list[str] = []

    if status == PlanStatus.RUNNING:
        parts.append(_form_btn("pause", "Pause", name, "btn-yellow"))
        parts.append(_form_btn("kill", "Kill", name, "btn-red"))
        parts.append(
            f'<button class="btn btn-blue" onclick="showGuide({_h(json.dumps(name))})">Guide</button>'
        )
    elif status == PlanStatus.REVIEWING:
        parts.append(_form_btn("kill", "Kill", name, "btn-red"))
    elif status == PlanStatus.INTERRUPTED:
        parts.append(_form_btn("resume", "Resume", name, "btn-green"))
        parts.append(_form_btn("kill", "Kill", name, "btn-red"))
    elif status in (PlanStatus.BLOCKED, PlanStatus.FAILED):
        parts.append(_form_btn("unblock", "Unblock", name, "btn-yellow"))
        esc = _h(name)
        parts.append(
            f'<form id="unblock-clean-form-{esc}" method="post" '
            f'action="/plans/{esc}/unblock-clean" style="display:inline">'
            f'<button type="button" class="btn btn-red" '
            f'onclick="confirmUnblockClean({_h(json.dumps(name))})">Unblock (clean)</button></form>'
        )

    parts.append(
        f'<button class="btn" onclick="showFile({_h(json.dumps(name))})">View</button>'
    )
    return "".join(parts)


def _render_plans(db: CoordinationDB) -> str:
    plans = db.get_all_plans()
    if not plans:
        return '<div class="card"><div class="empty">No plans in database yet.</div></div>'

    rows = []
    for p in plans:
        status = PlanStatus(p["status"])
        agent_type = db.get_active_agent_type(p["plan"])
        agent_str = agent_type.value if agent_type else ""
        branch = p.get("branch") or ""
        reason = (p.get("blocked_reason") or "")
        color = _STATUS_COLOR[status]
        bg = _STATUS_BG[status]
        rows.append(
            f"<tr>"
            f'<td class="plan-name">{_h(p["plan"])}</td>'
            f'<td><span class="badge" style="color:{color};background:{bg}">{_h(status.value)}</span></td>'
            f'<td class="dim">{_h(agent_str)}</td>'
            f'<td class="dim">{_h(branch)}</td>'
            f'<td class="dim">{_h(_time_ago(p.get("updated_at")))}</td>'
            f'<td class="dim" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;'
            f'white-space:nowrap" title="{_h(reason)}">{_h(reason[:80])}</td>'
            f'<td class="actions">{_plan_actions(status, p["plan"])}</td>'
            f"</tr>"
        )

    return (
        '<div class="card"><table>'
        "<thead><tr>"
        "<th>Plan</th><th>Status</th><th>Agent</th>"
        "<th>Branch</th><th>Updated</th><th>Info</th><th>Actions</th>"
        "</tr></thead>"
        f'<tbody>{"".join(rows)}</tbody>'
        "</table></div>"
    )


def _render_drafts(config: Config) -> str:
    if not config.plans_dir.exists():
        return ""
    drafts = sorted(config.plans_dir.glob("draft-*.md"))
    if not drafts:
        return ""

    rows = []
    for path in drafts:
        name = path.stem[len("draft-"):]
        is_innovator = INNOVATOR_MARKER in path.read_text(encoding="utf-8")[:120]
        tag = ' <span class="dim">(innovator)</span>' if is_innovator else ""
        esc = _h(name)
        rows.append(
            f"<tr>"
            f"<td class=\"plan-name\">{_h(name)}{tag}</td>"
            f"<td class=\"actions\">"
            f'<form method="post" action="/drafts/{esc}/activate" style="display:inline">'
            f'<button type="submit" class="btn btn-green">Activate</button></form>'
            f'<button class="btn btn-red" onclick="confirmDelete({_h(json.dumps(name))})">Delete</button>'
            f'<button class="btn" onclick="showFile(\'draft-\' + {_h(json.dumps(name))})">View</button>'
            f"</td>"
            f"</tr>"
        )

    return (
        '<div class="section-label">Draft Plans</div>'
        '<div class="card"><table>'
        "<thead><tr><th>Name</th><th>Actions</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody>'
        "</table></div>"
    )


def _render_logs(config: Config, n: int = 25) -> str:
    if not config.log_file.exists():
        return '<div class="card"><div class="empty">No log file yet.</div></div>'

    lines_raw = config.log_file.read_text(encoding="utf-8").splitlines()
    entries = []
    for raw in reversed(lines_raw):
        if not raw.strip():
            continue
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if e.get("level") in ("ERROR", "WARNING"):
            entries.append(e)
        if len(entries) >= n:
            break

    if not entries:
        return '<div class="card"><div class="empty">No warnings or errors.</div></div>'

    rows = []
    for e in entries:
        ts = (e.get("ts") or "")[:19].replace("T", " ").split("+")[0][-8:]
        lvl = e.get("level", "")
        cls = "log-error" if lvl == "ERROR" else "log-warning"
        msg = e.get("event") or e.get("msg") or ""
        plan = e.get("plan", "")
        if plan:
            msg = f"[{plan}] {msg}"
        rows.append(
            f'<div class="log-line">'
            f'<span class="log-ts">{_h(ts)}</span>'
            f'<span class="log-lvl {cls}">{_h(lvl)}</span>'
            f'<span class="log-msg" title="{_h(msg)}">{_h(msg[:120])}</span>'
            f"</div>"
        )

    return f'<div class="card">{"".join(rows)}</div>'


def _render_git_log(config: Config, n: int = 15) -> str:
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={n}", "--oneline", "--no-decorate"],
            capture_output=True, text=True, cwd=str(config.repo_root), timeout=5,
        )
        lines = [l for l in result.stdout.strip().splitlines() if l]
    except (subprocess.SubprocessError, OSError):
        lines = []

    if not lines:
        return '<div class="card"><div class="empty">No commits yet.</div></div>'

    rows = []
    for line in lines:
        parts = line.split(" ", 1)
        sha = parts[0] if parts else ""
        msg = parts[1] if len(parts) > 1 else ""
        rows.append(
            f'<div class="git-line">'
            f'<span class="git-hash">{_h(sha)}</span>'
            f'<span>{_h(msg[:90])}</span>'
            f"</div>"
        )

    return f'<div class="card">{"".join(rows)}</div>'


def _render_header(config: Config, db: CoordinationDB | None) -> str:
    foreman_alive = is_process_running(config.repo_root, PID_FILE_FOREMAN)
    observer_alive = is_process_running(config.repo_root, PID_FILE_OBSERVER)

    fdot = "dot-alive" if foreman_alive else "dot-dead"
    odot = "dot-alive" if observer_alive else "dot-dead"
    flabel = "running" if foreman_alive else "stopped"
    olabel = "running" if observer_alive else "stopped"

    workers = reviews = w_max = r_max = 0
    if db:
        workers = len(db.get_plans_by_status(PlanStatus.RUNNING))
        reviews = len(db.get_plans_by_status(PlanStatus.REVIEWING))
        w_max = config.agents.max_parallel_workers
        r_max = config.agents.max_parallel_reviews

    draft_count = sum(1 for _ in config.plans_dir.glob("draft-*.md")) if config.plans_dir.exists() else 0
    draft_note = f' <span class="dim">({draft_count} draft{"s" if draft_count != 1 else ""})</span>' if draft_count else ""

    return (
        f'<header>'
        f'<span class="logo">FOREMAN</span>'
        f'<div class="header-meta">'
        f'<span class="stat"><span class="dot {fdot}"></span>Foreman <b>{flabel}</b></span>'
        f'<span class="stat"><span class="dot {odot}"></span>Observer <b>{olabel}</b></span>'
        f'<span class="stat">Workers <b>{workers}/{w_max}</b></span>'
        f'<span class="stat">Reviews <b>{reviews}/{r_max}</b></span>'
        f'{draft_note}'
        f'<form method="post" action="/innovate" style="display:inline">'
        f'<button type="submit" class="btn btn-purple">⚡ Innovate</button></form>'
        f'</div>'
        f'</header>'
    )


def _render_main(config: Config) -> str:
    db: CoordinationDB | None = None
    if config.coordination_db.exists():
        db = CoordinationDB(config.coordination_db)

    try:
        plans_html = _render_plans(db) if db else '<div class="card"><div class="empty">Foreman not started yet.</div></div>'
        drafts_html = _render_drafts(config)
        logs_html = _render_logs(config)
        git_html = _render_git_log(config)
    finally:
        if db:
            db.close()

    return (
        '<div id="main-content" hx-get="/state" hx-trigger="every 2s" hx-swap="outerHTML">'
        '<main>'
        '<div>'
        '<div class="section-label">Plans</div>'
        f'{plans_html}'
        '</div>'
        f'{drafts_html}'
        '<div>'
        '<div class="section-label">Activity</div>'
        '<div class="two-col">'
        '<div>'
        '<div class="section-label" style="margin-bottom:4px">Recent Warnings &amp; Errors</div>'
        f'{logs_html}'
        '</div>'
        '<div>'
        '<div class="section-label" style="margin-bottom:4px">Git Log</div>'
        f'{git_html}'
        '</div>'
        '</div>'
        '</div>'
        '</main>'
        '</div>'
    )


def _page(config: Config) -> str:
    db: CoordinationDB | None = None
    if config.coordination_db.exists():
        db = CoordinationDB(config.coordination_db)
    try:
        header = _render_header(config, db)
    finally:
        if db:
            db.close()

    main_content = _render_main(config)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Foreman</title>
<script src="https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js"></script>
<style>{_CSS}</style>
</head>
<body>
{header}
{main_content}

<dialog id="guide-dialog">
  <h3>Guide: <span id="guide-plan"></span></h3>
  <form id="guide-form" method="post">
    <textarea id="guide-msg" name="message" rows="3" placeholder="Guidance for the agent\u2026"></textarea>
    <div class="dialog-actions">
      <button type="button" class="btn" onclick="document.getElementById('guide-dialog').close()">Cancel</button>
      <button type="submit" class="btn btn-blue">Send</button>
    </div>
  </form>
</dialog>

<dialog id="file-dialog" style="min-width:560px;max-width:800px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <span id="file-title" style="font-weight:bold;font-size:13px"></span>
    <button class="btn" onclick="document.getElementById('file-dialog').close()">&#x2715;</button>
  </div>
  <div id="file-body" class="file-body"></div>
</dialog>

<script>{_JS}</script>
</body>
</html>"""


def create_app(config: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _page(config)

    @app.get("/state", response_class=HTMLResponse)
    async def state() -> str:
        return _render_main(config)

    @app.get("/plans/{plan_name}/file-content", response_class=HTMLResponse)
    async def plan_file_content(plan_name: str) -> str:
        path = config.plans_dir / f"{plan_name}.md"
        if not path.exists():
            return "<span class=\"dim\">File not found.</span>"
        text = path.read_text(encoding="utf-8")
        return f'<pre>{_h(text)}</pre>'

    @app.post("/plans/{plan_name}/pause")
    async def pause_plan(plan_name: str) -> RedirectResponse:
        db = CoordinationDB(config.coordination_db)
        status = db.get_plan_status(plan_name)
        db.close()
        if status in (PlanStatus.RUNNING, PlanStatus.REVIEWING):
            spawner = Spawner(config)
            for agent_type in AgentType:
                await spawner.kill_agent(plan_name, agent_type)
            db2 = CoordinationDB(config.coordination_db)
            db2.set_plan_status(plan_name, PlanStatus.INTERRUPTED)
            db2.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/resume")
    async def resume_plan(plan_name: str) -> RedirectResponse:
        db = CoordinationDB(config.coordination_db)
        plan_data = db.get_plan(plan_name)
        db.close()
        if not plan_data or PlanStatus(plan_data["status"]) != PlanStatus.INTERRUPTED:
            return RedirectResponse("/", status_code=303)
        worktree_path = plan_data.get("worktree_path")
        branch = plan_data.get("branch")
        if not worktree_path or not Path(worktree_path).exists():
            return RedirectResponse("/", status_code=303)
        from foreman.plan_parser import load_plans
        plans = {p.name: p for p in load_plans(config.plans_dir)}
        plan_obj = plans.get(plan_name)
        if not plan_obj:
            return RedirectResponse("/", status_code=303)
        plan_file = plan_obj.file_path.resolve()
        msg = (
            f"You are resuming work on this plan. "
            f"Read the plan at {plan_file} and review what has already been done on branch {branch}. "
            f"Continue where the previous agent left off. Commit all changes when done."
        )
        spawner = Spawner(config)
        await spawner.setup()
        pid = await spawner.spawn_agent(plan_obj, Path(worktree_path), AgentType.IMPLEMENTATION, msg)
        db2 = CoordinationDB(config.coordination_db)
        db2.set_plan_status(plan_name, PlanStatus.RUNNING)
        db2.add_agent(plan_name, AgentType.IMPLEMENTATION, pid=pid)
        db2.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/kill")
    async def kill_plan(plan_name: str) -> RedirectResponse:
        spawner = Spawner(config)
        for agent_type in AgentType:
            await spawner.kill_agent(plan_name, agent_type)
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/unblock")
    async def unblock_plan(plan_name: str) -> RedirectResponse:
        if config.coordination_db.exists():
            db = CoordinationDB(config.coordination_db)
            status = db.get_plan_status(plan_name)
            if status in (PlanStatus.BLOCKED, PlanStatus.FAILED):
                db.set_plan_status(plan_name, PlanStatus.QUEUED)
            db.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/unblock-clean")
    async def unblock_plan_clean(plan_name: str) -> RedirectResponse:
        if config.coordination_db.exists():
            from foreman.worktree import remove_worktree
            await remove_worktree(plan_name, config)
            db = CoordinationDB(config.coordination_db)
            status = db.get_plan_status(plan_name)
            if status in (PlanStatus.BLOCKED, PlanStatus.FAILED):
                db.set_plan_status(plan_name, PlanStatus.QUEUED)
            db.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/guide")
    async def guide_plan(
        plan_name: str,
        message: Annotated[str, Form()],
    ) -> RedirectResponse:
        if config.coordination_db.exists():
            pass
        return RedirectResponse("/", status_code=303)

    @app.post("/drafts/{name}/activate")
    async def activate_draft(name: str) -> RedirectResponse:
        src = config.plans_dir / f"draft-{name}.md"
        dst = config.plans_dir / f"{name}.md"
        if src.exists() and not dst.exists():
            src.rename(dst)
        return RedirectResponse("/", status_code=303)

    @app.post("/drafts/{name}/reject")
    async def reject_draft(name: str) -> RedirectResponse:
        path = config.plans_dir / f"draft-{name}.md"
        path.unlink(missing_ok=True)
        return RedirectResponse("/", status_code=303)

    @app.post("/innovate")
    async def trigger_innovate(background_tasks: BackgroundTasks) -> RedirectResponse:
        background_tasks.add_task(_run_innovate_background, config)
        return RedirectResponse("/", status_code=303)

    return app


async def _run_innovate_background(config: Config) -> None:
    from foreman.innovate import innovate
    await innovate(config)
