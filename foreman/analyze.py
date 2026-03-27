"""Analyze a codebase with a specific focus and generate draft plan files."""

from __future__ import annotations

import asyncio
import logging
import re
from enum import StrEnum
from pathlib import Path

from foreman.brain import ForemanBrain
from foreman.config import AnalyzeConfig, Config

log = logging.getLogger(__name__)

BRAIN_TOOLS = "Read,Glob,Grep,Bash"
BRAIN_TOOLS_WEB = "Read,Glob,Grep,Bash,WebSearch,WebFetch"


class FocusArea(StrEnum):
    SECURITY = "security"
    PERFORMANCE = "performance"
    DEBT = "debt"
    DEPS = "deps"
    ARCHITECTURE = "architecture"
    TESTING = "testing"


FOCUS_DESCRIPTIONS: dict[FocusArea, str] = {
    FocusArea.SECURITY: "OWASP top 10, auth issues, injection, secrets in code, dependency CVEs",
    FocusArea.PERFORMANCE: "N+1 queries, blocking I/O, missing indexes, hot path bloat, memory leaks",
    FocusArea.DEBT: "Code smells, dead code, duplicated logic, outdated patterns, missing tests",
    FocusArea.DEPS: "Outdated dependencies, known CVEs, abandoned packages, license issues",
    FocusArea.ARCHITECTURE: "Coupling, circular deps, abstraction violations, missing separation of concerns",
    FocusArea.TESTING: "Missing test coverage, flaky tests, untested edge cases, integration gaps",
}


def _focus_description(focus: str) -> str:
    try:
        area = FocusArea(focus.lower())
        return f"{area.value}: {FOCUS_DESCRIPTIONS[area]}"
    except ValueError:
        return focus


_PLAN_SEPARATOR = re.compile(r"^---\s*$", re.MULTILINE)


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


def _build_prompt(focus: str, analyze_config: AnalyzeConfig, scope_path: str | None, web: bool) -> str:
    focus_desc = _focus_description(focus)

    scope_instruction = ""
    if scope_path:
        scope_instruction = f"\nFocus your analysis on the subtree: {scope_path}\n"

    web_instruction = ""
    if web:
        web_instruction = (
            "\nYou have web search available. Use it to check for known CVEs, "
            "latest package versions, and current best practices.\n"
        )

    return f"""\
You are analyzing a codebase for improvements.

Focus area: {focus_desc}
{scope_instruction}{web_instruction}
Instructions:
- Explore the codebase structure and read key files to understand the project
- Analyze the code through the lens of the focus area
- Only suggest improvements you are confident about
- Each suggestion must be concrete and actionable (not "consider adding tests")
- Each suggestion must be a self-contained plan that an implementation agent can execute
- Prefer small, focused plans over large refactoring plans
- Do not suggest changes that require business context you don't have
- Generate at most {analyze_config.max_plans} suggestions

For each improvement, output a markdown plan using exactly this format, separated by --- on its own line:

# Short descriptive title

> **Depends on:**

## Problem

Describe the specific issue found.

## Solution

Describe the concrete changes to make.

## Scope

List the files that would need to change.

---

Output ONLY the plan documents separated by ---, nothing else. If you find no actionable improvements, output exactly: NO_FINDINGS"""


async def run_analysis(
    config: Config,
    focus: str,
    web: bool = False,
    scope_path: str | None = None,
) -> list[Path]:
    tools = BRAIN_TOOLS_WEB if web else BRAIN_TOOLS
    foreman_dir = config.repo_root / ".foreman"
    foreman_dir.mkdir(parents=True, exist_ok=True)

    brain = ForemanBrain(
        foreman_dir=foreman_dir,
        allowed_tools=tools,
        permission_mode=config.agents.permission_mode,
    )

    prompt = _build_prompt(focus, config.analyze, scope_path, web)
    log.info("Starting analysis with focus: %s", focus)

    result = await brain.think(prompt)

    if "NO_FINDINGS" in result:
        log.info("Analysis complete — no findings")
        return []

    plans = _parse_draft_plans(result)
    if not plans:
        log.warning("Brain returned output but no parseable plans")
        return []

    written: list[Path] = []
    config.plans_dir.mkdir(parents=True, exist_ok=True)

    for slug, content in plans[: config.analyze.max_plans]:
        draft_path = config.plans_dir / f"draft-{slug}.md"
        counter = 1
        while draft_path.exists():
            draft_path = config.plans_dir / f"draft-{slug}-{counter}.md"
            counter += 1

        draft_path.write_text(content + "\n", encoding="utf-8")
        written.append(draft_path)
        log.info("Wrote draft plan: %s", draft_path.name)

    return written
