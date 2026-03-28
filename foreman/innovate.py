"""Autonomous innovation discovery with adversarial review pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

import foreman.config as _config
from foreman.brain import ForemanBrain
from foreman.config import Config

log = logging.getLogger(__name__)

BRAIN_TOOLS = "Read,Glob,Grep,Bash"
BRAIN_TOOLS_WEB = "Read,Glob,Grep,Bash,WebSearch,WebFetch"

_LOG_LOOKBACK_HOURS = 24
_WARN_KEYWORDS = frozenset({"orphan", "stuck", "timeout", "failed", "crash"})
_RAPID_FAILURE_COUNT = 3
_RAPID_FAILURE_WINDOW_SECONDS = 600


class IdeaCategory(StrEnum):
    RISK = "risk"
    PERFORMANCE = "performance"
    ARCHITECTURE = "architecture"
    DEBT = "debt"
    DX = "dx"
    FEATURES = "features"
    COMPETITIVE = "competitive"
    DELIGHT = "delight"
    MOONSHOTS = "moonshots"
    CREATE = "create"


QUESTIONS: dict[IdeaCategory, list[str]] = {
    IdeaCategory.RISK: [
        "What's the single point of failure in this system?",
        "If the database went down for 5 minutes, what would happen to users?",
        "What error conditions are silently swallowed?",
        "Where does this system trust input it shouldn't?",
    ],
    IdeaCategory.PERFORMANCE: [
        "What would fail first under 10x the current load?",
        "What O(n) operation is hiding that will become O(n^2) with growth?",
        "What data structure will become the bottleneck?",
    ],
    IdeaCategory.DX: [
        "What would a new developer find most confusing in the first week?",
        "What takes 10 steps that should take 1?",
        "Where does the code contradict the documentation?",
        "What convention is inconsistently followed?",
    ],
    IdeaCategory.ARCHITECTURE: [
        "Where is the code lying to itself? (abstraction says X, implementation does Y)",
        "What module knows too much about another module's internals?",
        "What would you redesign if you could start this component over?",
        "What's duplicated across the codebase that should be unified?",
    ],
    IdeaCategory.DEBT: [
        "What dependency hasn't been updated in over a year?",
        "What dependency has a better alternative that didn't exist when this was written?",
        "What's the blast radius if a key dependency has a breaking change?",
        "What feature appears half-finished based on the code?",
        "What configuration option exists but appears unused?",
        "What error message would confuse a user?",
    ],
    IdeaCategory.FEATURES: [
        "What's the natural next feature that users would expect but doesn't exist yet?",
        "What data does this app already have access to that it's not using to create value?",
        "What manual workflow does this app automate only halfway?",
        "What would make a user say 'I can't believe this can do that'?",
        "What would turn this from a tool people use into a tool people recommend?",
        "What integration with another system would unlock a completely new use case?",
        "What would this app look like if it could predict what the user wants next?",
    ],
    IdeaCategory.COMPETITIVE: [
        "What do similar tools in this space do that this app doesn't?",
        "What do similar tools get wrong that this app could get right?",
        "What's the one thing no tool in this space does — the gap everyone has missed?",
        "What approach from a completely different domain could be applied here?",
        "If this app had to justify a 10x price increase, what features would it need?",
        "What would make this app the default choice over every alternative?",
    ],
    IdeaCategory.DELIGHT: [
        "Where does the user have to think when the app should think for them?",
        "What takes the user out of their flow that could be eliminated?",
        "What would make the first 5 minutes of using this app feel magical?",
        "What feedback loops are missing — where does the user do something but never learn if it worked?",
        "What power-user shortcut is hiding that should be a first-class feature?",
        "What would make users feel like this app understands their work?",
    ],
    IdeaCategory.MOONSHOTS: [
        "If there were no technical constraints, what would the ideal version of this app do?",
        "What emerging technology could transform what this app is capable of?",
        "What would this app look like if it could collaborate with the user in real time?",
        "What if this app could learn from how each user works and adapt itself?",
        "What's the version of this app that makes its own category — not competing with existing tools but creating a new space?",
    ],
    IdeaCategory.CREATE: [
        "What new tool, library, or project would solve a problem you see in this codebase's ecosystem? What's missing from the developer's workflow?",
        "What tool would you build if you could start from scratch, taking all the lessons learned from this codebase?",
        "Think beyond this project — what would complement or extend this ecosystem in a genuinely novel way?",
        "What problem do users of this project frequently run into that a companion tool could solve?",
    ],
}

DEFENSIVE_CATEGORIES = {IdeaCategory.RISK, IdeaCategory.PERFORMANCE, IdeaCategory.ARCHITECTURE, IdeaCategory.DEBT, IdeaCategory.DX}
CREATIVE_CATEGORIES = {IdeaCategory.FEATURES, IdeaCategory.COMPETITIVE, IdeaCategory.DELIGHT, IdeaCategory.MOONSHOTS}


_DEVIL_PROMPT = """\
You are a ruthless critic. Your job is to destroy this plan.

