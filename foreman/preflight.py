"""Prerequisite checks before starting Foreman."""

from __future__ import annotations

import glob
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console

VSCODE_CLAUDE_GLOB = str(
    Path.home() / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"
)

CLAUDE_SEARCH_PATHS = [
    Path.home() / ".claude" / "local" / "claude",
    Path.home() / ".local" / "bin" / "claude",
]

TMUX_INSTALL_HINTS = {
    "linux": "sudo apt install tmux  # or: sudo pacman -S tmux / sudo dnf install tmux",
    "darwin": "brew install tmux",
}

CLAUDE_INSTALL_HINTS = {
    "linux": "npm install -g @anthropic-ai/claude-code",
    "darwin": "npm install -g @anthropic-ai/claude-code",
}


def find_claude() -> str | None:
    if shutil.which("claude"):
        return "claude"

    for path in CLAUDE_SEARCH_PATHS:
        if path.is_file() and _is_executable(path):
            return str(path)

    matches = sorted(glob.glob(VSCODE_CLAUDE_GLOB), reverse=True)
    for match in matches:
        path = Path(match)
        if path.is_file() and _is_executable(path):
            return str(path)

    return None


def _is_executable(path: Path) -> bool:
    import os
    return os.access(path, os.X_OK)


def check_prerequisites(console: Console | None = None) -> bool:
    if console is None:
        console = Console()

    problems = []

    if not shutil.which("git"):
        problems.append(("git", "Foreman uses git worktrees for agent isolation.", "sudo apt install git"))

    if not shutil.which("tmux"):
        hint_key = "darwin" if sys.platform == "darwin" else "linux"
        problems.append(("tmux", "Foreman uses tmux to run agents in visible terminal windows.", TMUX_INSTALL_HINTS[hint_key]))

    claude_path = find_claude()
    if not claude_path:
        hint_key = "darwin" if sys.platform == "darwin" else "linux"
        problems.append(("claude", "Foreman spawns Claude Code CLI sessions as worker agents.", CLAUDE_INSTALL_HINTS[hint_key]))
    else:
        import foreman.config as config_module
        config_module.CLAUDE_BIN = claude_path
        if claude_path != "claude":
            console.print(f"  Found claude at [dim]{claude_path}[/dim]")

    if not problems:
        return True

    console.print("[red bold]Missing prerequisites:[/red bold]\n")
    for tool, reason, hint in problems:
        console.print(f"  [bold]{tool}[/bold] — {reason}")
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
