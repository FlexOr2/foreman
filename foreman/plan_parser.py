"""Parse markdown plan files and extract structured metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_DEPENDS_RE = re.compile(
    r">\s*\*\*Depends\s+on:\s*(.*?)\*\*",
    re.IGNORECASE,
)
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_SKIP_PREFIXES = ("draft-",)


def is_valid_plan_name(name: str) -> bool:
    return bool(_VALID_NAME_RE.match(name))


class InvalidPlanNameError(ValueError):
    def __init__(self, name: str, file_path: Path) -> None:
        self.name = name
        self.file_path = file_path
        super().__init__(
            f"Invalid plan name {name!r} (from {file_path.name}): "
            "names must contain only alphanumerics, hyphens, dots, and underscores, "
            "and must not start with a special character"
        )


@dataclass
class Plan:
    name: str
    file_path: Path
    depends_on: list[str] = field(default_factory=list)


def is_plan_file(path: Path) -> bool:
    return (
        path.suffix == ".md"
        and not any(path.name.startswith(p) for p in _SKIP_PREFIXES)
    )


def parse_plan(file_path: Path) -> Plan:
    name = file_path.stem
    if not _VALID_NAME_RE.match(name):
        raise InvalidPlanNameError(name, file_path)

    content = file_path.read_text(encoding="utf-8")

    depends_on: list[str] = []
    match = _DEPENDS_RE.search(content)
    if match:
        raw = match.group(1).strip()
        depends_on = [dep.strip() for dep in raw.split(",") if dep.strip()]

    return Plan(
        name=name,
        file_path=file_path,
        depends_on=depends_on,
    )


def load_plans(plans_dir: Path) -> list[Plan]:
    if not plans_dir.is_dir():
        return []

    return [
        parse_plan(md_file)
        for md_file in sorted(plans_dir.glob("*.md"))
        if is_plan_file(md_file)
    ]