- What will this break?
- What's the hidden cost nobody mentioned?
- What second-order effects will this cause?
- Is this solving a real problem or a phantom one?
- What's the worst-case scenario if this goes wrong?
- Has the author considered rollback?

You MUST end your response with a verdict in exactly this format:

VERDICT: KILL
REASON: your reason here

OR

VERDICT: REVISE
REASON: your reason here
DEMANDS:
- specific demand 1
- specific demand 2

OR

VERDICT: PASS
REASON: your reason here"""

_PRAGMATIST_PROMPT = """\
You are a pragmatic senior engineer with limited time and patience.

- Is the juice worth the squeeze? (effort vs impact)
- Is there a simpler solution that gets 80% of the benefit?
- Does this actually need to be done now, or is it premature?
- Will the team understand and maintain this change?
- What's the opportunity cost — what won't get done if we do this?

You MUST end your response with a verdict in exactly this format:

VERDICT: KILL
REASON: your reason here

OR

VERDICT: REVISE
REASON: your reason here
DEMANDS:
- specific demand 1
- specific demand 2

OR

VERDICT: PASS
REASON: your reason here"""

_ARCHITECT_PROMPT = """\
You are a systems architect who thinks in decades.

- Does this align with the system's existing architecture?
- Does this create coupling or reduce it?
- Will this scale with the codebase as it grows?
- Does this introduce a new pattern, and is that pattern worth its weight?
- Is this the right abstraction level?

You MUST end your response with a verdict in exactly this format:

VERDICT: KILL
REASON: your reason here

OR

VERDICT: REVISE
REASON: your reason here
DEMANDS:
- specific demand 1
- specific demand 2

OR

