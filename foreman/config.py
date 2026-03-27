"""Configuration loading from .foreman/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from foreman.coordination import AgentType

CLAUDE_BIN = "claude"

PROCESS_POLL_INTERVAL = 2
SQLITE_BUSY_TIMEOUT_MS = 5000

FOREMAN_DIR = ".foreman"


@dataclass
class TimeoutConfig:
    implementation: int = 1800
    review: int = 600
    stuck_threshold: int = 300


@dataclass
class AgentConfig:
    max_parallel_workers: int = 3
    max_parallel_reviews: int = 2
    model: str = "opus"
    auto_review: bool = True
    max_review_retries: int = 2
    permission_mode: str = "dontAsk"


ALL_IDEA_CATEGORIES = [
    "risk", "performance", "architecture", "debt", "dx",
    "features", "competitive", "delight", "moonshots",
]


@dataclass
class InnovateConfig:
    enabled: bool = False
    max_drafts: int = 5
    interval: int = 600
    max_ideas: int = 10
    skip_review: bool = False
    categories: list[str] = field(default_factory=lambda: list(ALL_IDEA_CATEGORIES))


@dataclass
class Config:
    plans_dir: Path = field(default_factory=lambda: Path("plans"))
    prompts_dir: Path = field(default_factory=lambda: Path(f"{FOREMAN_DIR}/prompts"))
    coordination_db: Path = field(default_factory=lambda: Path(f"{FOREMAN_DIR}/coordination.db"))
    log_dir: Path = field(default_factory=lambda: Path(f"{FOREMAN_DIR}/logs"))
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

    allowed_tools: dict[str, str] = field(default_factory=lambda: {
        AgentType.REVIEW: "Read,Glob,Grep,Bash,Write",
    })

    plan_overrides: dict[str, dict] = field(default_factory=dict)

    repo_root: Path = field(default_factory=lambda: Path.cwd())

    def resolve_paths(self) -> None:
        self.plans_dir = self.repo_root / self.plans_dir
        self.prompts_dir = self.repo_root / self.prompts_dir
        self.coordination_db = self.repo_root / self.coordination_db
        self.log_dir = self.repo_root / self.log_dir
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


def load_config(repo_root: Path | None = None) -> Config:
    if repo_root is None:
        repo_root = Path.cwd()

    config_path = repo_root / FOREMAN_DIR / "config.toml"
    config = Config(repo_root=repo_root)

    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

        foreman = raw.get("foreman", {})

        for key in ("plans_dir", "prompts_dir", "coordination_db", "log_dir",
                     "worktree_dir", "scripts_dir", "branch_prefix"):
            if key in foreman:
                value = foreman[key]
                if key != "branch_prefix":
                    value = Path(value)
                setattr(config, key, value)

        if "prompts" in foreman:
            config.prompts.update(foreman["prompts"])

        if "timeouts" in foreman:
            for key, value in foreman["timeouts"].items():
                if hasattr(config.timeouts, key):
                    setattr(config.timeouts, key, value)

        if "agents" in foreman:
            for key, value in foreman["agents"].items():
                if hasattr(config.agents, key):
                    setattr(config.agents, key, value)

        if "innovate" in foreman:
            for key, value in foreman["innovate"].items():
                if hasattr(config.innovate, key):
                    setattr(config.innovate, key, value)

        if "allowed_tools" in foreman:
            config.allowed_tools.update(foreman["allowed_tools"])

        if "plans" in foreman:
            for plan_name, overrides in foreman["plans"].items():
                if isinstance(overrides, dict):
                    config.plan_overrides[plan_name] = overrides

    config.resolve_paths()
    return config
