"""Build a DAG from plan dependencies and determine what can run now."""

from __future__ import annotations

from foreman.plan_parser import Plan


class CircularDependencyError(Exception):
    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(f"Circular dependency detected: {' -> '.join(cycle)}")


class UnresolvedDependencyError(Exception):
    def __init__(self, unresolved: dict[str, list[str]]) -> None:
        self.unresolved = unresolved
        details = "; ".join(
            f"{plan} -> {', '.join(deps)}" for plan, deps in unresolved.items()
        )
        super().__init__(f"Unresolved dependencies: {details}")


def _unmet_deps(plan: Plan, completed: set[str]) -> list[str]:
    return [d for d in plan.depends_on if d not in completed]


def validate_dag(plans: list[Plan], known_completed: set[str] | None = None) -> None:
    plan_names = {p.name for p in plans}
    all_known = plan_names | (known_completed or set())

    unresolved = {
        p.name: [d for d in p.depends_on if d not in all_known]
        for p in plans
        if any(d not in all_known for d in p.depends_on)
    }
    if unresolved:
        raise UnresolvedDependencyError(unresolved)

    adjacency = {p.name: [d for d in p.depends_on if d in plan_names] for p in plans}

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in plan_names}
    path: list[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for dep in adjacency[node]:
            if color[dep] == GRAY:
                cycle_start = path.index(dep)
                raise CircularDependencyError(path[cycle_start:] + [dep])
            if color[dep] == WHITE:
                dfs(dep)
        path.pop()
        color[node] = BLACK

    for name in plan_names:
        if color[name] == WHITE:
            dfs(name)


def get_ready_plans(
    all_plans: list[Plan],
    completed: set[str],
    running: set[str],
) -> list[Plan]:
    return [
        plan for plan in all_plans
        if plan.name not in completed
        and plan.name not in running
        and not _unmet_deps(plan, completed)
    ]


def compute_waves(plans: list[Plan], known_completed: set[str] | None = None) -> list[list[Plan]]:
    validate_dag(plans, known_completed)

    plan_map = {p.name: p for p in plans}
    completed: set[str] = set()
    remaining = set(plan_map.keys())
    waves: list[list[Plan]] = []

    while remaining:
        wave = [
            plan_map[name] for name in remaining
            if not _unmet_deps(plan_map[name], completed)
        ]

        if not wave:
            waves.append([plan_map[name] for name in remaining])
            break

        waves.append(wave)
        for plan in wave:
            completed.add(plan.name)
            remaining.discard(plan.name)

    return waves