VERDICT: PASS
REASON: your reason here"""

REVIEWERS = [
    ("devil", _DEVIL_PROMPT),
    ("pragmatist", _PRAGMATIST_PROMPT),
    ("architect", _ARCHITECT_PROMPT),
]


class Verdict(StrEnum):
    KILL = "KILL"
    REVISE = "REVISE"
    PASS = "PASS"


@dataclass
class ReviewResult:
    action: Verdict
    reason: str
    demands: list[str]


_ReviewCallback = Callable[[str, ReviewResult], None]

_VERDICT_RE = re.compile(
    r"VERDICT:\s*(KILL|REVISE|PASS)\s*\n"
    r"REASON:\s*(.+?)(?:\nDEMANDS:\s*\n((?:- .+\n?)+))?$",
    re.DOTALL,
)

_PLAN_SEPARATOR = re.compile(r"^---\s*$", re.MULTILINE)


def _parse_verdict(text: str) -> ReviewResult:
    match = _VERDICT_RE.search(text)
    if not match:
        log.warning("Could not parse verdict, treating as KILL")
        return ReviewResult(action=Verdict.KILL, reason="unparseable response", demands=[])

    action = Verdict(match.group(1))
    reason = match.group(2).strip()
    demands_raw = match.group(3)
    demands = [d.strip().lstrip("- ") for d in demands_raw.strip().splitlines()] if demands_raw else []
    return ReviewResult(action=action, reason=reason, demands=demands)


def _select_questions(categories: list[str]) -> dict[str, list[str]]:
    if "all" in categories:
        return {cat.value: QUESTIONS[cat] for cat in IdeaCategory}

    selected: dict[str, list[str]] = {}
    for cat_name in categories:
        try:
            cat = IdeaCategory(cat_name)
            selected[cat.value] = QUESTIONS[cat]
        except ValueError:
            log.warning("Unknown category: %s", cat_name)
    return selected


def _parse_draft_plans(brain_output: str) -> list[tuple[str, str]]:
    plans: list[tuple[str, str]] = []
    chunks = _PLAN_SEPARATOR.split(brain_output)

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        title_match = re.search(r"^#\s+(.+)$", chunk, re.MULTILINE)
        if not title_match:
            continue

        title = title_match.group(1).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        if not slug:
            continue

        plans.append((slug, chunk))

    return plans


async def _invoke_reviewer(prompt: str, permission_mode: str, timeout: int, claude_bin: str = "") -> str:
    cmd = [
        claude_bin or _config.CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", permission_mode,
        "--allowed-tools", "",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        raise RuntimeError(f"Reviewer failed (rc={proc.returncode}): {stderr.decode().strip()}")

    response = json.loads(stdout.decode())
    return response.get("result", "")


def _build_runtime_context(config: Config) -> str:
    error_lines: list[tuple[datetime, str]] = []
    plan_fail_times: dict[str, list[datetime]] = {}

    if config.log_file.exists():
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=_LOG_LOOKBACK_HOURS)
        for raw in config.log_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts_str = entry.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if ts < cutoff:
                continue

            level = entry.get("level", "")
            msg = entry.get("msg") or entry.get("event") or ""
            plan = entry.get("plan", "")
            prefix = f"[{plan}] " if plan else ""
            time_str = ts_str[11:19]

            if level == "ERROR" or (level == "WARNING" and any(kw in msg.lower() for kw in _WARN_KEYWORDS)):
                error_lines.append((ts, f"- {time_str} {prefix}{msg}"))
                if plan:
                    plan_fail_times.setdefault(plan, []).append(ts)

    rapid_failures: list[str] = []
    for plan, times in plan_fail_times.items():
        times.sort()
        for i in range(len(times) - (_RAPID_FAILURE_COUNT - 1)):
            window = (times[i + _RAPID_FAILURE_COUNT - 1] - times[i]).total_seconds()
            if window <= _RAPID_FAILURE_WINDOW_SECONDS:
                rapid_failures.append(
                    f"- {plan}: {len(times)} failures, "
                    f"{_RAPID_FAILURE_COUNT} in {int(window / 60) + 1} min (rapid failure pattern)"
                )
                break

    failed_plans: list[str] = []
    if config.coordination_db.exists():
        conn = sqlite3.connect(str(config.coordination_db))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT plan, status, blocked_reason FROM plans WHERE status IN ('FAILED', 'BLOCKED')"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            reason = row["blocked_reason"] or "no reason recorded"
            failed_plans.append(f"- {row['plan']}: {row['status']} — {reason}")

    if not error_lines and not rapid_failures and not failed_plans:
        return ""

    parts = ["## Recent Runtime Issues (from logs)\n"]

    if error_lines:
        error_lines.sort(key=lambda x: x[0])
        parts.append("Errors and warnings (last 24h):")
        parts.extend(line for _, line in error_lines)

    if rapid_failures:
        parts.append("\nRapid failure patterns:")
        parts.extend(rapid_failures)

    if failed_plans:
        parts.append("\nFailed/Blocked plans:")
        parts.extend(failed_plans)

    parts.append(
        "\nThese are REAL problems that happened at runtime. "
        "Prioritize fixing these over code style issues."
    )
    return "\n".join(parts) + "\n"


def _build_explore_prompt(
    categories: list[str],
    max_ideas: int,
    scope_path: str | None,
    web: bool,
    runtime_context: str = "",
) -> str:
    questions = _select_questions(categories)

    selected_defensive = [c for c in categories if c in {cat.value for cat in DEFENSIVE_CATEGORIES}]
    selected_creative = [c for c in categories if c in {cat.value for cat in CREATIVE_CATEGORIES}]

    questions_block = ""
    if selected_defensive:
        questions_block += "\n### Defensive (what's broken/risky)\n"
        for cat in selected_defensive:
            if cat in questions:
                for q in questions[cat]:
                    questions_block += f"- {q}\n"

    if selected_creative:
        questions_block += "\n### Creative (what's possible/missing)\n"
        for cat in selected_creative:
            if cat in questions:
                for q in questions[cat]:
                    questions_block += f"- {q}\n"

    scope_instruction = ""
    if scope_path:
        scope_instruction = f"\nFocus your analysis on the subtree: {scope_path}\n"

    web_instruction = ""
    if web:
        web_instruction = (
            "\nYou have web search available. Use it to research competing tools, "
            "ecosystem trends, emerging patterns, user complaints about similar apps, "
            "and feature requests in this space.\n"
        )

    runtime_section = f"\n{runtime_context}\n" if runtime_context else ""
    priority_instruction = (
        "Runtime failures from the logs are higher priority than static code analysis findings. "
        "A bug that crashes the system is more important than a private method being called from the wrong module.\n\n"
        if runtime_context else ""
    )

    return f"""\
