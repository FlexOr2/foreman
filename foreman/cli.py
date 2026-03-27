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
from foreman.coordination import AgentType, CoordinationDB, PlanStatus
from foreman.loop import ForemanLoop
from foreman.plan_parser import load_plans
from foreman.preflight import check_git_repo, check_prerequisites
from foreman.resolver import CircularDependencyError, compute_waves
from foreman.spawner import Spawner

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


STATUS_STYLES = {
    "QUEUED": "white",
    "RUNNING": "cyan",
    "REVIEWING": "blue",
    "DONE": "green",
    "BLOCKED": "yellow",
    "FAILED": "red",
    "INTERRUPTED": "magenta",
}


@app.command
def init(repo: Path = Path(".")) -> None:
    """Initialize a repo for Foreman — creates plans/, .foreman/, prompt templates, config."""
    from importlib.resources import files

    from foreman.config import FOREMAN_DIR

    repo = repo.resolve()

    if not check_git_repo(str(repo)):
        console.print(f"[red]{repo} is not a git repository.[/red] Run 'git init' first.")
        sys.exit(1)

    if not check_prerequisites(console):
        sys.exit(1)

    plans_dir = repo / "plans"
    plans_dir.mkdir(exist_ok=True)
    console.print(f"  {plans_dir.relative_to(repo)}/")

    foreman_dir = repo / FOREMAN_DIR
    prompts_dir = foreman_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    templates = files("foreman") / "templates"
    for template in templates.iterdir():
        if template.name.startswith("prompt-") and template.name.endswith(".md"):
            dest = prompts_dir / template.name
            if not dest.exists():
                dest.write_text(template.read_text())
                console.print(f"  Created {dest.relative_to(repo)}")

    config_path = foreman_dir / "config.toml"
    if not config_path.exists():
        config_path.write_text(_DEFAULT_CONFIG)
        console.print(f"  Created {config_path.relative_to(repo)}")

    gitignore = repo / ".gitignore"
    gitignore_entry = ".foreman/"
    if gitignore.exists():
        content = gitignore.read_text()
        if gitignore_entry not in content:
            with open(gitignore, "a") as f:
                f.write(f"\n# Foreman\n{gitignore_entry}\n")
            console.print("  Added .foreman/ to .gitignore")
    else:
        gitignore.write_text(f"# Foreman\n{gitignore_entry}\n")
        console.print("  Created .gitignore")

    console.print(f"\n[green]Foreman initialized.[/green] Add plan files to plans/ and run 'foreman start'.")


_DEFAULT_CONFIG = """\
[foreman]
# plans_dir = "plans"          # relative to repo root
# branch_prefix = "feat/"

[foreman.agents]
# max_parallel_workers = 3
# max_parallel_reviews = 2
# model = "opus"
# permission_mode = "dontAsk"

[foreman.timeouts]
# implementation = 1800        # 30 min per plan
# review = 600                 # 10 min per review
# stuck_threshold = 300        # 5 min no activity = stuck
"""


@app.command
def start(
    repo: Path = Path("."),
    debug: bool = False,
) -> None:
    """Start Foreman — enters the event loop, watches for plans, spawns agents."""
    _setup_logging(debug)

    if not check_prerequisites(console):
        sys.exit(1)

    config = load_config(repo.resolve())

    if not config.plans_dir.exists():
        console.print(f"[yellow]No plans directory found.[/yellow] Run 'foreman init' first.")
        sys.exit(1)

    console.print(f"[bold]Foreman[/bold] starting in {config.repo_root}")
    console.print(f"  Plans dir: {config.plans_dir}")
    console.print(f"  Max workers: {config.agents.max_parallel_workers}")
    console.print(f"  Max reviews: {config.agents.max_parallel_reviews}")
    console.print(f"  Model: {config.agents.model}")
    console.print()

    asyncio.run(ForemanLoop(config).run())


@app.command
def plan(repo: Path = Path(".")) -> None:
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
        deps = ", ".join(set(d for p in wave for d in p.depends_on)) or "—"
        table.add_row(str(i), plan_names, deps)

    console.print(table)
    console.print(f"\n[bold]{len(plans)}[/bold] plans in [bold]{len(waves)}[/bold] waves")


