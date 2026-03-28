"""Configuration loading from .foreman/config.toml."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import get_type_hints

from foreman.coordination import AgentType, StuckAction

CLAUDE_BIN = "claude"

RESTART_EXIT_CODE = 75
PROCESS_POLL_INTERVAL = 2
SQLITE_BUSY_TIMEOUT_MS = 5000

FOREMAN_DIR = ".foreman"
RELOAD_CONFIG_MARKER = f"{FOREMAN_DIR}/reload_config"


@dataclass
class TimeoutConfig:
    # Hard timeout per agent type in seconds. 0 = no hard timeout.
    implementation: int = 1800
    review: int = 900
    stuck_threshold: int = 300
    brain_timeout: int = 900


@dataclass
class AgentConfig:
    max_parallel_workers: int = 3
    max_parallel_reviews: int = 2
    model: str = "opus"
    auto_review: bool = True
    max_review_retries: int = 2
    permission_mode: str = "dontAsk"
    stuck_action: StuckAction = StuckAction.WARN


ALL_IDEA_CATEGORIES = [
    "risk", "performance", "architecture", "debt", "dx",
    "features", "competitive", "delight", "moonshots",
]


@dataclass
class InnovateConfig:
    enabled: bool = False
    auto_activate: bool = False
    max_drafts: int = 5
    interval: int = 600
    max_ideas: int = 10
    skip_review: bool = False
    reviewer_timeout: int = 600
    categories: list[str] = field(default_factory=lambda: list(ALL_IDEA_CATEGORIES))


@dataclass
class Config:
    plans_dir: Path = field(default_factory=lambda: Path("plans"))
    prompts_dir: Path = field(default_factory=lambda: Path(f"{FOREMAN_DIR}/prompts"))
    coordination_db: Path = field(default_factory=lambda: Path(f"{FOREMAN_DIR}/coordination.db"))
    log_dir: Path = field(default_factory=lambda: Path(f"{FOREMAN_DIR}/logs"))
    log_file: Path = field(default_factory=lambda: Path(f"{FOREMAN_DIR}/foreman.log"))
    worktree_dir: Path = field(default_factory=lambda: Path(f"{FOREMAN_DIR}/worktrees"))
    scripts_dir: Path = field(default_factory=lambda: Path(f"{FOREMAN_DIR}/scripts"))
    branch_prefix: str = "feat/"

    prompts: dict[str, str] = field(default_factory=lambda: {
        AgentType.IMPLEMENTATION: "prompt-implementation.md",
        AgentType.REVIEW: "prompt-review.md",
        AgentType.FIX: "prompt-fix.md",
    })

    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    agents: AgentConfig = field(default_factory=AgentConfig)
    innovate: InnovateConfig = field(default_factory=InnovateConfig)

    allowed_tools: dict[str, str] = field(default_factory=dict)

    auto_restart: bool = True
    web_port: int = 8765

    plan_overrides: dict[str, dict] = field(default_factory=dict)

    repo_root: Path = field(default_factory=lambda: Path.cwd())

    def resolve_paths(self) -> None:
        self.plans_dir = self.repo_root / self.plans_dir
        self.prompts_dir = self.repo_root / self.prompts_dir
        self.coordination_db = self.repo_root / self.coordination_db
        self.log_dir = self.repo_root / self.log_dir
        self.log_file = self.repo_root / self.log_file
        self.worktree_dir = self.repo_root / self.worktree_dir
        self.scripts_dir = self.repo_root / self.scripts_dir

    def ensure_dirs(self) -> None:
        for d in (self.prompts_dir, self.log_dir, self.worktree_dir,
                  self.scripts_dir, self.coordination_db.parent):
            d.mkdir(parents=True, exist_ok=True)

    def get_prompt_path(self, agent_type: AgentType) -> Path:
        return self.prompts_dir / self.prompts.get(agent_type, "")

    def get_timeout(self, plan_name: str, agent_type: AgentType) -> int:
        override = self.plan_overrides.get(plan_name, {})
        if "timeout" in override:
            return override["timeout"]
        return getattr(self.timeouts, agent_type.value, self.timeouts.implementation)


log = logging.getLogger(__name__)

KNOWN_TOP_LEVEL_KEYS = {
    "plans_dir", "prompts_dir", "coordination_db", "log_dir",
    "log_file", "worktree_dir", "scripts_dir", "branch_prefix",
    "auto_restart", "web_port", "prompts", "timeouts", "agents", "innovate",
    "allowed_tools", "plans",
}


def _apply_section(target: object, section_name: str, data: dict) -> None:
    known_keys = {f.name for f in fields(target)}
    for key, value in data.items():
        if key in known_keys:
            setattr(target, key, value)
        else:
            log.warning("Unknown config key: foreman.%s.%s", section_name, key)


def _validate_enum_fields(target: object, section_name: str) -> None:
    hints = get_type_hints(type(target))
    for f in fields(target):
        hint = hints.get(f.name)
        if isinstance(hint, type) and issubclass(hint, StrEnum):
            value = getattr(target, f.name)
            if not isinstance(value, hint):
                try:
                    setattr(target, f.name, hint(value))
                except ValueError:
                    valid = [e.value for e in hint]
                    log.warning(
                        "Invalid value for foreman.%s.%s: %r (valid: %s)",
                        section_name, f.name, value, ", ".join(valid),
                    )
                    setattr(target, f.name, f.default)


def load_config(repo_root: Path | None = None) -> Config:
    if repo_root is None:
        repo_root = Path.cwd()

    config_path = repo_root / FOREMAN_DIR / "config.toml"
    config = Config(repo_root=repo_root)

    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

        foreman = raw.get("foreman", {})

        for key in foreman:
            if key not in KNOWN_TOP_LEVEL_KEYS:
                log.warning("Unknown config key: foreman.%s", key)

        for key in ("plans_dir", "prompts_dir", "coordination_db", "log_dir",
                     "log_file", "worktree_dir", "scripts_dir", "branch_prefix",
                     "auto_restart", "web_port"):
            if key in foreman:
                value = foreman[key]
                if key not in ("branch_prefix", "auto_restart", "web_port"):
                    value = Path(value)
                setattr(config, key, value)

        if "prompts" in foreman:
            config.prompts.update(foreman["prompts"])

        if "timeouts" in foreman:
            _apply_section(config.timeouts, "timeouts", foreman["timeouts"])

        if "agents" in foreman:
            _apply_section(config.agents, "agents", foreman["agents"])
            _validate_enum_fields(config.agents, "agents")

        if "innovate" in foreman:
            _apply_section(config.innovate, "innovate", foreman["innovate"])

        if "allowed_tools" in foreman:
            config.allowed_tools.update(foreman["allowed_tools"])

        if "plans" in foreman:
            for plan_name, overrides in foreman["plans"].items():
                if isinstance(overrides, dict):
                    config.plan_overrides[plan_name] = overrides

    config.resolve_paths()
    return config


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"Cannot serialize {type(v)} to TOML")


def _write_toml_section(data: dict, path: list[str], lines: list[str]) -> None:
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")
    for key, value in data.items():
        if isinstance(value, dict):
            section_path = path + [key]
            lines.append("")
            lines.append(f"[{'.'.join(section_path)}]")
            _write_toml_section(value, section_path, lines)


def save_config(config: Config) -> None:
    config_path = config.repo_root / FOREMAN_DIR / "config.toml"
    existing: dict = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            existing = tomllib.load(f)

    foreman = existing.setdefault("foreman", {})
    foreman["auto_restart"] = config.auto_restart

    agents = foreman.setdefault("agents", {})
    agents["max_parallel_workers"] = config.agents.max_parallel_workers
    agents["max_parallel_reviews"] = config.agents.max_parallel_reviews
    agents["model"] = config.agents.model

    timeouts = foreman.setdefault("timeouts", {})
    timeouts["implementation"] = config.timeouts.implementation
    timeouts["review"] = config.timeouts.review
    timeouts["stuck_threshold"] = config.timeouts.stuck_threshold

    innovate = foreman.setdefault("innovate", {})
    innovate["enabled"] = config.innovate.enabled
    innovate["auto_activate"] = config.innovate.auto_activate
    innovate["max_drafts"] = config.innovate.max_drafts
    innovate["interval"] = config.innovate.interval
    innovate["skip_review"] = config.innovate.skip_review
    innovate["categories"] = config.innovate.categories

    lines: list[str] = []
    _write_toml_section(existing, [], lines)
    config_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def apply_config_update(target: Config, updated: Config) -> None:
    target.auto_restart = updated.auto_restart
    target.agents.max_parallel_workers = updated.agents.max_parallel_workers
    target.agents.max_parallel_reviews = updated.agents.max_parallel_reviews
    target.agents.model = updated.agents.model
    target.timeouts.implementation = updated.timeouts.implementation
    target.timeouts.review = updated.timeouts.review
    target.timeouts.stuck_threshold = updated.timeouts.stuck_threshold
    target.innovate.enabled = updated.innovate.enabled
    target.innovate.auto_activate = updated.innovate.auto_activate
    target.innovate.max_drafts = updated.innovate.max_drafts
    target.innovate.interval = updated.innovate.interval
    target.innovate.skip_review = updated.innovate.skip_review
    target.innovate.categories = updated.innovate.categories