You are an autonomous innovation agent. Your job is to deeply analyze a codebase and discover non-obvious improvement opportunities.
{priority_instruction}
## Phase 1: Explore
Read the codebase structure, dependencies, recent git log, README, CLAUDE.md, config files, and entry points. Understand what this project does, how it works, and what its current state is.
{scope_instruction}{web_instruction}{runtime_section}
## Phase 2: Provoke
Think deeply about these questions. Don't settle for surface-level answers — dig into the code to find real insights.
{questions_block}
## Phase 3: Shape
For each genuine insight worth pursuing, create a structured improvement plan. Quality over quantity — only suggest improvements you are confident about after reading the actual code.

Requirements:
- Each plan must be concrete and actionable (not "consider doing X")
- Each plan must be self-contained — an implementation agent should be able to execute it
- Prefer small, focused plans over large refactoring efforts
- Do not suggest changes that require business context you don't have
- Generate at most {max_ideas} plans

For each improvement, output a markdown plan using exactly this format, separated by --- on its own line:

# Short descriptive title

> **Depends on:**

## Problem

Describe the specific issue found, with evidence from the code.

## Solution

Describe the concrete changes to make.

## Scope

List the files that would need to change.

## Risk Assessment

What could go wrong with this change.

---

Output ONLY the plan documents separated by ---, nothing else. If you find no actionable improvements, output exactly: NO_FINDINGS"""


_CREATE_PLAN_FORMAT = """\
# Short descriptive project name

## Why It Should Exist

Describe the specific problem it solves and why no existing tool addresses it adequately.

## Tech Stack

List languages, frameworks, and key dependencies with brief justification for each choice.

## File Structure

```
project-root/
├── ...
```

## Implementation Plan

### Phase 1: Core
- ...

### Phase 2: ...
- ...

## CLAUDE.md

Provide a complete CLAUDE.md for the new project covering code style, architecture decisions, and what not to do."""


def _build_create_prompt(max_ideas: int, web: bool) -> str:
    questions_block = "\n".join(f"- {q}" for q in QUESTIONS[IdeaCategory.CREATE])

    web_instruction = ""
    if web:
        web_instruction = (
            "\nYou have web search available. Research the existing tool landscape to validate "
            "that your proposed project fills a genuine gap and doesn't already exist.\n"
        )

    return f"""\
You are an autonomous innovation agent. Your job is to discover what entirely new projects should exist to complement or extend this codebase's ecosystem.

## Phase 1: Explore
Read the codebase structure, dependencies, README, CLAUDE.md, and entry points. Understand what this project does and what problems it solves.
{web_instruction}
## Phase 2: Provoke
Think beyond this codebase. Consider these questions:
{questions_block}

