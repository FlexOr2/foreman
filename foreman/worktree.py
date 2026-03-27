"""Create and remove git worktrees for agents."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from foreman.config import Config

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


async def create_worktree(plan_name: str, config: Config) -> tuple[Path, str]:
    branch = f"{config.branch_prefix}{plan_name}"
    worktree_path = config.worktree_dir / plan_name

    rc, _, stderr = await _run_git(
        "worktree", "add", str(worktree_path), "-b", branch,
        cwd=config.repo_root,
    )
    if rc != 0:
        if "already exists" in stderr:
            log.warning("Branch %s already exists, checking out", branch)
            rc, _, stderr = await _run_git(
                "worktree", "add", str(worktree_path), branch,
                cwd=config.repo_root,
            )
            if rc != 0:
                raise RuntimeError(f"Failed to create worktree: {stderr}")
        else:
            raise RuntimeError(f"Failed to create worktree: {stderr}")

    log.info("Created worktree at %s on branch %s", worktree_path, branch)
    return worktree_path, branch


async def remove_worktree(plan_name: str, config: Config) -> None:
    worktree_path = config.worktree_dir / plan_name
    branch = f"{config.branch_prefix}{plan_name}"

    rc, _, stderr = await _run_git(
        "worktree", "remove", str(worktree_path), "--force",
        cwd=config.repo_root,
    )
    if rc != 0:
        log.warning("Failed to remove worktree %s: %s", worktree_path, stderr)
    else:
        log.info("Removed worktree at %s", worktree_path)

    rc, _, stderr = await _run_git("branch", "-D", branch, cwd=config.repo_root)
    if rc != 0:
        log.warning("Failed to delete branch %s: %s", branch, stderr.strip())
    else:
        log.info("Deleted branch %s", branch)


async def merge_branch(branch: str, repo_root: Path) -> tuple[bool, str]:
    rc, stdout, stderr = await _run_git("merge", branch, cwd=repo_root)
    return rc == 0, stderr if rc != 0 else stdout


async def get_conflict_files(repo_root: Path) -> list[str]:
    rc, stdout, _ = await _run_git("diff", "--name-only", "--diff-filter=U", cwd=repo_root)
    if rc != 0:
        return []
    return [f.strip() for f in stdout.strip().splitlines() if f.strip()]


async def get_merge_diff(repo_root: Path) -> str:
    _, stdout, _ = await _run_git("diff", cwd=repo_root)
    return stdout


async def complete_merge(repo_root: Path, message: str) -> tuple[bool, str]:
    rc_add, _, stderr_add = await _run_git("add", "-A", cwd=repo_root)
    if rc_add != 0:
        return False, stderr_add
    rc, stdout, stderr = await _run_git("commit", "--no-edit", "-m", message, cwd=repo_root)
    return rc == 0, stderr if rc != 0 else stdout


async def abort_merge(repo_root: Path) -> None:
    await _run_git("merge", "--abort", cwd=repo_root)


async def list_worktrees(config: Config) -> list[WorktreeInfo]:
    if not config.worktree_dir.exists():
        return []

    async def _get_info(path: Path) -> WorktreeInfo | None:
        if not path.is_dir() or not (path / ".git").exists():
            return None
        rc, stdout, _ = await _run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)
        if rc != 0:
            return None
        return WorktreeInfo(path=path, branch=stdout.strip(), plan_name=path.name)

    results = await asyncio.gather(*[
        _get_info(p) for p in sorted(config.worktree_dir.iterdir())
    ])
    return [r for r in results if r is not None]
