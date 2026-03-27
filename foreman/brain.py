"""Persistent Claude CLI session that provides intelligence when needed."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CLAUDE_BIN = "claude"


class ForemanBrain:
    """Persistent Claude CLI session via -p --resume.

    All invocations are serialized via asyncio.Lock to prevent
    concurrent --resume calls to the same session.
    """

    def __init__(self, foreman_dir: Path, allowed_tools: str = "Read,Edit,Bash,Glob,Grep") -> None:
        self._foreman_dir = foreman_dir
        self._session_file = foreman_dir / "session_id"
        self._context_file = foreman_dir / "context.md"
        self._allowed_tools = allowed_tools
        self._lock = asyncio.Lock()
        self.session_id: str | None = self._load_session_id()

    def _load_session_id(self) -> str | None:
        if self._session_file.exists():
            sid = self._session_file.read_text().strip()
            if sid:
                log.info("Loaded brain session: %s", sid)
                return sid
        return None

    def _save_session_id(self) -> None:
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        self._session_file.write_text(self.session_id or "")

    async def think(self, prompt: str) -> str:
        """Send a prompt to the persistent Claude session.

        Returns the text result. Serialized — only one call at a time.
        """
        async with self._lock:
            try:
                return await self._invoke(prompt)
            except Exception:
                log.warning("Brain invocation failed, starting fresh session", exc_info=True)
                self.session_id = None
                context = self._load_context()
                if context:
                    prompt = f"Context from previous session:\n{context}\n\n---\n\n{prompt}"
                return await self._invoke(prompt)

    async def _invoke(self, prompt: str) -> str:
        cmd = [
            CLAUDE_BIN, "-p", prompt,
            "--output-format", "json",
            "--allowed-tools", self._allowed_tools,
            "--permission-mode", "dontAsk",
        ]
        if self.session_id:
            cmd += ["--resume", self.session_id]

        log.debug("Brain invocation: session=%s, prompt length=%d", self.session_id, len(prompt))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            raise RuntimeError(f"Brain claude -p failed (rc={proc.returncode}): {error_msg}")

        response = json.loads(stdout.decode())
        self.session_id = response.get("session_id", self.session_id)
        self._save_session_id()

        result = response.get("result", "")
        log.info("Brain responded (session=%s, result length=%d)", self.session_id, len(result))
        return result

    def _load_context(self) -> str | None:
        if self._context_file.exists():
            return self._context_file.read_text().strip()
        return None

    async def summarize_and_reset(self) -> None:
        """Ask the brain to summarize its knowledge, save to context.md, start fresh."""
        summary = await self.think(
            "Summarize everything you know about this orchestration session: "
            "what plans were executed, what merges happened, what conflicts were resolved, "
            "and any important context for future work. Be concise but complete."
        )
        self._context_file.write_text(summary)
        self.session_id = None
        self._save_session_id()
        log.info("Brain session summarized and reset")
