from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

import foreman.config as config_module
from foreman.config import Config
from foreman.coordination import PlanStatus
from foreman.loop import ForemanLoop

MOCK_CLAUDE = Path(__file__).parent / "mock_claude.sh"


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _make_config(repo: Path) -> Config:
    config = Config(repo_root=repo, claude_bin=str(MOCK_CLAUDE))
    config.resolve_paths()
    config.timeouts.implementation = 30
    config.timeouts.review = 30
    config.timeouts.stuck_threshold = 60
    return config


async def _mock_spawner_setup(self) -> None:
    (self.config.repo_root / ".foreman" / "done").mkdir(parents=True, exist_ok=True)


async def _run_until_terminal(
    loop: ForemanLoop, plan_name: str, timeout: float = 30.0
) -> PlanStatus | None:
    loop_task = asyncio.create_task(loop.run())
    status = None

    elapsed = 0.0
    while elapsed < timeout:
        status = loop.db.get_plan_status(plan_name)
        if status in (PlanStatus.DONE, PlanStatus.FAILED, PlanStatus.BLOCKED):
            break
        await asyncio.sleep(0.2)
        elapsed += 0.2

    # Cancel the task so the finally-block graceful shutdown runs, but
    # avoid triggering _shutdown_waiter's KeyboardInterrupt which would
    # propagate out of asyncio.run() and confuse pytest.
    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=15.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:
        pass

    return status


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    foreman_dir = tmp_path / ".foreman"
    (foreman_dir / "prompts").mkdir(parents=True)
    for name in ["prompt-implementation.md", "prompt-review.md", "prompt-fix.md", "prompt-rebase.md"]:
        (foreman_dir / "prompts" / name).write_text(f"# {name}\n")
    (tmp_path / "plans").mkdir()
    return tmp_path


class TestSpawnCycle:
    def test_happy_path(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # CLAUDE_BIN global also covers brain.summarize_and_reset during shutdown
        monkeypatch.setattr(config_module, "CLAUDE_BIN", str(MOCK_CLAUDE))
        monkeypatch.setattr("foreman.loop.check_prerequisites", lambda: True)
        monkeypatch.setattr("foreman.spawner.Spawner.setup", _mock_spawner_setup)

        (repo / "plans" / "my-plan.md").write_text("# My Plan\nDo something.\n")
        config = _make_config(repo)

        async def _run() -> PlanStatus | None:
            return await _run_until_terminal(ForemanLoop(config), "my-plan")

        status = asyncio.run(_run())

        assert status == PlanStatus.DONE
        assert not (config.worktree_dir / "my-plan").exists()

    def test_review_with_findings(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_module, "CLAUDE_BIN", str(MOCK_CLAUDE))
        monkeypatch.setattr("foreman.loop.check_prerequisites", lambda: True)
        monkeypatch.setattr("foreman.spawner.Spawner.setup", _mock_spawner_setup)
        monkeypatch.setenv("MOCK_REVIEW_VERDICT", "findings")

        (repo / "plans" / "my-plan.md").write_text("# My Plan\nDo something.\n")
        config = _make_config(repo)

        async def _run() -> PlanStatus | None:
            return await _run_until_terminal(ForemanLoop(config), "my-plan", timeout=60.0)

        status = asyncio.run(_run())

        assert status == PlanStatus.DONE

    def test_merge_conflict(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_module, "CLAUDE_BIN", str(MOCK_CLAUDE))
        monkeypatch.setattr("foreman.loop.check_prerequisites", lambda: True)
        monkeypatch.setattr("foreman.spawner.Spawner.setup", _mock_spawner_setup)
        monkeypatch.setenv("MOCK_MERGE_CONFLICT", "1")

        (repo / "plans" / "my-plan.md").write_text("# My Plan\nDo something.\n")
        config = _make_config(repo)

        async def _run() -> PlanStatus | None:
            return await _run_until_terminal(ForemanLoop(config), "my-plan", timeout=60.0)

        status = asyncio.run(_run())

        assert status == PlanStatus.DONE

    def test_agent_crash(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_module, "CLAUDE_BIN", str(MOCK_CLAUDE))
        monkeypatch.setattr("foreman.loop.check_prerequisites", lambda: True)
        monkeypatch.setattr("foreman.spawner.Spawner.setup", _mock_spawner_setup)
        monkeypatch.setenv("MOCK_CLAUDE_EXIT_CODE", "1")

        (repo / "plans" / "my-plan.md").write_text("# My Plan\nDo something.\n")
        config = _make_config(repo)

        async def _run() -> PlanStatus | None:
            return await _run_until_terminal(ForemanLoop(config), "my-plan")

        status = asyncio.run(_run())

        assert status == PlanStatus.FAILED