@app.command
def status(repo: Path = Path(".")) -> None:
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

    for plan_data in db.get_all_plans():
        status_str = plan_data["status"]
        style = STATUS_STYLES.get(status_str, "white")
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
def kill(plan_name: str, repo: Path = Path(".")) -> None:
    """Kill all agents for a plan."""
    _setup_logging()
    config = load_config(repo.resolve())

    async def _kill_all() -> None:
        spawner = Spawner(config)
        for agent_type in AgentType:
            await spawner.kill_agent(plan_name, agent_type)

    asyncio.run(_kill_all())
    console.print(f"Killed agents for [bold]{plan_name}[/bold]")


@app.command
def pause(plan_name: str, repo: Path = Path(".")) -> None:
    """Pause a running agent — kills the process, marks INTERRUPTED. Worktree is preserved."""
    _setup_logging()
    config = load_config(repo.resolve())
    db = CoordinationDB(config.coordination_db)

    status = db.get_plan_status(plan_name)
    if status not in (PlanStatus.RUNNING, PlanStatus.REVIEWING):
        console.print(f"[yellow]Plan {plan_name} is not running[/yellow] (status: {status})")
        db.close()
        return

    async def _pause() -> None:
        spawner = Spawner(config)
        for agent_type in AgentType:
            await spawner.kill_agent(plan_name, agent_type)

    asyncio.run(_pause())
    db.set_plan_status(plan_name, PlanStatus.INTERRUPTED)
    db.close()
    console.print(f"Paused [bold]{plan_name}[/bold] — worktree preserved, use 'foreman resume' to continue")


@app.command
def resume(plan_name: str, repo: Path = Path(".")) -> None:
    """Resume an interrupted agent in its existing worktree."""
    _setup_logging()
    config = load_config(repo.resolve())
    db = CoordinationDB(config.coordination_db)

    plan_data = db.get_plan(plan_name)
    if not plan_data:
        console.print(f"[red]Plan {plan_name} not found[/red]")
        db.close()
        return

    if PlanStatus(plan_data["status"]) != PlanStatus.INTERRUPTED:
        console.print(f"[yellow]Plan {plan_name} is not interrupted[/yellow] (status: {plan_data['status']})")
        db.close()
        return

    worktree_path = plan_data.get("worktree_path")
    branch = plan_data.get("branch")
    if not worktree_path or not Path(worktree_path).exists():
        console.print(f"[red]Worktree not found for {plan_name}[/red]")
        db.close()
        return

    from foreman.plan_parser import load_plans
    plans = {p.name: p for p in load_plans(config.plans_dir)}
    plan = plans.get(plan_name)
    if not plan:
        console.print(f"[red]Plan file not found for {plan_name}[/red]")
        db.close()
        return

    plan_file = plan.file_path.resolve()
    initial_message = (
        f"You are resuming work on this plan. "
        f"Read the plan at {plan_file} and review what has already been done on branch {branch}. "
        f"Continue where the previous agent left off. Commit all changes when done."
    )

    async def _resume() -> None:
        spawner = Spawner(config)
        await spawner.setup()
        pid = await spawner.spawn_agent(
            plan, Path(worktree_path), AgentType.IMPLEMENTATION, initial_message,
        )
        db.set_plan_status(plan_name, PlanStatus.RUNNING)
        db.add_agent(plan_name, AgentType.IMPLEMENTATION, pid=pid)

    asyncio.run(_resume())
    db.close()
    console.print(f"Resumed [bold]{plan_name}[/bold] in existing worktree")


@app.command
def guide(plan_name: str, message: str, repo: Path = Path(".")) -> None:
    """Send guidance to a running agent."""
    _setup_logging()
    config = load_config(repo.resolve())
    db = CoordinationDB(config.coordination_db)

    status = db.get_plan_status(plan_name)
    if status not in (PlanStatus.RUNNING, PlanStatus.REVIEWING):
        console.print(f"[yellow]Plan {plan_name} is not running[/yellow] (status: {status})")
        db.close()
        return

    agent_type = db.get_active_agent_type(plan_name) or AgentType.IMPLEMENTATION

    async def _guide() -> None:
        spawner = Spawner(config)
        await spawner.notify_agent(plan_name, agent_type, message)

    asyncio.run(_guide())
    db.close()
    console.print(f"Sent guidance to [bold]{plan_name}[/bold] ({agent_type.value} agent)")


