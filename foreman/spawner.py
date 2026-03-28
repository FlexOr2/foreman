"""Launch agent processes via tmux or VS Code extension backends."""

from __future__ import annotations

import asyncio
import logging
import shlex
from abc import ABC, abstractmethod
from pathlib import Path

import foreman.config as _config
from foreman.config import Config
from foreman.coordination import AgentType
from foreman.plan_parser import Plan

log = logging.getLogger(__name__)

TMUX_SESSION = "foreman"


class Backend(ABC):
    @abstractmethod
    async def create_terminal(self, name: str, command: str, log_file: Path) -> None: ...

    @abstractmethod
    async def get_pid(self, name: str) -> int | None: ...

    @abstractmethod
    async def send_text(self, name: str, text: str) -> None: ...

    @abstractmethod
    async def capture_output(self, name: str) -> str | None: ...

    @abstractmethod
    async def kill_terminal(self, name: str) -> None: ...

    @abstractmethod
    async def has_terminal(self, name: str) -> bool: ...

    @abstractmethod
    async def kill_all(self) -> None: ...

    @abstractmethod
    async def setup(self) -> None: ...

    @abstractmethod
    async def teardown(self) -> None: ...


class TmuxBackend(Backend):
    async def setup(self) -> None:
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
        else:
            log.info("Reusing existing tmux session: %s", TMUX_SESSION)

    async def teardown(self) -> None:
        pass

    async def create_terminal(self, name: str, command: str, log_file: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "new-window", "-t", TMUX_SESSION, "-n", name, command,
        )
        await proc.wait()

        proc = await asyncio.create_subprocess_exec(
            "tmux", "pipe-pane", "-t", f"{TMUX_SESSION}:{name}",
            "-o", f"cat >> {log_file}",
        )
        await proc.wait()

        log.info("Spawned agent %s in tmux window", name)

    async def get_pid(self, name: str) -> int | None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-panes", "-t", f"{TMUX_SESSION}:{name}",
            "-F", "#{pane_pid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        try:
            return int(stdout.decode().strip())
        except ValueError:
            return None

    async def capture_output(self, name: str) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "capture-pane", "-t", f"{TMUX_SESSION}:{name}", "-p",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        return stdout.decode()

    async def send_text(self, name: str, text: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{TMUX_SESSION}:{name}", text, "Enter",
        )
        await proc.wait()

    async def kill_terminal(self, name: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-window", "-t", f"{TMUX_SESSION}:{name}",
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def has_terminal(self, name: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "display-message", "-t", f"{TMUX_SESSION}:{name}", "-p", "",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    async def kill_all(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", TMUX_SESSION,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


class VSCodeBackend(Backend):
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    async def _send(self, msg: dict) -> None:
        import json
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
            writer.write(json.dumps(msg).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except (ConnectionRefusedError, FileNotFoundError):
            log.warning("VS Code extension not reachable at %s", self.socket_path)

    async def setup(self) -> None:
        pass

    async def teardown(self) -> None:
        pass

    async def create_terminal(self, name: str, command: str, log_file: Path) -> None:
        await self._send({"action": "create_terminal", "name": name, "command": command})

    async def get_pid(self, name: str) -> int | None:
        return None

    async def capture_output(self, name: str) -> str | None:
        return None

    async def send_text(self, name: str, text: str) -> None:
        await self._send({"action": "send_text", "name": name, "text": text})

    async def kill_terminal(self, name: str) -> None:
        await self._send({"action": "kill_terminal", "name": name})

    async def has_terminal(self, name: str) -> bool:
        return False

    async def kill_all(self) -> None:
        pass


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
) -> str:
    prompt_path = config.get_prompt_path(agent_type).resolve()
    plans_dir = config.plans_dir.resolve()
    done_dir = (config.repo_root / ".foreman" / "done").resolve()
    tools = config.allowed_tools.get(agent_type, "")

    lines = [
        "#!/bin/bash",
        f"cd {shlex.quote(str(worktree_path.resolve()))}",
    ]

    cmd_parts = [
        _config.CLAUDE_BIN,
        f'  --append-system-prompt "$(cat {shlex.quote(str(prompt_path))})"',
        f"  --permission-mode {shlex.quote(config.agents.permission_mode)}",
        f"  --model {shlex.quote(config.agents.model)}",
        f"  --name {shlex.quote(f'foreman:{plan.name}')}",
        f"  --add-dir {shlex.quote(str(plans_dir))}",
    ]

    if agent_type in (AgentType.REVIEW, AgentType.FIX):
        cmd_parts.append("  --bare")

    if tools:
        cmd_parts.append(f"  --allowed-tools {shlex.quote(tools)}")

    lines.append(" \\\n".join(cmd_parts))
    sentinel_name = f"{plan.name}{AGENT_TYPE_SEP}{agent_type.value}"
    sentinel_path = shlex.quote(str(done_dir / sentinel_name))
    lines.append(f"_ec=$?; echo $_ec > {sentinel_path}.tmp && mv {sentinel_path}.tmp {sentinel_path}")
    return "\n".join(lines) + "\n"


class Spawner:
    def __init__(self, config: Config) -> None:
        self.config = config
        socket_path = config.repo_root / ".foreman" / "extension.sock"
        if socket_path.exists():
            self.backend: Backend = VSCodeBackend(socket_path)
        else:
            self.backend = TmuxBackend()

    async def setup(self) -> None:
        await self.backend.setup()
        (self.config.repo_root / ".foreman" / "done").mkdir(parents=True, exist_ok=True)

    async def teardown(self) -> None:
        await self.backend.teardown()

    def terminal_name(self, plan_name: str, agent_type: AgentType) -> str:
        return f"{plan_name}{AGENT_TYPE_SEP}{agent_type.value}"

    async def spawn_agent(
        self,
        plan: Plan,
        worktree_path: Path,
        agent_type: AgentType,
        initial_message: str,
    ) -> int | None:
        script_content = _build_launcher_script(
            plan, worktree_path, agent_type, self.config,
        )

        script_path = self.config.scripts_dir / _script_filename(plan.name, agent_type)
        script_path.write_text(script_content)
        script_path.chmod(0o755)

        log_file = self.config.log_dir / log_filename(plan.name, agent_type)
        log_file.touch()

        terminal = self.terminal_name(plan.name, agent_type)
        await self.backend.kill_terminal(terminal)
        await self.backend.create_terminal(
            name=terminal,
            command=f"bash {shlex.quote(str(script_path))}",
            log_file=log_file,
        )

        pid = await self.backend.get_pid(terminal)
        log.info("Spawned %s agent for %s (PID: %s)", agent_type.value, plan.name, pid)

        await self._wait_for_ready(terminal)
        await self.backend.send_text(terminal, initial_message)
        log.info("Sent initial message to %s agent for %s", agent_type.value, plan.name)

        await self._confirm_paste_if_needed(terminal)
        return pid

    async def capture_output(self, terminal: str) -> str | None:
        return await self.backend.capture_output(terminal)

    async def send_command(self, terminal: str, text: str) -> None:
        await self.backend.send_text(terminal, text)

    async def _wait_for_ready(self, terminal: str, timeout: int = 180) -> None:
        for _ in range(timeout):
            pane_content = await self.backend.capture_output(terminal)
            if pane_content and "\u276f" in pane_content:
                return
            await asyncio.sleep(1)
        raise TimeoutError(f"Agent in terminal {terminal!r} did not become ready within {timeout}s")

    async def _confirm_paste_if_needed(self, terminal: str) -> None:
        await asyncio.sleep(2)
        content = await self.backend.capture_output(terminal)
        if content and "[Pasted text" in content:
            log.info("Paste confirmation detected in %s, sending Enter", terminal)
            await self.backend.send_text(terminal, "")

    async def notify_agent(self, plan_name: str, agent_type: AgentType, message: str) -> None:
        await self.backend.send_text(self.terminal_name(plan_name, agent_type), message)

    async def has_window(self, terminal: str) -> bool:
        return await self.backend.has_terminal(terminal)

    async def kill_session(self) -> None:
        await self.backend.kill_all()

    async def kill_agent(self, plan_name: str, agent_type: AgentType) -> None:
        await self.backend.kill_terminal(self.terminal_name(plan_name, agent_type))
