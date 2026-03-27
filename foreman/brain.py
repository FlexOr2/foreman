"""Persistent Claude CLI session that provides intelligence when needed."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import foreman.config as _config

log = logging.getLogger(__name__)


def _read_file_or_none(path: Path) -> str | None:
    try:
        text = path.read_text().strip()
        return text or None
    except FileNotFoundError:
        return None


class ForemanBrain:
    def __init__(self, foreman_dir: Path, allowed_tools: str, permission_mode: str, timeout: int = 300) -> None:
        self._session_file = foreman_dir / "session_id"
        self._context_file = foreman_dir / "context.md"
        self._allowed_tools = allowed_tools
        self._permission_mode = permission_mode
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self.session_id: str | None = _read_file_or_none(self._session_file)
        if self.session_id:
            log.info("Loaded brain session: %s", self.session_id)

    def save(self) -> None:
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        self._session_file.write_text(self.session_id or "")

    async def think(self, prompt: str) -> str:
        async with self._lock:
            try:
                return await self._invoke(prompt)
            except asyncio.TimeoutError:
                self.session_id = None
                self.save()
                raise
            except Exception:
                log.warning("Brain invocation failed, starting fresh session", exc_info=True)
                self.session_id = None
                context = _read_file_or_none(self._context_file)
                if context:
                    prompt = f"Context from previous session:\n{context}\n\n---\n\n{prompt}"
                return await self._invoke(prompt)

    async def _invoke(self, prompt: str) -> str:
        cmd = [
            _config.CLAUDE_BIN, "-p", prompt,
            "--output-format", "json",
            "--allowed-tools", self._allowed_tools,
            "--permission-mode", self._permission_mode,
        ]
        if self.session_id:
            cmd += ["--resume", self.session_id]

        log.debug("Brain invocation: session=%s, prompt length=%d", self.session_id, len(prompt))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        start = time.monotonic()
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            log.error(
                "Brain invocation timed out after %.0fs (prompt_len=%d, session=%s)",
                elapsed, len(prompt), self.session_id,
            )
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            raise RuntimeError(f"Brain claude -p failed (rc={proc.returncode}): {stderr.decode().strip()}")

        response = json.loads(stdout.decode())
        self.session_id = response.get("session_id", self.session_id)
        self.save()

        result = response.get("result", "")
        log.info("Brain responded (session=%s, result length=%d)", self.session_id, len(result))
        return result

    async def summarize_and_reset(self) -> None:
        summary = await self.think(
            "Summarize everything you know about this orchestration session: "
            "what plans were executed, what merges happened, what conflicts were resolved, "
            "and any important context for future work. Be concise but complete."
        )
        self._context_file.write_text(summary)
        self.session_id = None
        self.save()
        log.info("Brain session summarized and reset")
