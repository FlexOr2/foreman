"""Build a DAG from plan dependencies and determine what can run now."""

from __future__ import annotations

from foreman.plan_parser import Plan


class CircularDependencyError(Exception):
    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(f"Circular dependency detected: {' -> '.join(cycle)}")


def validate_dag(plans: list[Plan]) -> None:
    """Check for circular dependencies using DFS. Raises CircularDependencyError."""
    plan_names = {p.name for p in plans}
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
    """Return plans whose dependencies are all completed and aren't running or done."""
    ready = []
    for plan in all_plans:
        if plan.name in completed or plan.name in running:
            continue
        unmet = [d for d in plan.depends_on if d not in completed]
        if not unmet:
            ready.append(plan)
    return ready


def compute_waves(plans: list[Plan]) -> list[list[Plan]]:
    """Compute execution waves for display (dry run). Returns list of waves."""
    validate_dag(plans)

    plan_map = {p.name: p for p in plans}
    completed: set[str] = set()
    remaining = set(plan_map.keys())
    waves: list[list[Plan]] = []

    while remaining:
        wave = []
        for name in list(remaining):
            plan = plan_map[name]
            unmet = [d for d in plan.depends_on if d not in completed]
            if not unmet:
                wave.append(plan)

        if not wave:
            # Remaining plans have unresolvable dependencies (missing plans)
            wave = [plan_map[name] for name in remaining]
            waves.append(wave)
            break

        waves.append(wave)
        for plan in wave:
            completed.add(plan.name)
            remaining.discard(plan.name)

    return waves
