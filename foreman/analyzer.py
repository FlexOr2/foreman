"""Background analyzer that generates draft plan files via the brain."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from foreman.brain import ForemanBrain
from foreman.config import AnalyzerFocus, Config

log = logging.getLogger(__name__)

FILENAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


def _sanitize_name(raw: str) -> str | None:
    name = raw.strip().removeprefix("draft-").removesuffix(".md").lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name if name and FILENAME_PATTERN.match(name) else None


def _existing_plan_names(plans_dir: Path) -> list[str]:
    return [f.stem.removeprefix("draft-") for f in plans_dir.glob("*.md")]


LANG_TAGS = {"markdown", "md", ""}


def _parse_draft_blocks(response: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    parts = response.split("```")
    for i in range(1, len(parts), 2):
        lines = parts[i].strip().splitlines()
        if len(lines) < 2:
            continue

        start = 0
        if lines[0].strip().lower() in LANG_TAGS:
            start = 1

        if start >= len(lines):
            continue

        name = _sanitize_name(lines[start])
        content = "\n".join(lines[start + 1:]).strip()
        if name and content:
            blocks.append((name, content))
    return blocks


async def run_analysis(
    focus: AnalyzerFocus,
    config: Config,
    brain: ForemanBrain,
    completed_plans: set[str] | None = None,
) -> list[Path]:
    existing = _existing_plan_names(config.plans_dir)
    completed = completed_plans or set()
    skip_names = sorted(set(existing) | completed)
    skip_list = ", ".join(skip_names) if skip_names else "(none)"

    prompt = (
        f"Focus area: {focus.prompt}\n\n"
        f"Existing and completed plans (do not duplicate): {skip_list}\n\n"
        "Read the codebase and generate 1-3 actionable plan files as markdown. "
        "Each plan should be a concrete, implementable task. "
        "Output each plan as a fenced code block with the filename as the first line "
        "inside the block (e.g. ```\\nmy-plan-name.md\\n...content...\\n```)."
    )

    log.info("Running analysis: %s", focus.name)

    try:
        response = await brain.think(prompt)
    except Exception:
        log.error("Analysis failed for focus '%s'", focus.name, exc_info=True)
        return []

    blocks = _parse_draft_blocks(response)
    created: list[Path] = []

    for name, content in blocks:
        if name in existing:
            log.debug("Skipping duplicate plan name: %s", name)
            continue
        draft_path = config.plans_dir / f"draft-{name}.md"
        if draft_path.exists():
            log.debug("Draft already exists: %s", draft_path.name)
            continue
        await asyncio.to_thread(draft_path.write_text, content + "\n")
        existing.append(name)
        created.append(draft_path)
        log.info("Created draft plan: %s", draft_path.name)

    return created