## Phase 3: Blueprint
For each new project worth building, write a complete self-contained blueprint that could bootstrap an entirely new repository.

Requirements:
- Each project must solve a real, demonstrable problem not solved by existing tools
- The blueprint must be complete enough for an agent to implement from scratch
- Include a full CLAUDE.md for the new project
- Generate at most {max_ideas} blueprints

For each new project, output a markdown blueprint using exactly this format, separated by --- on its own line:

{_CREATE_PLAN_FORMAT}

---

Output ONLY the blueprint documents separated by ---, nothing else. If you find no genuinely valuable new project to propose, output exactly: NO_FINDINGS"""


def _build_review_input(
    reviewer_name: str,
    reviewer_prompt: str,
    plan_text: str,
    feedback_history: list[tuple[str, str]],
) -> str:
    parts = [reviewer_prompt, "\n\n"]

    if feedback_history:
        parts.append("Previous reviewer feedback:\n")
        for name, feedback in feedback_history:
            parts.append(f"\n### {name.title()}'s review:\n{feedback}\n")
        parts.append("\n")

    parts.append(f"Plan to review:\n\n{plan_text}")
    return "".join(parts)


async def adversarial_review(
    plan_text: str,
    permission_mode: str,
    brain: ForemanBrain,
    reviewer_timeout: int,
    on_review: _ReviewCallback | None = None,
    claude_bin: str = "",
) -> tuple[bool, str]:
    feedback_history: list[tuple[str, str]] = []

    for round_num, (reviewer_name, reviewer_prompt) in enumerate(REVIEWERS, 1):
        review_input = _build_review_input(reviewer_name, reviewer_prompt, plan_text, feedback_history)

        log.info("Review round %d/%d (%s)", round_num, len(REVIEWERS), reviewer_name)
        try:
            result_text = await _invoke_reviewer(review_input, permission_mode, reviewer_timeout, claude_bin=claude_bin)
        except asyncio.TimeoutError:
            verdict = ReviewResult(action=Verdict.KILL, reason=f"timed out after {reviewer_timeout}s", demands=[])
            if on_review:
                on_review(reviewer_name, verdict)
            log.info("Plan killed by %s: %s", reviewer_name, verdict.reason)
            return False, f"Killed by {reviewer_name}: {verdict.reason}"
        verdict = _parse_verdict(result_text)

        if on_review:
            on_review(reviewer_name, verdict)

        if verdict.action == Verdict.KILL:
            log.info("Plan killed by %s: %s", reviewer_name, verdict.reason)
            return False, f"Killed by {reviewer_name}: {verdict.reason}"

        feedback_history.append((reviewer_name, result_text))

        if verdict.action == Verdict.REVISE:
            log.info("Plan revision demanded by %s", reviewer_name)
            demands_text = "\n".join(f"- {d}" for d in verdict.demands)
            plan_text = await brain.think(
                f"Revise this plan based on the reviewer's demands. "
                f"Output ONLY the revised plan in the same markdown format, nothing else.\n\n"
                f"Demands:\n{demands_text}\n\n"
                f"Original plan:\n{plan_text}"
            )

    return True, plan_text


INNOVATOR_MARKER = "<!-- foreman:innovator -->"
DRAFT_PREFIX = "draft-"


def _write_plans(
    plans_dir: Path,
    plans: list[tuple[str, str]],
    *,
    auto_activate: bool,
    slug_prefix: str = "",
) -> list[Path]:
    plans_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    prefix = "" if auto_activate else DRAFT_PREFIX

    for slug, content in plans:
        path = plans_dir / f"{prefix}{slug_prefix}{slug}.md"
        counter = 1
        while path.exists():
            path = plans_dir / f"{prefix}{slug_prefix}{slug}-{counter}.md"
            counter += 1

        marked_content = f"{INNOVATOR_MARKER}\n{content}\n"
        path.write_text(marked_content, encoding="utf-8")
        written.append(path)
        log.info("Wrote %s plan: %s", "active" if auto_activate else "draft", path.name)

    return written


async def _review_plans(
    plans: list[tuple[str, str]],
    brain: ForemanBrain,
    config: Config,
    skip_review: bool,
    on_review: _ReviewCallback | None,
    should_stop: Callable[[], bool] | None,
) -> list[tuple[str, str]]:
    if skip_review:
        return plans

    survivors: list[tuple[str, str]] = []
    for slug, plan_text in plans:
        if should_stop and should_stop():
            log.info("Innovator pausing for restart before reviewing %s", slug)
            break
        log.info("Reviewing plan: %s", slug)
        survived, final_text = await adversarial_review(
            plan_text, config.agents.permission_mode, brain,
            config.innovate.reviewer_timeout, on_review,
            claude_bin=config.claude_bin,
        )
        if survived:
            survivors.append((slug, final_text))
    return survivors


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def innovate(
    config: Config,
    categories: list[str] | None = None,
    max_ideas: int | None = None,
    web: bool = False,
    scope_path: str | None = None,
    skip_review: bool = False,
    on_review: _ReviewCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[Path]:
    foreman_dir = config.repo_root / ".foreman"
    lock_path = foreman_dir / "innovator" / "innovate.lock"
    if lock_path.exists():
        try:
            pid = int(lock_path.read_text().strip())
            if _is_pid_alive(pid):
                log.info("Another innovate is running (PID %d), skipping", pid)
                return []
        except (ValueError, FileNotFoundError):
            pass
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()))
    try:
        return await _innovate(
            config,
            categories=categories,
            max_ideas=max_ideas,
            web=web,
            scope_path=scope_path,
            skip_review=skip_review,
            on_review=on_review,
            should_stop=should_stop,
        )
    finally:
        lock_path.unlink(missing_ok=True)


async def _innovate(
    config: Config,
    categories: list[str] | None = None,
    max_ideas: int | None = None,
    web: bool = False,
    scope_path: str | None = None,
    skip_review: bool = False,
    on_review: _ReviewCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[Path]:
    effective_categories = categories or config.innovate.categories
    effective_max = max_ideas or config.innovate.max_ideas

    create_enabled = IdeaCategory.CREATE in effective_categories
    regular_categories = [c for c in effective_categories if c != IdeaCategory.CREATE]

    tools = BRAIN_TOOLS_WEB if web else BRAIN_TOOLS
    foreman_dir = config.repo_root / ".foreman"
    foreman_dir.mkdir(parents=True, exist_ok=True)

    brain = ForemanBrain(
        foreman_dir=foreman_dir / "innovator",
        allowed_tools=tools,
        permission_mode=config.agents.permission_mode,
        claude_bin=config.claude_bin,
    )

    written: list[Path] = []

    if regular_categories:
        runtime_context = _build_runtime_context(config)
        prompt = _build_explore_prompt(regular_categories, effective_max, scope_path, web, runtime_context)
        log.info("Exploring codebase — categories: %s", ", ".join(regular_categories))
        result = await brain.think(prompt)

        if should_stop and should_stop():
            log.info("Innovator pausing for restart after exploration")
            return written

        if "NO_FINDINGS" not in result:
            plans = _parse_draft_plans(result)[:effective_max]
            if not plans:
                log.warning("Brain returned output but no parseable plans")
            else:
                log.info("Shaped %d candidate ideas into plans", len(plans))
                survivors = await _review_plans(plans, brain, config, skip_review, on_review, should_stop)
                written += _write_plans(config.plans_dir, survivors, auto_activate=config.innovate.auto_activate)
        else:
            log.info("Exploration complete — no findings")

    if create_enabled:
        if should_stop and should_stop():
            log.info("Innovator pausing for restart before create exploration")
            return written

        prompt = _build_create_prompt(effective_max, web)
        log.info("Exploring new project ideas")
        result = await brain.think(prompt)

        if should_stop and should_stop():
            log.info("Innovator pausing for restart after create exploration")
            return written

        if "NO_FINDINGS" not in result:
            plans = _parse_draft_plans(result)[:effective_max]
            if not plans:
                log.warning("Brain returned output but no parseable create blueprints")
            else:
                log.info("Shaped %d new project blueprints", len(plans))
                survivors = await _review_plans(plans, brain, config, skip_review, on_review, should_stop)
                written += _write_plans(config.plans_dir, survivors, auto_activate=False, slug_prefix="create-")
        else:
            log.info("Create exploration complete — no findings")

    return written


_INNOVATOR_STATE_FILE = "innovator_state.json"
_ARCHITECTURE_REVIEW_FILE = "architecture-review.md"

_CLEANUP_EXTRA_CHECKS = """\
Additional cleanup checks:
- Which files are over 200 lines and should be split?
- What functions do more than one thing?
- What code is duplicated across modules?
- What imports or functions are unused?
- What was added by a previous plan but is no longer needed?
- Does the code still match CLAUDE.md rules?

