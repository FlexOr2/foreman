"""CLI interface for Foreman — built with cyclopts."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import cyclopts
from rich.console import Console
from rich.table import Table

from foreman.config import load_config
from foreman.coordination import CoordinationDB, PlanStatus
from foreman.loop import ForemanLoop
from foreman.plan_parser import load_plans
from foreman.resolver import CircularDependencyError, compute_waves

app = cyclopts.App(
    name="foreman",
    help="AI agent orchestrator for parallel Claude Code execution.",
)
console = Console()


def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command
def start(
    repo: Path = Path("."),
    debug: bool = False,
) -> None:
    """Start Foreman — enters the event loop, watches for plans, spawns agents."""
    _setup_logging(debug)
    config = load_config(repo.resolve())

    console.print(f"[bold]Foreman[/bold] starting in {config.repo_root}")
    console.print(f"  Plans dir: {config.plans_dir}")
    console.print(f"  Max workers: {config.agents.max_parallel_workers}")
    console.print(f"  Max reviews: {config.agents.max_parallel_reviews}")
    console.print(f"  Model: {config.agents.model}")
    console.print()

    loop = ForemanLoop(config)
    asyncio.run(loop.run())


@app.command
def plan(
    repo: Path = Path("."),
) -> None:
    """Dry run — analyze plans and show execution order."""
    _setup_logging()
    config = load_config(repo.resolve())
    plans = load_plans(config.plans_dir)

    if not plans:
        console.print("[yellow]No plans found[/yellow] in", config.plans_dir)
        return

    try:
        waves = compute_waves(plans)
    except CircularDependencyError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    table = Table(title="Execution Plan")
    table.add_column("Wave", style="bold")
    table.add_column("Plans", style="cyan")
    table.add_column("Dependencies")

    for i, wave in enumerate(waves):
        plan_names = ", ".join(p.name for p in wave)
        deps = ", ".join(
            set(d for p in wave for d in p.depends_on)
        ) or "—"
        table.add_row(str(i), plan_names, deps)

    console.print(table)
    console.print(f"\n[bold]{len(plans)}[/bold] plans in [bold]{len(waves)}[/bold] waves")


@app.command
def status(
    repo: Path = Path("."),
) -> None:
    """Show status of running/completed agents."""
    config = load_config(repo.resolve())

    if not config.coordination_db.exists():
        console.print("[yellow]No coordination DB found.[/yellow] Run 'foreman start' first.")
        return

    db = CoordinationDB(config.coordination_db)

    table = Table(title="Foreman Status")
    table.add_column("Plan", style="bold")
    table.add_column("Status")
    table.add_column("Branch", style="dim")
    table.add_column("Updated")
    table.add_column("Reason", style="dim")

    status_styles = {
        "QUEUED": "white",
        "RUNNING": "cyan",
        "REVIEWING": "blue",
        "DONE": "green",
        "BLOCKED": "yellow",
        "FAILED": "red",
        "INTERRUPTED": "magenta",
    }

    for plan_data in db.get_all_plans():
        status_str = plan_data["status"]
        style = status_styles.get(status_str, "white")
        table.add_row(
            plan_data["plan"],
            f"[{style}]{status_str}[/{style}]",
            plan_data.get("branch", ""),
            plan_data.get("updated_at", "")[:19],
            plan_data.get("blocked_reason", "") or "",
        )

    console.print(table)
    db.close()


@app.command
def kill(
    plan_name: str,
    repo: Path = Path("."),
) -> None:
    """Kill a stuck agent."""
    _setup_logging()
    config = load_config(repo.resolve())

    from foreman.spawner import Spawner
    spawner = Spawner(config)
    asyncio.run(spawner.kill_agent(plan_name))
    console.print(f"Killed agent for [bold]{plan_name}[/bold]")


@app.command
def reset(
    repo: Path = Path("."),
) -> None:
    """Reset coordination DB and clean up worktrees."""
    _setup_logging()
    config = load_config(repo.resolve())

    if config.coordination_db.exists():
        db = CoordinationDB(config.coordination_db)
        db.reset()
        db.close()
        console.print("Coordination DB reset.")

    # Clean up worktrees
    import shutil
    if config.worktree_dir.exists():
        # Use git to properly remove worktrees
        async def cleanup() -> None:
            from foreman.worktree import list_worktrees, remove_worktree
            worktrees = await list_worktrees(config.worktree_dir)
            for wt in worktrees:
                await remove_worktree(
                    wt.plan_name, config.worktree_dir,
                    config.branch_prefix, config.repo_root,
                )
        asyncio.run(cleanup())
        console.print("Worktrees cleaned up.")

    # Clean up scripts
    if config.scripts_dir.exists():
        shutil.rmtree(config.scripts_dir)
        console.print("Scripts cleaned up.")

    console.print("[green]Reset complete.[/green]")
