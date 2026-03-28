"""Launch agent processes as background subprocesses in print mode."""

from __future__ import annotations

import asyncio
import logging
import shlex
import signal
from pathlib import Path

import foreman.config as _config
from foreman.config import Config
from foreman.coordination import AgentType
from foreman.plan_parser import Plan

log = logging.getLogger(__name__)

TMUX_SESSION = "foreman"
AGENT_TYPE_SEP = "__"


def log_filename(plan_name: str, agent_type: AgentType) -> str:
    return f"{plan_name}{AGENT_TYPE_SEP}{agent_type.value}.log"


def _script_filename(plan_name: str, agent_type: AgentType) -> str:
    return f"{plan_name}{AGENT_TYPE_SEP}{agent_type.value}.sh"


def _build_launcher_script(
    plan: Plan,
    worktree_path: Path,
    agent_type: AgentType,
    config: Config,
    initial_message: str,
) -> str:
    prompt_path = config.get_prompt_path(agent_type).resolve()
    plans_dir = config.plans_dir.resolve()
    done_dir = (config.repo_root / ".foreman" / "done").resolve()
    log_dir = config.log_dir.resolve()
    tools = config.allowed_tools.get(agent_type, "")

    sentinel_name = f"{plan.name}{AGENT_TYPE_SEP}{agent_type.value}"
    sentinel_path = shlex.quote(str(done_dir / sentinel_name))
    log_path = shlex.quote(str(log_dir / log_filename(plan.name, agent_type)))

    lines = [
        "#!/bin/bash",
        "_ec=1",
        f"trap 'echo \"$_ec\" > {sentinel_path}.tmp && mv {sentinel_path}.tmp {sentinel_path}' EXIT",
        f"cd {shlex.quote(str(worktree_path.resolve()))}",
    ]

    cmd_parts = [
        _config.CLAUDE_BIN,
        f"  -p {shlex.quote(initial_message)}",
        "  --output-format json",
        f'  --append-system-prompt "$(cat {shlex.quote(str(prompt_path))})"',
        f"  --permission-mode {shlex.quote(config.agents.permission_mode)}",
        f"  --model {shlex.quote(config.agents.model)}",
        f"  --name {shlex.quote(f'foreman:{plan.name}:{agent_type.value}')}",
        f"  --add-dir {shlex.quote(str(plans_dir))}",
    ]

    if tools:
        cmd_parts.append(f"  --allowed-tools {shlex.quote(tools)}")

    lines.append(" \\\n".join(cmd_parts) + f" > {log_path} 2>&1")
    lines.append("_ec=$?")
    return "\n".join(lines) + "\n"


class Spawner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def setup(self) -> None:
        (self.config.repo_root / ".foreman" / "done").mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", TMUX_SESSION,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "new-session", "-d", "-s", TMUX_SESSION, "-n", "dashboard",
            )
            await proc.wait()
            log.info("Created tmux session: %s", TMUX_SESSION)

    async def teardown(self) -> None:
        pass

    def _process_key(self, plan_name: str, agent_type: AgentType) -> str:
        return f"{plan_name}{AGENT_TYPE_SEP}{agent_type.value}"

    async def spawn_agent(
        self,
        plan: Plan,
        worktree_path: Path,
        agent_type: AgentType,
        initial_message: str,
    ) -> int | None:
        script_content = _build_launcher_script(
            plan, worktree_path, agent_type, self.config, initial_message,
        )

        script_path = self.config.scripts_dir / _script_filename(plan.name, agent_type)
        script_path.write_text(script_content)
        script_path.chmod(0o755)

        log_file = self.config.log_dir / log_filename(plan.name, agent_type)
        log_file.touch()

        key = self._process_key(plan.name, agent_type)
        await self._kill_process(key)

        proc = await asyncio.create_subprocess_exec(
            "bash", str(script_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._processes[key] = proc
        log.info("Spawned %s agent for %s (PID: %s)", agent_type.value, plan.name, proc.pid)
        return proc.pid

    async def is_agent_alive(self, plan_name: str, agent_type: AgentType) -> bool:
        key = self._process_key(plan_name, agent_type)
        proc = self._processes.get(key)
        return proc is not None and proc.returncode is None

    async def kill_agent(self, plan_name: str, agent_type: AgentType) -> None:
        await self._kill_process(self._process_key(plan_name, agent_type))

    async def _kill_process(self, key: str) -> None:
        proc = self._processes.pop(key, None)
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        except ProcessLookupError:
            pass

    async def kill_session(self) -> None:
        for key in list(self._processes):
            await self._kill_process(key)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", TMUX_SESSION,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
