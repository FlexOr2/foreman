"""Configuration loading from foreman.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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


@dataclass
class Config:
    plans_dir: Path = field(default_factory=lambda: Path("plans"))
    coordination_db: Path = field(default_factory=lambda: Path(".foreman/coordination.db"))
    log_dir: Path = field(default_factory=lambda: Path(".foreman/logs"))
    worktree_dir: Path = field(default_factory=lambda: Path(".foreman/worktrees"))
    scripts_dir: Path = field(default_factory=lambda: Path(".foreman/scripts"))
    branch_prefix: str = "feat/"

    prompts: dict[str, str] = field(default_factory=lambda: {
        "implementation": "plans/prompt-implementation.md",
        "review": "plans/prompt-review.md",
        "fix": "plans/prompt-fix.md",
    })

    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    agents: AgentConfig = field(default_factory=AgentConfig)

    allowed_tools: dict[str, str] = field(default_factory=lambda: {
        "review": "Read,Glob,Grep,Bash,Write",
    })

    plan_overrides: dict[str, dict] = field(default_factory=dict)

    repo_root: Path = field(default_factory=lambda: Path.cwd())

    def resolve_paths(self) -> None:
        """Resolve all relative paths against repo_root."""
        self.plans_dir = self.repo_root / self.plans_dir
        self.coordination_db = self.repo_root / self.coordination_db
        self.log_dir = self.repo_root / self.log_dir
        self.worktree_dir = self.repo_root / self.worktree_dir
        self.scripts_dir = self.repo_root / self.scripts_dir

    def ensure_dirs(self) -> None:
        """Create required directories."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.worktree_dir.mkdir(parents=True, exist_ok=True)
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        self.coordination_db.parent.mkdir(parents=True, exist_ok=True)

    def get_timeout(self, plan_name: str, agent_type: str) -> int:
        """Get timeout for a plan, checking overrides first."""
        override = self.plan_overrides.get(plan_name, {})
        if "timeout" in override:
            return override["timeout"]
        return getattr(self.timeouts, agent_type, self.timeouts.implementation)


def load_config(repo_root: Path | None = None) -> Config:
    """Load config from foreman.toml, falling back to defaults."""
    if repo_root is None:
        repo_root = Path.cwd()

    config_path = repo_root / "foreman.toml"
    config = Config(repo_root=repo_root)

    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

        foreman = raw.get("foreman", {})

        for key in ("plans_dir", "coordination_db", "log_dir", "worktree_dir",
                     "scripts_dir", "branch_prefix"):
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

        if "allowed_tools" in foreman:
            config.allowed_tools.update(foreman["allowed_tools"])

        if "plans" in foreman:
            for plan_name, overrides in foreman["plans"].items():
                if isinstance(overrides, dict):
                    config.plan_overrides[plan_name] = overrides

    config.resolve_paths()
    return config