@app.command
def analyze(
    focus: str,
    repo: Path = Path("."),
    web: bool = False,
    path: str | None = None,
    dry_run: bool = False,
) -> None:
    """Analyze the codebase with a focus area and generate draft plans."""
    from foreman.analyze import FocusArea, run_analysis

    _setup_logging()
    config = load_config(repo.resolve())

    focus_label = focus
    try:
        focus_label = FocusArea(focus.lower()).value
    except ValueError:
        pass

    console.print(f"[bold]Analyzing[/bold] with focus: [cyan]{focus_label}[/cyan]")
    if path:
        console.print(f"  Scope: {path}")
    if web:
        console.print("  Web search: enabled")

    if dry_run:
        target = path or config.repo_root
        console.print(f"\n[dim]Dry run — would analyze {target} and generate draft plans.[/dim]")
        return

    drafts = asyncio.run(run_analysis(config, focus, web=web, scope_path=path))

    if not drafts:
        console.print("\n[yellow]No actionable findings.[/yellow]")
        return

    table = Table(title="Generated Draft Plans")
    table.add_column("File", style="cyan")
    table.add_column("Status", style="green")

    for draft in drafts:
        table.add_row(str(draft.relative_to(config.repo_root)), "ready for review")

    console.print()
    console.print(table)
    console.print(
        f"\n[bold]{len(drafts)}[/bold] draft plans written. "
        f"Review and rename (remove 'draft-' prefix) to approve."
    )


@app.command
def innovate(
    repo: Path = Path("."),
    web: bool = False,
    path: str | None = None,
    max_ideas: int | None = None,
    skip_review: bool = False,
    review_only: bool = False,
    categories: str | None = None,
) -> None:
    """Autonomously discover improvements via adversarial review pipeline."""
    from foreman.innovate import innovate as run_innovate, review_existing_drafts, Verdict

    _setup_logging()
    config = load_config(repo.resolve())

    cat_list = [c.strip() for c in categories.split(",")] if categories else None

    def _on_review(reviewer_name: str, verdict) -> None:
        style = {"KILL": "red", "REVISE": "yellow", "PASS": "green"}[verdict.action.value]
        console.print(f"  [{style}]{reviewer_name}: {verdict.action.value}[/{style}] — {verdict.reason}")

    if review_only:
        console.print("[bold]Reviewing[/bold] existing draft plans")
        survivors, killed = asyncio.run(review_existing_drafts(config, on_review=_on_review))

        for name, reason in killed:
            console.print(f"  [red]KILLED[/red] {name}: {reason}")

        if survivors:
            console.print(f"\n[bold]{len(survivors)}[/bold] drafts survived review.")
        else:
            console.print("\n[yellow]No drafts survived review.[/yellow]")
        return

    console.print("[bold]Foreman Innovate[/bold] — autonomous improvement discovery")
    if cat_list:
        console.print(f"  Categories: {', '.join(cat_list)}")
    if path:
        console.print(f"  Scope: {path}")
    if web:
        console.print("  Web search: enabled")
    if skip_review:
        console.print("  Adversarial review: [yellow]skipped[/yellow]")
    console.print()

    drafts = asyncio.run(run_innovate(
        config,
        categories=cat_list,
        max_ideas=max_ideas,
        web=web,
        scope_path=path,
        skip_review=skip_review,
        on_review=_on_review,
    ))

    if not drafts:
        console.print("\n[yellow]No actionable ideas survived.[/yellow]")
        return

    table = Table(title="Generated Draft Plans")
    table.add_column("File", style="cyan")
    table.add_column("Status", style="green")

    for draft in drafts:
        status_label = "ready for review" if skip_review else "survived adversarial review"
        table.add_row(str(draft.relative_to(config.repo_root)), status_label)

    console.print()
    console.print(table)
    console.print(
        f"\n[bold]{len(drafts)}[/bold] draft plans written. "
        f"Review and rename (remove 'draft-' prefix) to approve."
    )


@app.command
def reset(repo: Path = Path(".")) -> None:
    """Reset coordination DB and clean up worktrees."""
    _setup_logging()
    config = load_config(repo.resolve())

    if config.coordination_db.exists():
        db = CoordinationDB(config.coordination_db)
        db.reset()
        db.close()
        console.print("Coordination DB reset.")

    import shutil

    async def cleanup() -> None:
        from foreman.worktree import list_worktrees, remove_worktree
        worktrees = await list_worktrees(config)
        await asyncio.gather(*[remove_worktree(wt.plan_name, config) for wt in worktrees])

    if config.worktree_dir.exists():
        asyncio.run(cleanup())
        console.print("Worktrees cleaned up.")

    if config.scripts_dir.exists():
        shutil.rmtree(config.scripts_dir)
        console.print("Scripts cleaned up.")

    done_dir = config.repo_root / ".foreman" / "done"
    if done_dir.exists():
        shutil.rmtree(done_dir)

    console.print("[green]Reset complete.[/green]")