Also check if CLAUDE.md still reflects the actual codebase:
- Are the module descriptions accurate after recent refactors?
- Are there new conventions that emerged and should be documented?
- Are there rules that no longer apply?

If CLAUDE.md is outdated, generate a plan to update it."""

_CLEANUP_OUTPUT_FORMAT = """\
For each issue worth fixing, output a plan in exactly this format, separated by --- on its own line:

# Short descriptive title

> **Depends on:**

## Problem

Describe the specific issue found, with evidence from the code.

## Solution

Describe the concrete changes to make.

## Scope

List the files that would need to change.

## Risk Assessment

What could go wrong with this change.

---

Output ONLY the plan documents separated by ---, nothing else. If you find no actionable refactoring tasks, output exactly: NO_FINDINGS"""

_TEST_PROMPT = """\
You are an autonomous test coverage agent. Your job is to analyze this codebase and identify what is missing from the test suite.

## Phase 1: Explore
Read the codebase, existing tests, and understand the critical logic paths.

## Phase 2: Analyze
- What core logic has no test coverage?
- What edge cases in the resolver/parser/coordination could break silently?
- What integration between modules is untested?
- What failure modes are never exercised?
- What happy paths are tested but critical error paths are not?

## Phase 3: Plan
Generate concrete test plans. Each plan should describe specific test cases to write as pytest tests.

