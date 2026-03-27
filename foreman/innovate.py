"""Autonomous innovation discovery with adversarial review pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import foreman.config as _config
from foreman.brain import ForemanBrain
from foreman.config import Config

log = logging.getLogger(__name__)

BRAIN_TOOLS = "Read,Glob,Grep,Bash"
BRAIN_TOOLS_WEB = "Read,Glob,Grep,Bash,WebSearch,WebFetch"


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


async def _invoke_reviewer(prompt: str, permission_mode: str) -> str:
    cmd = [
        _config.CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", permission_mode,
        "--allowed-tools", "",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"Reviewer failed (rc={proc.returncode}): {stderr.decode().strip()}")

    response = json.loads(stdout.decode())
    return response.get("result", "")


def _build_explore_prompt(
    categories: list[str],
    max_ideas: int,
    scope_path: str | None,
    web: bool,
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

    return f"""\
You are an autonomous innovation agent. Your job is to deeply analyze a codebase and discover non-obvious improvement opportunities.

## Phase 1: Explore
Read the codebase structure, dependencies, recent git log, README, CLAUDE.md, config files, and entry points. Understand what this project does, how it works, and what its current state is.
{scope_instruction}{web_instruction}
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
    on_review: _ReviewCallback | None = None,
) -> tuple[bool, str]:
    feedback_history: list[tuple[str, str]] = []

    for round_num, (reviewer_name, reviewer_prompt) in enumerate(REVIEWERS, 1):
        review_input = _build_review_input(reviewer_name, reviewer_prompt, plan_text, feedback_history)

        log.info("Review round %d/%d (%s)", round_num, len(REVIEWERS), reviewer_name)
        result_text = await _invoke_reviewer(review_input, permission_mode)
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


def _write_plans(plans_dir: Path, plans: list[tuple[str, str]], *, auto_activate: bool) -> list[Path]:
    plans_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    prefix = "" if auto_activate else "draft-"

    for slug, content in plans:
        path = plans_dir / f"{prefix}{slug}.md"
        counter = 1
        while path.exists():
            path = plans_dir / f"{prefix}{slug}-{counter}.md"
            counter += 1

        marked_content = f"{INNOVATOR_MARKER}\n{content}\n"
        path.write_text(marked_content, encoding="utf-8")
        written.append(path)
        log.info("Wrote %s plan: %s", "active" if auto_activate else "draft", path.name)

    return written


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
    effective_categories = categories or config.innovate.categories
    effective_max = max_ideas or config.innovate.max_ideas

    tools = BRAIN_TOOLS_WEB if web else BRAIN_TOOLS
    foreman_dir = config.repo_root / ".foreman"
    foreman_dir.mkdir(parents=True, exist_ok=True)

    brain = ForemanBrain(
        foreman_dir=foreman_dir,
        allowed_tools=tools,
        permission_mode=config.agents.permission_mode,
    )

    prompt = _build_explore_prompt(effective_categories, effective_max, scope_path, web)
    log.info("Exploring codebase — categories: %s", ", ".join(effective_categories))

    result = await brain.think(prompt)

    if should_stop and should_stop():
        log.info("Innovator pausing for restart after exploration")
        return []

    if "NO_FINDINGS" in result:
        log.info("Exploration complete — no findings")
        return []

    plans = _parse_draft_plans(result)
    if not plans:
        log.warning("Brain returned output but no parseable plans")
        return []

    plans = plans[:effective_max]
    log.info("Shaped %d candidate ideas into plans", len(plans))

    if skip_review:
        return _write_plans(config.plans_dir, plans, auto_activate=config.innovate.auto_activate)

    survivors: list[tuple[str, str]] = []
    for slug, plan_text in plans:
        if should_stop and should_stop():
            log.info("Innovator pausing for restart before reviewing %s", slug)
            break

        log.info("Reviewing plan: %s", slug)
        survived, final_text = await adversarial_review(
            plan_text, config.agents.permission_mode, brain, on_review,
        )
        if survived:
            survivors.append((slug, final_text))

    return _write_plans(config.plans_dir, survivors, auto_activate=config.innovate.auto_activate)


async def review_existing_drafts(
    config: Config,
    on_review: _ReviewCallback | None = None,
) -> tuple[list[Path], list[tuple[str, str]]]:
    foreman_dir = config.repo_root / ".foreman"
    foreman_dir.mkdir(parents=True, exist_ok=True)

    brain = ForemanBrain(
        foreman_dir=foreman_dir,
        allowed_tools=BRAIN_TOOLS,
        permission_mode=config.agents.permission_mode,
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
            plan_text, config.agents.permission_mode, brain, on_review,
        )

        if survived:
            draft_path.write_text(final_text + "\n", encoding="utf-8")
            survivors.append(draft_path)
        else:
            killed.append((draft_path.name, final_text))

    return survivors, killed
