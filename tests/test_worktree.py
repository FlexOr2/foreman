from __future__ import annotations

import asyncio
from pathlib import Path

from foreman.worktree import _run_git, complete_merge


class TestCompleteMerge:
    def test_unrelated_dirty_files_not_staged(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        async def _setup_and_test() -> None:
            await _run_git("init", cwd=repo)
            await _run_git("config", "user.email", "test@test.com", cwd=repo)
            await _run_git("config", "user.name", "Test", cwd=repo)
            (repo / "tracked.txt").write_text("initial")
            await _run_git("add", "tracked.txt", cwd=repo)
            await _run_git("commit", "-m", "initial", cwd=repo)

            await _run_git("checkout", "-b", "feature", cwd=repo)
            (repo / "tracked.txt").write_text("feature change")
            await _run_git("add", "tracked.txt", cwd=repo)
            await _run_git("commit", "-m", "feature", cwd=repo)

            await _run_git("checkout", "main", cwd=repo)
            (repo / "tracked.txt").write_text("main change")
            await _run_git("add", "tracked.txt", cwd=repo)
            await _run_git("commit", "-m", "main diverge", cwd=repo)

            await _run_git("merge", "feature", cwd=repo)
            (repo / "tracked.txt").write_text("resolved")
            (repo / "unrelated.txt").write_text("should not be staged")

            success, _ = await complete_merge(repo, "merge commit", files=["tracked.txt"])
            assert success

            _, stdout, _ = await _run_git("diff", "--name-only", "HEAD~1", "HEAD", cwd=repo)
            committed_files = stdout.strip().splitlines()
            assert "unrelated.txt" not in committed_files

            _, stdout, _ = await _run_git("status", "--porcelain", cwd=repo)
            assert "unrelated.txt" in stdout

        asyncio.run(_setup_and_test())