Requirements:
- Each plan must be concrete — specify the exact test functions and what they verify
- Prefer tests that catch real bugs, not trivial coverage padding
- Focus on: parser, resolver, coordination, and module integration
- Generate at most 5 test plans

For each test plan, output a plan in exactly this format, separated by --- on its own line:

# Short descriptive title

> **Depends on:**

## Problem

What currently has no coverage and why that is risky.

## Solution

The specific tests to write, with function names and what each verifies.

## Scope

List the test files to create or modify.

## Risk Assessment

What could go wrong if these tests do not exist.

---

Output ONLY the plan documents separated by ---, nothing else. If the test coverage is already adequate, output exactly: NO_FINDINGS"""


def load_cycle_count(foreman_dir: Path) -> int:
    state_file = foreman_dir / _INNOVATOR_STATE_FILE
    if not state_file.exists():
        return 0
    try:
        return json.loads(state_file.read_text(encoding="utf-8")).get("cycle", 0)
    except (json.JSONDecodeError, OSError):
        return 0


def save_cycle_count(foreman_dir: Path, cycle: int) -> None:
    state_file = foreman_dir / _INNOVATOR_STATE_FILE
    state_file.write_text(json.dumps({"cycle": cycle}), encoding="utf-8")


def _build_cleanup_prompt(review_template: str, max_plans: int) -> str:
    return (
        f"{review_template}\n\n---\n\n"
        f"## Generate Refactoring Plans\n\n"
        f"Based on The Bad and The Ugly sections of your review above, generate concrete refactoring plans.\n\n"
        f"{_CLEANUP_EXTRA_CHECKS}\n\n"
        f"Generate at most {max_plans} plans.\n\n"
        f"{_CLEANUP_OUTPUT_FORMAT}"
    )


async def run_cleanup_cycle(
    config: Config,
    should_stop: Callable[[], bool] | None = None,
) -> list[Path]:
    review_file = config.repo_root / _ARCHITECTURE_REVIEW_FILE
    if not review_file.exists():
        log.warning("architecture-review.md not found at %s, skipping cleanup cycle", review_file)
        return []

    review_template = review_file.read_text(encoding="utf-8")
    prompt = _build_cleanup_prompt(review_template, config.innovate.max_ideas)

    foreman_dir = config.repo_root / ".foreman"
    foreman_dir.mkdir(parents=True, exist_ok=True)

    brain = ForemanBrain(
        foreman_dir=foreman_dir / "innovator",
        allowed_tools=BRAIN_TOOLS,
        permission_mode=config.agents.permission_mode,
        claude_bin=config.claude_bin,
    )

    log.info("Running cleanup cycle")
    result = await brain.think(prompt)

    if should_stop and should_stop():
        log.info("Innovator pausing for restart after cleanup exploration")
        return []

    if "NO_FINDINGS" in result:
        log.info("Cleanup cycle complete — no findings")
        return []

    plans = _parse_draft_plans(result)[: config.innovate.max_ideas]
    if not plans:
        log.warning("Cleanup brain returned output but no parseable plans")
        return []

    log.info("Cleanup cycle shaped %d candidate plans", len(plans))
    survivors = await _review_plans(plans, brain, config, config.innovate.skip_review, None, should_stop)
    return _write_plans(config.plans_dir, survivors, auto_activate=config.innovate.auto_activate, slug_prefix="cleanup-")


async def run_test_cycle(
    config: Config,
    should_stop: Callable[[], bool] | None = None,
) -> list[Path]:
    foreman_dir = config.repo_root / ".foreman"
    foreman_dir.mkdir(parents=True, exist_ok=True)

    brain = ForemanBrain(
        foreman_dir=foreman_dir / "innovator",
        allowed_tools=BRAIN_TOOLS,
        permission_mode=config.agents.permission_mode,
        claude_bin=config.claude_bin,
    )

    log.info("Running test generation cycle")
    result = await brain.think(_TEST_PROMPT)

    if should_stop and should_stop():
        log.info("Innovator pausing for restart after test exploration")
        return []

    if "NO_FINDINGS" in result:
        log.info("Test cycle complete — no findings")
        return []

    plans = _parse_draft_plans(result)[: config.innovate.max_ideas]
    if not plans:
        log.warning("Test brain returned output but no parseable plans")
        return []

    log.info("Test cycle shaped %d candidate plans", len(plans))
    survivors = await _review_plans(plans, brain, config, config.innovate.skip_review, None, should_stop)
    return _write_plans(config.plans_dir, survivors, auto_activate=config.innovate.auto_activate, slug_prefix="test-")


async def review_existing_drafts(
    config: Config,
    on_review: _ReviewCallback | None = None,
) -> tuple[list[Path], list[tuple[str, str]]]:
    foreman_dir = config.repo_root / ".foreman"
    foreman_dir.mkdir(parents=True, exist_ok=True)

    brain = ForemanBrain(
        foreman_dir=foreman_dir / "innovator",
        allowed_tools=BRAIN_TOOLS,
        permission_mode=config.agents.permission_mode,
        claude_bin=config.claude_bin,
    )

    draft_files = sorted(
        f for f in config.plans_dir.glob("*.md")
        if INNOVATOR_MARKER in f.read_text(encoding="utf-8")[:100]
    )
    if not draft_files:
        return [], []

    survivors: list[Path] = []
    killed: list[tuple[str, str]] = []

    for draft_path in draft_files:
        plan_text = draft_path.read_text(encoding="utf-8")
        log.info("Reviewing existing draft: %s", draft_path.name)

        survived, final_text = await adversarial_review(
            plan_text, config.agents.permission_mode, brain,
            config.innovate.reviewer_timeout, on_review,
            claude_bin=config.claude_bin,
        )

        if survived:
            draft_path.write_text(final_text + "\n", encoding="utf-8")
            survivors.append(draft_path)
        else:
            killed.append((draft_path.name, final_text))

    return survivors, killed
