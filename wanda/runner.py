from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

KILL_GRACE_S = 10


@dataclass
class RunResult:
    ok: bool
    timed_out: bool = False
    exit_code: int | None = None
    envelope: dict[str, Any] | None = None
    structured: Any = None
    result_text: str | None = None
    session_id: str | None = None
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class RunnerService:
    """All claude -p subprocess handling and envelope parsing lives here, so
    CLI drift across versions is a one-file fix."""

    claude_bin: str
    triage_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    agent_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))

    async def run(
        self,
        prompt: str,
        *,
        model: str,
        max_budget_usd: float,
        timeout_s: int,
        output_schema: dict | None = None,
        no_tools: bool = False,
        tools: str | None = None,
        system_prompt: str | None = None,
        session_id: str | None = None,
        resume: str | None = None,
        allowed_tools: str | None = None,
        permission_mode: str | None = None,
        restricted: bool = False,
        setting_sources: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        argv = [
            self.claude_bin,
            "-p",
            "--output-format", "json",
            "--model", model,
            "--max-budget-usd", str(max_budget_usd),
        ]
        if output_schema is not None:
            argv += ["--json-schema", json.dumps(output_schema)]
        if no_tools:
            argv += ["--tools", "", "--no-session-persistence"]
        elif tools:
            argv += ["--tools", tools]
        if system_prompt:
            argv += ["--system-prompt", system_prompt]
        if session_id:
            argv += ["--session-id", session_id]
        if resume:
            argv += ["--resume", resume]
        if allowed_tools:
            argv += ["--allowedTools", allowed_tools]
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        if restricted:
            # Confines file tools to the working directory, drops code-running
            # tools, and ignores user/project settings — the containment that
            # matters when a prompt carries attacker-controlled email text.
            argv += ["--restricted", "--strict-mcp-config"]
        if setting_sources:
            argv += ["--setting-sources", setting_sources]

        # start_new_session so a timeout can kill the whole process group —
        # claude spawns children for shell tools that would otherwise orphan.
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,  # prompt goes via stdin: no ARG_MAX/quoting limits
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            cwd=cwd,
            env={**os.environ, **env} if env else None,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=timeout_s
            )
        except TimeoutError:
            await self._kill_group(proc)
            # The envelope (and the true cost) is lost, so charge the budget
            # pessimistically rather than letting a killed run look free.
            return RunResult(
                ok=False, timed_out=True, cost_usd=max_budget_usd,
                error=f"timed out after {timeout_s}s",
            )
        except asyncio.CancelledError:
            # Daemon shutdown. Without this the subprocess survives in its own
            # session (start_new_session), outliving even launchd's cleanup.
            self._kill_group_now(proc)
            raise

        rr = self._parse(proc.returncode, stdout, stderr)
        if rr.envelope is None:
            rr.cost_usd = max_budget_usd
        return rr

    @staticmethod
    def _kill_group_now(proc: asyncio.subprocess.Process) -> None:
        """Synchronous best-effort group kill for teardown paths that cannot await."""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    @classmethod
    async def _kill_group(cls, proc: asyncio.subprocess.Process) -> None:
        pgid = proc.pid  # start_new_session made the child its own group leader
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        cancelled = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            # Shutdown arrived mid-grace. Fall through to SIGKILL rather than
            # leaving the group with only a SIGTERM it may have trapped.
            cancelled = True
        # Always SIGKILL the group: reaping the direct child says nothing about
        # grandchildren spawned by its tools, which can survive SIGTERM.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if cancelled:
            raise asyncio.CancelledError
        await proc.wait()

    @staticmethod
    def _parse(exit_code: int | None, stdout: bytes, stderr: bytes) -> RunResult:
        text = stdout.decode("utf-8", "replace").strip()
        try:
            envelope = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            envelope = None
        if not isinstance(envelope, dict):
            # Valid JSON that isn't an envelope (null, a list, a bare string)
            # must fail like malformed output, not raise on .get().
            err = stderr.decode("utf-8", "replace").strip()
            return RunResult(
                ok=False,
                exit_code=exit_code,
                error=f"unparseable envelope (exit {exit_code}): {text[:500] or err[:500]}",
            )
        try:
            cost = float(envelope.get("total_cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        result = RunResult(
            ok=(exit_code == 0 and not envelope.get("is_error")),
            exit_code=exit_code,
            envelope=envelope,
            structured=envelope.get("structured_output"),
            result_text=envelope.get("result"),
            session_id=envelope.get("session_id"),
            cost_usd=cost,
        )
        if not result.ok:
            result.error = envelope.get("result") or envelope.get("subtype") or "claude reported an error"
        return result
