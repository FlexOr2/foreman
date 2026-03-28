"""Web dashboard for Foreman — FastAPI + HTMX single-page app."""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from foreman.config import ALL_IDEA_CATEGORIES, FOREMAN_DIR, Config, RELOAD_CONFIG_MARKER, save_config
from foreman.plan_parser import is_valid_plan_name
from foreman.coordination import AgentType, CoordinationDB, PlanStatus
from foreman.observer import PID_FILE_FOREMAN, PID_FILE_OBSERVER, is_process_running
from foreman.spawner import Spawner, log_filename

log = logging.getLogger(__name__)

LOG_TAIL_BYTES = 65536

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
input[type=number], select {
  background: var(--bg); border: 1px solid var(--border);
  color: var(--text); padding: 5px 8px; border-radius: 3px;
  font-family: inherit; font-size: 12px;
}
.cfg-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.cfg-title { font-size: 10px; letter-spacing: 2px; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; font-weight: bold; }
.cfg-field { margin-bottom: 10px; }
.cfg-field label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 3px; }
.cfg-field input[type=number], .cfg-field select { width: 100%; }
.check-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 12px; cursor: pointer; }
.check-row input[type=checkbox] { accent-color: var(--accent); width: 13px; height: 13px; cursor: pointer; }
.cat-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; }
.resume-option { display: block; width: 100%; text-align: left; padding: 8px 12px; margin-bottom: 6px; }
.resume-desc { font-size: 11px; color: var(--muted); margin-top: 2px; }
.priority-badge { font-size: 10px; color: var(--muted); min-width: 20px; text-align: center; }
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
function showResume(planName, hasWorktree) {
  document.getElementById('resume-plan-name').textContent = planName;
  var enc = encodeURIComponent(planName);
  document.getElementById('resume-review-form').action = '/plans/' + enc + '/resume-review';
  document.getElementById('resume-opus-form').action = '/plans/' + enc + '/resume-review';
  document.getElementById('force-merge-form').action = '/plans/' + enc + '/force-merge';
  document.getElementById('resume-scratch-form').action = '/plans/' + enc + '/unblock-clean';
  var implOpts = document.querySelectorAll('.resume-needs-impl');
  implOpts.forEach(function(el) { el.style.display = hasWorktree ? '' : 'none'; });
  document.getElementById('resume-dialog').showModal();
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


def _priority_controls(name: str, priority: int) -> str:
    enc = _h(name)
    return (
        f'<form method="post" action="/plans/{enc}/priority-up" style="display:inline">'
        f'<button type="submit" class="btn" title="Increase priority">▲</button></form>'
        f'<span class="priority-badge">{priority}</span>'
        f'<form method="post" action="/plans/{enc}/priority-down" style="display:inline">'
        f'<button type="submit" class="btn" title="Decrease priority">▼</button></form>'
        f'<form method="post" action="/plans/{enc}/run-next" style="display:inline">'
        f'<button type="submit" class="btn btn-blue" title="Move to front of queue">Next</button></form>'
    )


def _plan_actions(status: PlanStatus, name: str, plan_data: dict) -> str:
    parts: list[str] = []

    if status == PlanStatus.QUEUED:
        parts.append(_priority_controls(name, plan_data.get("priority", 0)))
    elif status == PlanStatus.RUNNING:
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
        has_worktree = bool(plan_data.get("worktree_path"))
        parts.append(
            f'<button class="btn btn-yellow" '
            f'onclick="showResume({_h(json.dumps(name))}, {str(has_worktree).lower()})">Resume</button>'
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
            f'<td class="actions">{_plan_actions(status, p["plan"], p)}</td>'
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
        tag = ' <span class="dim">(innovator)</span>'
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

    with open(config.log_file, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - LOG_TAIL_BYTES))
        tail = f.read().decode("utf-8", errors="replace")

    entries = []
    for line in reversed(tail.splitlines()):
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


def _render_config_dialog(config: Config) -> str:
    def checked(val: bool) -> str:
        return " checked" if val else ""

    model_options = "".join(
        f'<option value="{m}"{"  selected" if config.agents.model == m else ""}>{m}</option>'
        for m in ("opus", "sonnet", "haiku")
    )

    cat_checkboxes = "".join(
        f'<label class="check-row">'
        f'<input type="checkbox" name="categories" value="{_h(c)}"'
        f'{checked(c in config.innovate.categories)}>'
        f'{_h(c)}</label>'
        for c in ALL_IDEA_CATEGORIES
    )

    return (
        '<dialog id="config-dialog" style="min-width:560px;max-width:90vw;max-height:85vh;overflow-y:auto">'
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'
        '<span style="font-weight:bold;font-size:13px">Settings</span>'
        '<button type="button" class="btn" onclick="document.getElementById(\'config-dialog\').close()">&#x2715;</button>'
        '</div>'
        '<form method="post" action="/config">'
        '<div class="cfg-grid">'
        '<div>'
        '<div class="cfg-title">Agents</div>'
        f'<div class="cfg-field"><label>Max workers</label>'
        f'<input type="number" name="max_parallel_workers" value="{config.agents.max_parallel_workers}" min="1" max="20"></div>'
        f'<div class="cfg-field"><label>Max reviews</label>'
        f'<input type="number" name="max_parallel_reviews" value="{config.agents.max_parallel_reviews}" min="1" max="20"></div>'
        f'<div class="cfg-field"><label>Model</label>'
        f'<select name="model">{model_options}</select></div>'
        '<div class="cfg-title" style="margin-top:14px">General</div>'
        f'<label class="check-row"><input type="checkbox" name="auto_restart" value="1"{checked(config.auto_restart)}>Auto restart</label>'
        '</div>'
        '<div>'
        '<div class="cfg-title">Timeouts (seconds)</div>'
        f'<div class="cfg-field"><label>Implementation</label>'
        f'<input type="number" name="implementation_timeout" value="{config.timeouts.implementation}" min="60"></div>'
        f'<div class="cfg-field"><label>Review</label>'
        f'<input type="number" name="review_timeout" value="{config.timeouts.review}" min="60"></div>'
        f'<div class="cfg-field"><label>Stuck threshold</label>'
        f'<input type="number" name="stuck_threshold" value="{config.timeouts.stuck_threshold}" min="60"></div>'
        '</div>'
        '</div>'
        '<div style="margin-top:14px;border-top:1px solid var(--border);padding-top:14px">'
        '<div class="cfg-title">Innovator</div>'
        '<div style="display:flex;gap:20px;flex-wrap:wrap;margin-bottom:10px">'
        f'<label class="check-row"><input type="checkbox" name="innovate_enabled" value="1"{checked(config.innovate.enabled)}>Enabled</label>'
        f'<label class="check-row"><input type="checkbox" name="auto_activate" value="1"{checked(config.innovate.auto_activate)}>Auto activate</label>'
        f'<label class="check-row"><input type="checkbox" name="skip_review" value="1"{checked(config.innovate.skip_review)}>Skip review</label>'
        '</div>'
        '<div class="cfg-grid" style="margin-bottom:10px">'
        f'<div class="cfg-field"><label>Interval (seconds)</label>'
        f'<input type="number" name="innovate_interval" value="{config.innovate.interval}" min="60"></div>'
        f'<div class="cfg-field"><label>Max drafts</label>'
        f'<input type="number" name="innovate_max_drafts" value="{config.innovate.max_drafts}" min="1"></div>'
        '</div>'
        '<div class="cfg-title">Categories</div>'
        f'<div class="cat-grid">{cat_checkboxes}</div>'
        '</div>'
        '<div class="dialog-actions" style="margin-top:16px">'
        '<button type="button" class="btn" onclick="document.getElementById(\'config-dialog\').close()">Cancel</button>'
        '<button type="submit" class="btn btn-blue">Save</button>'
        '</div>'
        '</form>'
        '</dialog>'
    )


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
        f'<div id="header-content" hx-get="/header" hx-trigger="every 3s" hx-swap="outerHTML">'
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
        f'<button class="btn" onclick="document.getElementById(\'config-dialog\').showModal()">Config</button>'
        f'</div>'
        f'</header>'
        f'</div>'
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

<dialog id="resume-dialog">
  <h3>Resume: <span id="resume-plan-name"></span></h3>
  <div style="display:flex;flex-direction:column;gap:4px;margin-top:10px">
    <div class="resume-needs-impl">
      <form id="resume-review-form" method="post">
        <button type="submit" class="btn btn-blue resume-option">
          Resume review
          <div class="resume-desc">Re-run the review agent — code is already committed</div>
        </button>
      </form>
    </div>
    <div class="resume-needs-impl">
      <form id="resume-opus-form" method="post">
        <input type="hidden" name="model" value="claude-opus-4-6">
        <button type="submit" class="btn btn-purple resume-option">
          Resume with Opus
          <div class="resume-desc">Run review with claude-opus-4-6 for complex or tricky changes</div>
        </button>
      </form>
    </div>
    <div class="resume-needs-impl">
      <form id="force-merge-form" method="post">
        <button type="submit" class="btn btn-green resume-option">
          Force merge
          <div class="resume-desc">Skip review, merge the branch directly — you trust the implementation</div>
        </button>
      </form>
    </div>
    <div>
      <form id="resume-scratch-form" method="post">
        <button type="submit" class="btn btn-red resume-option">
          Retry from scratch
          <div class="resume-desc">Discard the existing worktree and re-queue for implementation</div>
        </button>
      </form>
    </div>
  </div>
  <div class="dialog-actions" style="margin-top:14px">
    <button type="button" class="btn" onclick="document.getElementById('resume-dialog').close()">Cancel</button>
  </div>
</dialog>

<dialog id="file-dialog" style="min-width:560px;max-width:800px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <span id="file-title" style="font-weight:bold;font-size:13px"></span>
    <button class="btn" onclick="document.getElementById('file-dialog').close()">&#x2715;</button>
  </div>
  <div id="file-body" class="file-body"></div>
</dialog>

{_render_config_dialog(config)}
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

    @app.get("/header", response_class=HTMLResponse)
    async def header() -> str:
        db = CoordinationDB(config.coordination_db) if config.coordination_db.exists() else None
        try:
            return _render_header(config, db)
        finally:
            if db:
                db.close()

    @app.get("/plans/{plan_name}/file-content", response_class=HTMLResponse)
    async def plan_file_content(plan_name: str) -> str:
        if not is_valid_plan_name(plan_name):
            return '<span class="dim">Invalid plan name.</span>'
        path = config.plans_dir / f"{plan_name}.md"
        if not path.exists():
            return "<span class=\"dim\">File not found.</span>"
        text = path.read_text(encoding="utf-8")
        return f'<pre>{_h(text)}</pre>'

    @app.post("/plans/{plan_name}/pause")
    async def pause_plan(plan_name: str) -> RedirectResponse:
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
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
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
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
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
        spawner = Spawner(config)
        for agent_type in AgentType:
            await spawner.kill_agent(plan_name, agent_type)
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/unblock")
    async def unblock_plan(plan_name: str) -> RedirectResponse:
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
        if config.coordination_db.exists():
            db = CoordinationDB(config.coordination_db)
            status = db.get_plan_status(plan_name)
            if status in (PlanStatus.BLOCKED, PlanStatus.FAILED):
                db.set_plan_status(plan_name, PlanStatus.QUEUED)
            db.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/unblock-clean")
    async def unblock_plan_clean(plan_name: str) -> RedirectResponse:
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
        if config.coordination_db.exists():
            from foreman.worktree import remove_worktree
            await remove_worktree(plan_name, config)
            db = CoordinationDB(config.coordination_db)
            status = db.get_plan_status(plan_name)
            if status in (PlanStatus.BLOCKED, PlanStatus.FAILED):
                db.set_plan_status(plan_name, PlanStatus.QUEUED)
            db.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/resume-review")
    async def resume_review_plan(
        plan_name: str,
        model: Annotated[str | None, Form()] = None,
    ) -> RedirectResponse:
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
        if not config.coordination_db.exists():
            return RedirectResponse("/", status_code=303)
        db = CoordinationDB(config.coordination_db)
        plan_data = db.get_plan(plan_name)
        if not plan_data or PlanStatus(plan_data["status"]) not in (PlanStatus.BLOCKED, PlanStatus.FAILED):
            db.close()
            return RedirectResponse("/", status_code=303)
        worktree_path = plan_data.get("worktree_path")
        if not worktree_path or not Path(worktree_path).exists():
            db.close()
            return RedirectResponse("/", status_code=303)
        if model:
            db.set_model_override(plan_name, model)
        db.close()
        from foreman.plan_parser import load_plans
        plans = {p.name: p for p in load_plans(config.plans_dir)}
        plan_obj = plans.get(plan_name)
        if not plan_obj:
            return RedirectResponse("/", status_code=303)
        plan_file = plan_obj.file_path.resolve()
        initial_message = (
            f"Review the changes on this branch against main. "
            f"The original plan is at {plan_file}."
        )
        spawner = Spawner(config)
        await spawner.setup()
        pid = await spawner.spawn_agent(
            plan_obj, Path(worktree_path), AgentType.REVIEW, initial_message,
            model_override=model or None,
        )
        db2 = CoordinationDB(config.coordination_db)
        db2.set_plan_status(plan_name, PlanStatus.REVIEWING)
        db2.add_agent(
            plan_name, AgentType.REVIEW, pid=pid,
            log_file=str(config.log_dir / log_filename(plan_name, AgentType.REVIEW)),
        )
        db2.close()
        log.info("Resume-review triggered for %s (model=%s)", plan_name, model or "default")
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/force-merge")
    async def force_merge_plan(plan_name: str) -> RedirectResponse:
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
        if not config.coordination_db.exists():
            return RedirectResponse("/", status_code=303)
        db = CoordinationDB(config.coordination_db)
        plan_data = db.get_plan(plan_name)
        db.close()
        if not plan_data or PlanStatus(plan_data["status"]) not in (PlanStatus.BLOCKED, PlanStatus.FAILED):
            return RedirectResponse("/", status_code=303)
        branch = plan_data.get("branch")
        if not branch:
            return RedirectResponse("/", status_code=303)
        from foreman.worktree import merge_branch, remove_worktree
        success, output, _ = await merge_branch(branch, config.repo_root)
        db2 = CoordinationDB(config.coordination_db)
        if success:
            log.info("Force-merged branch %s for plan %s (skipped review)", branch, plan_name)
            db2.set_plan_status(plan_name, PlanStatus.DONE)
            db2.close()
            await remove_worktree(plan_name, config)
            plan_file = config.plans_dir / f"{plan_name}.md"
            plan_file.unlink(missing_ok=True)
        else:
            log.warning("Force merge failed for %s: %s", plan_name, output[:200])
            db2.set_plan_status(plan_name, PlanStatus.BLOCKED, reason=f"Force merge failed: {output[:200]}")
            db2.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/guide")
    async def guide_plan(
        plan_name: str,
        message: Annotated[str, Form()],
    ) -> RedirectResponse:
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
        if config.coordination_db.exists():
            pass
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/priority-up")
    async def priority_up(plan_name: str) -> RedirectResponse:
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
        if config.coordination_db.exists():
            db = CoordinationDB(config.coordination_db)
            plan_data = db.get_plan(plan_name)
            if plan_data and PlanStatus(plan_data["status"]) == PlanStatus.QUEUED:
                db.set_plan_priority(plan_name, plan_data.get("priority", 0) + 1)
            db.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/priority-down")
    async def priority_down(plan_name: str) -> RedirectResponse:
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
        if config.coordination_db.exists():
            db = CoordinationDB(config.coordination_db)
            plan_data = db.get_plan(plan_name)
            if plan_data and PlanStatus(plan_data["status"]) == PlanStatus.QUEUED:
                db.set_plan_priority(plan_name, plan_data.get("priority", 0) - 1)
            db.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/plans/{plan_name}/run-next")
    async def run_next(plan_name: str) -> RedirectResponse:
        if not is_valid_plan_name(plan_name):
            return RedirectResponse("/", status_code=303)
        if config.coordination_db.exists():
            db = CoordinationDB(config.coordination_db)
            plan_data = db.get_plan(plan_name)
            if plan_data and PlanStatus(plan_data["status"]) == PlanStatus.QUEUED:
                db.set_plan_priority(plan_name, db.get_max_queued_priority() + 1)
            db.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/drafts/{name}/activate")
    async def activate_draft(name: str) -> RedirectResponse:
        if not is_valid_plan_name(name):
            return RedirectResponse("/", status_code=303)
        src = config.plans_dir / f"draft-{name}.md"
        dst = config.plans_dir / f"{name}.md"
        if src.exists() and not dst.exists():
            src.rename(dst)
        return RedirectResponse("/", status_code=303)

    @app.post("/drafts/{name}/reject")
    async def reject_draft(name: str) -> RedirectResponse:
        if not is_valid_plan_name(name):
            return RedirectResponse("/", status_code=303)
        path = config.plans_dir / f"draft-{name}.md"
        path.unlink(missing_ok=True)
        return RedirectResponse("/", status_code=303)

    @app.post("/innovate")
    async def trigger_innovate(background_tasks: BackgroundTasks) -> RedirectResponse:
        background_tasks.add_task(_run_innovate_background, config)
        return RedirectResponse("/", status_code=303)

    @app.post("/config")
    async def update_config(
        max_parallel_workers: Annotated[int, Form()],
        max_parallel_reviews: Annotated[int, Form()],
        model: Annotated[str, Form()],
        implementation_timeout: Annotated[int, Form()],
        review_timeout: Annotated[int, Form()],
        stuck_threshold: Annotated[int, Form()],
        innovate_interval: Annotated[int, Form()],
        innovate_max_drafts: Annotated[int, Form()],
        auto_restart: Annotated[str | None, Form()] = None,
        innovate_enabled: Annotated[str | None, Form()] = None,
        auto_activate: Annotated[str | None, Form()] = None,
        skip_review: Annotated[str | None, Form()] = None,
        categories: Annotated[list[str] | None, Form()] = None,
    ) -> RedirectResponse:
        config.agents.max_parallel_workers = max(1, max_parallel_workers)
        config.agents.max_parallel_reviews = max(1, max_parallel_reviews)
        if model in ("opus", "sonnet", "haiku"):
            config.agents.model = model
        config.timeouts.implementation = max(60, implementation_timeout)
        config.timeouts.review = max(60, review_timeout)
        config.timeouts.stuck_threshold = max(60, stuck_threshold)
        config.auto_restart = auto_restart is not None
        config.innovate.enabled = innovate_enabled is not None
        config.innovate.auto_activate = auto_activate is not None
        config.innovate.skip_review = skip_review is not None
        config.innovate.interval = max(60, innovate_interval)
        config.innovate.max_drafts = max(1, innovate_max_drafts)
        config.innovate.categories = [c for c in (categories or []) if c in ALL_IDEA_CATEGORIES]
        save_config(config)
        (config.repo_root / RELOAD_CONFIG_MARKER).write_text("")
        log.info("Config saved via web UI")
        return RedirectResponse("/", status_code=303)

    return app


_innovate_lock = asyncio.Lock()


async def _run_innovate_background(config: Config) -> None:
    if _innovate_lock.locked():
        log.info("Innovate already running, skipping")
        return
    async with _innovate_lock:
        try:
            from foreman.innovate import innovate
            await innovate(config)
        except Exception:
            log.error("Background innovate failed", exc_info=True)
