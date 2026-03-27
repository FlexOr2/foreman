"""Parse markdown plan files and extract structured metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Plan:
    name: str
    file_path: Path
    depends_on: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    effort: str | None = None

    @property
    def has_dependencies(self) -> bool:
        return len(self.depends_on) > 0


_DEPENDS_RE = re.compile(
    r">\s*\*\*Depends\s+on:\s*(.*?)\*\*",
    re.IGNORECASE,
)
_PHASE_RE = re.compile(r"^###\s+Phase\s+\d+", re.MULTILINE)


def parse_plan(file_path: Path) -> Plan:
    """Parse a single plan file and extract metadata."""
    content = file_path.read_text(encoding="utf-8")
    name = file_path.stem

    depends_on: list[str] = []
    match = _DEPENDS_RE.search(content)
    if match:
        raw = match.group(1).strip()
        depends_on = [dep.strip() for dep in raw.split(",") if dep.strip()]

    phases = [m.group().strip("# ") for m in _PHASE_RE.finditer(content)]

    return Plan(
        name=name,
        file_path=file_path,
        depends_on=depends_on,
        phases=phases,
    )


def load_plans(plans_dir: Path) -> list[Plan]:
    """Load all non-draft plan files from the plans directory."""
    if not plans_dir.is_dir():
        return []

    plans = []
    for md_file in sorted(plans_dir.glob("*.md")):
        if md_file.name.startswith("draft-"):
            continue
        if md_file.name.startswith("prompt-"):
            continue
        plans.append(parse_plan(md_file))

    return plans
