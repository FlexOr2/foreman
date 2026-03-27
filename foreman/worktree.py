"""Create and remove git worktrees for agents."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class WorktreeInfo:
    path: Path
    branch: str
    plan_name: str


async def _run_git(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def create_worktree(
    plan_name: str,
    branch_prefix: str,
    worktree_dir: Path,
    repo_root: Path,
) -> tuple[Path, str]:
    """Create a worktree branching from current HEAD.

    Returns (worktree_path, branch_name).
    """
    branch = f"{branch_prefix}{plan_name}"
    worktree_path = worktree_dir / plan_name

    if worktree_path.exists():
        log.warning("Worktree already exists at %s, reusing", worktree_path)
        return worktree_path, branch

    rc, _, stderr = await _run_git(
        "worktree", "add", str(worktree_path), "-b", branch,
        cwd=repo_root,
    )
    if rc != 0:
        # Branch might already exist (from a previous interrupted run)
        if "already exists" in stderr:
            log.warning("Branch %s already exists, checking out", branch)
            rc, _, stderr = await _run_git(
                "worktree", "add", str(worktree_path), branch,
                cwd=repo_root,
            )
            if rc != 0:
                raise RuntimeError(f"Failed to create worktree: {stderr}")
        else:
            raise RuntimeError(f"Failed to create worktree: {stderr}")

    log.info("Created worktree at %s on branch %s", worktree_path, branch)
    return worktree_path, branch


async def remove_worktree(
    plan_name: str,
    worktree_dir: Path,
    branch_prefix: str,
    repo_root: Path,
) -> None:
    """Remove a worktree and delete its branch."""
    worktree_path = worktree_dir / plan_name
    branch = f"{branch_prefix}{plan_name}"

    if worktree_path.exists():
        rc, _, stderr = await _run_git(
            "worktree", "remove", str(worktree_path), "--force",
            cwd=repo_root,
        )
        if rc != 0:
            log.warning("Failed to remove worktree %s: %s", worktree_path, stderr)
        else:
            log.info("Removed worktree at %s", worktree_path)

    # Delete the feature branch
    rc, _, stderr = await _run_git(
        "branch", "-D", branch,
        cwd=repo_root,
    )
    if rc != 0:
        log.warning("Failed to delete branch %s: %s", branch, stderr.strip())
    else:
        log.info("Deleted branch %s", branch)


async def merge_branch(
    branch: str,
    repo_root: Path,
) -> tuple[bool, str]:
    """Merge a branch into current HEAD. Returns (success, output)."""
    rc, stdout, stderr = await _run_git("merge", branch, cwd=repo_root)
    if rc != 0:
        return False, stderr
    return True, stdout


async def abort_merge(repo_root: Path) -> None:
    """Abort an in-progress merge."""
    await _run_git("merge", "--abort", cwd=repo_root)


async def list_worktrees(worktree_dir: Path) -> list[WorktreeInfo]:
    """List active foreman worktrees."""
    if not worktree_dir.exists():
        return []

    worktrees = []
    for path in sorted(worktree_dir.iterdir()):
        if not path.is_dir():
            continue
        # Read HEAD to get branch
        head_file = path / ".git"
        if not head_file.exists():
            continue

        rc, stdout, _ = await _run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)
        if rc == 0:
            branch = stdout.strip()
            worktrees.append(WorktreeInfo(
                path=path,
                branch=branch,
                plan_name=path.name,
            ))

    return worktrees
