"""Prerequisite checks before starting Foreman."""

from __future__ import annotations

import shutil
import subprocess
import sys

from rich.console import Console

REQUIRED_TOOLS = {
    "tmux": (
        "Foreman uses tmux to run agents in visible terminal windows.",
        {
            "linux": "sudo apt install tmux  # or: sudo pacman -S tmux",
            "darwin": "brew install tmux",
        },
    ),
    "claude": (
        "Foreman spawns Claude Code CLI sessions as worker agents.",
        {
            "linux": "npm install -g @anthropic-ai/claude-code",
            "darwin": "npm install -g @anthropic-ai/claude-code",
        },
    ),
    "git": (
        "Foreman uses git worktrees for agent isolation.",
        {
            "linux": "sudo apt install git",
            "darwin": "xcode-select --install",
        },
    ),
}


def check_prerequisites(console: Console | None = None) -> bool:
    if console is None:
        console = Console()

    missing = []
    for tool, (reason, install_hints) in REQUIRED_TOOLS.items():
        if shutil.which(tool) is None:
            missing.append((tool, reason, install_hints))

    if not missing:
        return True

    console.print("[red bold]Missing prerequisites:[/red bold]\n")
    for tool, reason, install_hints in missing:
        console.print(f"  [bold]{tool}[/bold] — {reason}")
        platform = sys.platform
        hint_key = "darwin" if platform == "darwin" else "linux"
        hint = install_hints.get(hint_key, install_hints.get("linux", ""))
        if hint:
            console.print(f"    Install: [cyan]{hint}[/cyan]")
        console.print()

    return False


def check_git_repo(path: str = ".") -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
