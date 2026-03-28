from __future__ import annotations

import re
from pathlib import Path

import pytest

from foreman.coordination import AgentType, CoordinationDB
from foreman.plan_parser import InvalidPlanNameError, Plan, parse_plan
from foreman.resolver import (
    CircularDependencyError,
    UnresolvedDependencyError,
    validate_dag,
)


# --- Plan name validation ---


class TestPlanNameValidation:
    def test_valid_names(self, tmp_path: Path) -> None:
        for name in ("my-plan", "plan_v2", "bugfix.3", "A123"):
            plan_file = tmp_path / f"{name}.md"
            plan_file.write_text("# Title\n")
            plan = parse_plan(plan_file)
            assert plan.name == name

    def test_rejects_leading_hyphen(self, tmp_path: Path) -> None:
        plan_file = tmp_path / "-bad-name.md"
        plan_file.write_text("# Title\n")
        with pytest.raises(InvalidPlanNameError):
            parse_plan(plan_file)

    def test_rejects_leading_dot(self, tmp_path: Path) -> None:
        plan_file = tmp_path / ".hidden.md"
        plan_file.write_text("# Title\n")
        with pytest.raises(InvalidPlanNameError):
            parse_plan(plan_file)

    def test_rejects_spaces(self, tmp_path: Path) -> None:
        plan_file = tmp_path / "has space.md"
        plan_file.write_text("# Title\n")
        with pytest.raises(InvalidPlanNameError):
            parse_plan(plan_file)

    def test_rejects_leading_underscore(self, tmp_path: Path) -> None:
        plan_file = tmp_path / "_internal.md"
        plan_file.write_text("# Title\n")
        with pytest.raises(InvalidPlanNameError):
            parse_plan(plan_file)

    def test_rejects_shell_metacharacters(self, tmp_path: Path) -> None:
        plan_file = tmp_path / "plan;rm -rf.md"
        plan_file.write_text("# Title\n")
        with pytest.raises(InvalidPlanNameError):
            parse_plan(plan_file)


# --- Unresolved dependency detection ---


class TestUnresolvedDependencies:
    def test_valid_deps_pass(self) -> None:
        plans = [
            Plan(name="a", file_path=Path("a.md")),
            Plan(name="b", file_path=Path("b.md"), depends_on=["a"]),
        ]
        validate_dag(plans)

    def test_unknown_dep_raises(self) -> None:
        plans = [
            Plan(name="a", file_path=Path("a.md"), depends_on=["nonexistent"]),
        ]
        with pytest.raises(UnresolvedDependencyError) as exc_info:
            validate_dag(plans)
        assert "nonexistent" in str(exc_info.value)
        assert "a" in exc_info.value.unresolved

    def test_multiple_unresolved(self) -> None:
        plans = [
            Plan(name="a", file_path=Path("a.md"), depends_on=["x"]),
            Plan(name="b", file_path=Path("b.md"), depends_on=["y", "a"]),
        ]
        with pytest.raises(UnresolvedDependencyError) as exc_info:
            validate_dag(plans)
        assert "x" in str(exc_info.value)
        assert "y" in str(exc_info.value)

    def test_cycle_still_detected(self) -> None:
        plans = [
            Plan(name="a", file_path=Path("a.md"), depends_on=["b"]),
            Plan(name="b", file_path=Path("b.md"), depends_on=["a"]),
        ]
        with pytest.raises(CircularDependencyError):
            validate_dag(plans)

    def test_no_deps_pass(self) -> None:
        plans = [
            Plan(name="a", file_path=Path("a.md")),
            Plan(name="b", file_path=Path("b.md")),
        ]
        validate_dag(plans)

    def test_known_completed_satisfies_dependency(self) -> None:
        plans = [
            Plan(name="a", file_path=Path("a.md"), depends_on=["archived"]),
        ]
        validate_dag(plans, known_completed={"archived"})

    def test_still_raises_for_truly_unknown_dep(self) -> None:
        plans = [
            Plan(name="a", file_path=Path("a.md"), depends_on=["missing"]),
        ]
        with pytest.raises(UnresolvedDependencyError):
            validate_dag(plans, known_completed={"archived"})


# --- Module encapsulation ---


class TestAgentLifecycle:
    def test_finish_agent_closes_record(self, tmp_path: Path) -> None:
        db = CoordinationDB(tmp_path / "foreman.db")
        db.upsert_plan("test-plan", "queued")

        agent_id = db.add_agent("test-plan", AgentType.IMPLEMENTATION, pid=1234)
        assert db.get_active_agent_type("test-plan") == AgentType.IMPLEMENTATION

        db.finish_agent(agent_id, exit_code=0)
        assert db.get_active_agent_type("test-plan") is None

        db.close()


MONITOR_PATH = Path(__file__).resolve().parent.parent / "foreman" / "monitor.py"


class TestMonitorEncapsulation:
    def test_no_private_spawner_access(self) -> None:
        source = MONITOR_PATH.read_text()
        private_attr = re.findall(r"_spawner\._", source)
        assert private_attr == [], f"monitor.py accesses private spawner attrs: {private_attr}"

    def test_no_direct_backend_access(self) -> None:
        source = MONITOR_PATH.read_text()
        backend_access = re.findall(r"_spawner\.backend", source)
        assert backend_access == [], f"monitor.py accesses spawner.backend directly: {backend_access}"
