from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KILL_GRACE_S = 10
MIN_BILLABLE_S = 10  # below this, a failed run bought no tokens
# How long what a session left running is given to go after each signal:
# bounded, since a process in a call on a stalled mount ends only when the
# call returns, and the session's turn waits for this.
LEFT_GRACE_S = 5


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
    # how many processes the session left running, ended when it ended
    left_running: int = 0


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
        append_system_prompt: str | None = None,
        session_id: str | None = None,
        resume: str | None = None,
        allowed_tools: str | None = None,
        permission_mode: str | None = None,
        setting_sources: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        inherit_env: bool = True,
        mark: str | None = None,
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
        if append_system_prompt:
            argv += ["--append-system-prompt", append_system_prompt]
        if session_id:
            argv += ["--session-id", session_id]
        if resume:
            argv += ["--resume", resume]
        if allowed_tools:
            argv += ["--allowedTools", allowed_tools]
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        if setting_sources:
            argv += ["--setting-sources", setting_sources]

        # start_new_session so a timeout can kill the whole process group —
        # claude spawns children for shell tools that would otherwise orphan.
        # Claude Code may start a Bash command in a session of its own, outside
        # that group (its code asks for detached): given `mark`,
        # end_left_behind ends what the command left, in the group or out of it.
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,  # prompt goes via stdin: no ARG_MAX/quoting limits
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            cwd=cwd,
            # inherit_env=False: `env` is the child's whole environment, for a
            # caller that has to keep some of the daemon's own out of it
            env=({**os.environ, **env} if inherit_env else env) if env else None,
        )
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), timeout=timeout_s
                )
            except TimeoutError:
                await self._kill_group(proc)
                left = await self._end_left_behind(proc.pid, mark)
                # The envelope (and the true cost) is lost, so charge the budget
                # pessimistically rather than letting a killed run look free.
                return RunResult(
                    ok=False, timed_out=True, cost_usd=max_budget_usd,
                    error=f"timed out after {timeout_s}s", left_running=left,
                )
            except asyncio.CancelledError:
                # Daemon shutdown. Without this the subprocess survives in its own
                # session (start_new_session), outliving even launchd's cleanup.
                self._kill_group_now(proc)
                raise
            left = await self._end_left_behind(proc.pid, mark)
        except asyncio.CancelledError:
            if mark:
                # at shutdown nothing more can be awaited
                end_left_behind(proc.pid, mark, grace_s=0)
            raise

        rr = self._parse(proc.returncode, stdout, stderr)
        rr.left_running = left
        if rr.envelope is None:
            # No envelope means the true cost is unknown, so charge the ceiling
            # — unless it exited too fast to have bought anything (a bad flag,
            # a missing binary), where billing $2 a time would trip the daily
            # breaker after a few failures.
            rr.cost_usd = max_budget_usd if time.monotonic() - t0 > MIN_BILLABLE_S else 0.0
        return rr

    @staticmethod
    async def _end_left_behind(leader: int, mark: str | None) -> int:
        return await asyncio.to_thread(end_left_behind, leader, mark) if mark else 0

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


def end_left_behind(leader: int, mark: str, grace_s: float = LEFT_GRACE_S) -> int:
    """Ends whatever a session left running, and returns how many processes
    that was: every process in its process session, which `leader`, the
    claude it ran, heads, and every process carrying `mark`, an entry of the
    session's environment, which reaches what Claude Code may start in
    sessions of their own (its code asks for detached), its Bash commands.
    Claude Code ends none of them: at its Bash timeout a command is moved to
    the background, or only its shell is ended, and what the command started
    goes on, a `mem` waiting for the vault among them, to write after the
    session is recorded and snapshotted. SIGTERM, then SIGKILL, each followed
    by up to `grace_s` for them to be gone."""
    found: set[int] = set()
    left: set[int] = set()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        left = _left_behind(leader, mark)
        found |= left
        for pid in left:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
        deadline = time.monotonic() + grace_s
        while left and time.monotonic() < deadline:
            time.sleep(0.05)
            left &= _left_behind(leader, mark)
    if found:
        # without a wait, what is still there says nothing
        still = sorted(left) if grace_s else []
        log.warning("%s left %d process(es) running when it ended (%s); ended them%s", mark, len(found),
                    ", ".join(map(str, sorted(found))),
                    f", but {', '.join(map(str, still))} still ran {grace_s} s after SIGKILL" if still else "")
    else:
        log.info("%s left nothing running when it ended", mark)
    return len(found)


def _left_behind(leader: int, mark: str) -> set[int]:
    """The processes, ended ones aside, in `leader`'s process session or
    with `mark` in their environment, but for `leader` itself, the runner's
    own child, which it ends and waits for. A process's environment is read
    from /proc; where there is none (macOS, where only the tests run), the
    process session alone is found."""
    skip = {os.getpid(), leader}
    found = set()
    entry = b"\0" + mark.encode() + b"\0"
    proc = Path("/proc")
    if not proc.is_dir():
        listed = subprocess.run(["ps", "-A", "-o", "pid=,stat="], capture_output=True, text=True).stdout
        for line in listed.splitlines():
            pid, state = (line.split() + ["", ""])[:2]
            if pid.isdigit() and int(pid) not in skip and not state.startswith("Z"):
                with contextlib.suppress(OSError):
                    if os.getsid(int(pid)) == leader:
                        found.add(int(pid))
        return found
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) in skip:
            continue
        try:
            stat = (d / "stat").read_bytes()
            # the fields after the command's name, which can hold anything
            state, _, _, session = stat[stat.rindex(b")") + 2:].split()[:4]
            if state == b"Z":
                continue
            if int(session) == leader or entry in b"\0" + (d / "environ").read_bytes():
                found.add(int(d.name))
        except (OSError, ValueError):
            continue
    return found
