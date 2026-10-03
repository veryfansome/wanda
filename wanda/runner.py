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
# The longest line a streamed session's output may hold. Each event is one
# line, and one carrying a tool's result holds that result twice; asyncio's
# default of 64 KiB is less than a large file a session reads.
STREAM_LINE_LIMIT = 64 * 1024 * 1024
# Every type of event Claude Code 2.1.268 writes on a streamed session's
# output, as its code lists them. An event of another type means the pinned
# version has moved, and with it the shapes the harness reads (an added
# message's handing, a further turn, a notice), so the session fails rather
# than being read as if nothing had changed.
STREAM_EVENTS = frozenset({
    "assistant", "user", "result", "system", "stream_event", "tool_progress", "tool_use_summary",
    "auth_status", "rate_limit_event", "prompt_suggestion", "conversation_reset",
    "command_lifecycle", "transcript_mirror", "active_goal", "autocompact_state", "control_request",
    "control_response", "control_cancel_request", "keep_alive",
})
# what a result must carry for the harness to read it: its outcome and cost,
# and a successful one its text
RESULT_FIELDS = (("subtype", str), ("is_error", bool), ("total_cost_usd", (int, float)))


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
    # every result a streamed session gave, in order, one a turn
    results: list[dict] = field(default_factory=list)


def user_line(text: str) -> bytes:
    """One message on a streamed session's input, as one text block. Claude
    Code joins messages that wait for the same turn into one: strings with a
    newline, which no reader can split again, and text blocks one after
    another, which a transcript keeps apart."""
    return (json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": text}]}}) + "\n").encode()


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
        feed=None,
    ) -> RunResult:
        """`feed`, when given, keeps the session's input open while it works,
        for the messages `feed.next()` hands it, and is handed each result as
        it comes, in `feed.results` (see `_streamed`)."""
        argv = [
            self.claude_bin,
            "-p",
            *(("--input-format", "stream-json", "--output-format", "stream-json", "--verbose")
              if feed is not None else ("--output-format", "json")),
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
            **({"limit": STREAM_LINE_LIMIT} if feed is not None else {}),
        )
        if feed is not None:
            return await self._streamed(proc, prompt, feed, timeout_s, max_budget_usd, t0, mark)
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
                # session (start_new_session), outliving the daemon.
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

    async def _streamed(self, proc: asyncio.subprocess.Process, prompt: str, feed, timeout_s: int,
                        max_budget_usd: float, t0: float, mark: str | None) -> RunResult:
        """A session whose input stays open while it works. The prompt is its
        first message; each one `feed.next()` hands over is written as it
        comes, from the start of the first turn until the session's first
        result, which closes `feed` and the input. Claude Code hands a message
        written while a turn runs to that turn at its next step, and one
        written after the turn's last step to a further turn of the same
        session, which gives a result of its own: the session's outcome is
        its last result, read once its output has ended, and `results` holds
        them all. Which messages it was handed, its transcript says. What it
        leaves running is ended as `run` ends it. An event it does not know
        fails the session, as an output it cannot read does."""
        # the feed's own list: a shutdown that cancels this still leaves the
        # caller every answer the session gave
        results: list[dict] = feed.results
        began = asyncio.Event()

        async def write_input() -> None:
            try:
                proc.stdin.write(user_line(prompt))
                await proc.stdin.drain()
                # nothing more until the first turn has begun (`system`/
                # `init`): Claude Code takes every message waiting when a turn
                # starts into that turn as one message, which would make an
                # addition part of the prompt
                await began.wait()
                while (text := await feed.next()) is not None:
                    proc.stdin.write(user_line(text))
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # it has ended; its transcript says what it was handed
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.close()

        async def read_output() -> None:
            async for line in proc.stdout:
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    ev = None
                kind = ev.get("type") if isinstance(ev, dict) else None
                if kind not in STREAM_EVENTS:
                    raise ValueError(f"an event of a type this runner does not know: {kind!r}")
                if kind == "result":
                    missing = [k for k, t in RESULT_FIELDS if not isinstance(ev.get(k), t)]
                    if ev.get("subtype") == "success" and not isinstance(ev.get("result"), str):
                        missing.append("result")
                    if missing:
                        raise ValueError(f"a result without {', '.join(missing)}")
                if ev.get("type") == "system" and ev.get("subtype") == "init":
                    began.set()
                elif ev.get("type") == "result":
                    results.append(ev)
                    feed.close()
                    # the input closes now, not once a message being framed
                    # is ready: that one goes back on its list
                    writer.cancel()
            await proc.wait()

        writer = asyncio.create_task(write_input())
        errors = asyncio.create_task(proc.stderr.read())
        try:
            try:
                await asyncio.wait_for(read_output(), timeout=timeout_s)
            except TimeoutError:
                feed.close()
                await self._kill_group(proc)
                left = await self._end_left_behind(proc.pid, mark)
                return RunResult(ok=False, timed_out=True, cost_usd=max_budget_usd, results=results,
                                 error=f"timed out after {timeout_s}s", left_running=left)
            except asyncio.CancelledError:
                self._kill_group_now(proc)
                raise
            except Exception as e:
                # an output it cannot read is a failed session, ended like one
                # that ran out of time
                await self._kill_group(proc)
                left = await self._end_left_behind(proc.pid, mark)
                return RunResult(ok=False, cost_usd=max_budget_usd, results=results,
                                 error=f"could not read the session's output: {e}", left_running=left)
            finally:
                feed.close()
                for t in (writer, errors):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(writer, errors, return_exceptions=True)
            left = await self._end_left_behind(proc.pid, mark)
        except asyncio.CancelledError:
            if mark:
                # at shutdown nothing more can be awaited
                end_left_behind(proc.pid, mark, grace_s=0)
            raise
        stderr = errors.result() if not errors.cancelled() and errors.exception() is None else b""
        if not results:
            err = stderr.decode("utf-8", "replace").strip()
            return RunResult(
                ok=False, exit_code=proc.returncode, left_running=left,
                # as for an envelope that never came: the ceiling, unless it
                # ended too soon to have bought anything
                cost_usd=max_budget_usd if time.monotonic() - t0 > MIN_BILLABLE_S else 0.0,
                error=f"no result (exit {proc.returncode}): {err[:500]}",
            )
        if proc.returncode != 0 and not results[-1].get("is_error"):
            # an exit that says it failed after a result that says it
            # succeeded (a crash, or a kill from outside): no result says what
            # failed or what came after it cost, and a success's text is the
            # session's report, never an error to post
            err = stderr.decode("utf-8", "replace").strip()
            return RunResult(ok=False, exit_code=proc.returncode, results=results, left_running=left,
                             cost_usd=max_budget_usd if time.monotonic() - t0 > MIN_BILLABLE_S else 0.0,
                             error=f"claude exited {proc.returncode} after its last result"
                             + (f": {err[:500]}" if err else ""))
        rr = self._parse(proc.returncode, json.dumps(results[-1]).encode(), stderr)
        rr.results = results
        rr.left_running = left
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
