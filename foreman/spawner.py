"""Launch agent processes via tmux or VS Code extension backends."""

from __future__ import annotations

import asyncio
import logging
import shlex
from abc import ABC, abstractmethod
from pathlib import Path

from foreman.config import Config
from foreman.plan_parser import Plan

log = logging.getLogger(__name__)

CLAUDE_BIN = "claude"


class Backend(ABC):
    @abstractmethod
    async def create_terminal(
        self, name: str, command: str, log_file: Path,
    ) -> None: ...

    @abstractmethod
    async def get_pid(self, name: str) -> int | None: ...

    @abstractmethod
    async def send_text(self, name: str, text: str) -> None: ...

    @abstractmethod
    async def kill_terminal(self, name: str) -> None: ...

    @abstractmethod
    async def setup(self) -> None: ...

    @abstractmethod
    async def teardown(self) -> None: ...


class TmuxBackend(Backend):
    SESSION = "foreman"

    async def setup(self) -> None:
        """Create the foreman tmux session if it doesn't exist."""
        rc = (await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", self.SESSION,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )).returncode if False else 1

        # Actually check
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", self.SESSION,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        if proc.returncode != 0:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "new-session", "-d", "-s", self.SESSION, "-n", "dashboard",
            )
            await proc.wait()
            log.info("Created tmux session: %s", self.SESSION)
        else:
            log.info("Reusing existing tmux session: %s", self.SESSION)

    async def teardown(self) -> None:
        pass  # Leave tmux session alive for user

    async def create_terminal(
        self, name: str, command: str, log_file: Path,
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "new-window", "-t", self.SESSION, "-n", name, command,
        )
        await proc.wait()

        # Enable log capture via pipe-pane
        proc = await asyncio.create_subprocess_exec(
            "tmux", "pipe-pane", "-t", f"{self.SESSION}:{name}",
            "-o", f"cat >> {log_file}",
        )
        await proc.wait()

        log.info("Spawned agent %s in tmux window", name)

    async def get_pid(self, name: str) -> int | None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-panes", "-t", f"{self.SESSION}:{name}",
            "-F", "#{pane_pid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        pid_str = stdout.decode().strip()
        try:
            return int(pid_str)
        except ValueError:
            return None

    async def send_text(self, name: str, text: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{self.SESSION}:{name}", text, "Enter",
        )
        await proc.wait()

    async def kill_terminal(self, name: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-window", "-t", f"{self.SESSION}:{name}",
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


class VSCodeBackend(Backend):
    """Communicates with the foreman-vscode extension via Unix socket."""

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

    async def create_terminal(
        self, name: str, command: str, log_file: Path,
    ) -> None:
        await self._send({
            "action": "create_terminal",
            "name": name,
            "command": command,
        })

    async def get_pid(self, name: str) -> int | None:
        return None  # VS Code backend doesn't support PID querying

    async def send_text(self, name: str, text: str) -> None:
        await self._send({"action": "send_text", "name": name, "text": text})

    async def kill_terminal(self, name: str) -> None:
        await self._send({"action": "kill_terminal", "name": name})


def _build_launcher_script(
    plan: Plan,
    worktree_path: Path,
    agent_type: str,
    config: Config,
    initial_message: str,
) -> str:
    """Generate the launcher bash script content."""
    prompt_path = (config.repo_root / config.prompts.get(agent_type, "")).resolve()
    plans_dir = config.plans_dir.resolve()
    tools = config.allowed_tools.get(agent_type, "")

    lines = [
        "#!/bin/bash",
        f"cd {shlex.quote(str(worktree_path.resolve()))}",
    ]

    cmd_parts = [
        f"exec {CLAUDE_BIN}",
        f'  --append-system-prompt "$(cat {shlex.quote(str(prompt_path))})"',
        f"  --permission-mode {shlex.quote(config.agents.permission_mode)}",
        f"  --model {shlex.quote(config.agents.model)}",
        f"  --name {shlex.quote(f'foreman:{plan.name}')}",
        f"  --add-dir {shlex.quote(str(plans_dir))}",
    ]

    if tools:
        cmd_parts.append(f"  --allowed-tools {shlex.quote(tools)}")

    cmd_parts.append(f"  {shlex.quote(initial_message)}")

    lines.append(" \\\n".join(cmd_parts))
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

    async def teardown(self) -> None:
        await self.backend.teardown()

    async def spawn_agent(
        self,
        plan: Plan,
        worktree_path: Path,
        agent_type: str,
        initial_message: str,
    ) -> int | None:
        """Write launcher script, spawn agent in terminal, return PID."""
        script_content = _build_launcher_script(
            plan, worktree_path, agent_type, self.config, initial_message,
        )

        script_path = self.config.scripts_dir / f"{plan.name}-{agent_type}.sh"
        script_path.write_text(script_content)
        script_path.chmod(0o755)

        log_file = self.config.log_dir / f"{plan.name}-{agent_type}.log"
        # Ensure log file exists for inotify watch
        log_file.touch()

        await self.backend.create_terminal(
            name=plan.name,
            command=f"bash {shlex.quote(str(script_path))}",
            log_file=log_file,
        )

        # Query PID immediately after creation
        pid = await self.backend.get_pid(plan.name)
        log.info("Spawned %s agent for %s (PID: %s)", agent_type, plan.name, pid)
        return pid

    async def notify_agent(self, plan_name: str, message: str) -> None:
        await self.backend.send_text(plan_name, message)

    async def kill_agent(self, plan_name: str) -> None:
        await self.backend.kill_terminal(plan_name)
