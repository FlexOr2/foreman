"""Parse markdown plan files and extract structured metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_DEPENDS_RE = re.compile(
    r">\s*\*\*Depends\s+on:\s*(.*?)\*\*",
    re.IGNORECASE,
)
_PHASE_RE = re.compile(r"^###\s+Phase\s+\d+", re.MULTILINE)
_SKIP_PREFIXES = ("draft-", "prompt-")


@dataclass
class Plan:
    name: str
    file_path: Path
    depends_on: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)


def is_plan_file(path: Path) -> bool:
    return (
        path.suffix == ".md"
        and not any(path.name.startswith(p) for p in _SKIP_PREFIXES)
    )


def parse_plan(file_path: Path) -> Plan:
    content = file_path.read_text(encoding="utf-8")

    depends_on: list[str] = []
    match = _DEPENDS_RE.search(content)
    if match:
        raw = match.group(1).strip()
        depends_on = [dep.strip() for dep in raw.split(",") if dep.strip()]

    phases = [m.group().strip("# ") for m in _PHASE_RE.finditer(content)]

    return Plan(
        name=file_path.stem,
        file_path=file_path,
        depends_on=depends_on,
        phases=phases,
    )


def load_plans(plans_dir: Path) -> list[Plan]:
    if not plans_dir.is_dir():
        return []

    return [
        parse_plan(md_file)
        for md_file in sorted(plans_dir.glob("*.md"))
        if is_plan_file(md_file)
    ]
