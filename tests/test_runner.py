import asyncio
import contextlib
import fcntl
import json
import os
import signal
import stat
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from wanda import vault
from wanda.runner import RunnerService, RunResult, refused


def make_fake_claude(tmp_path, script: str) -> str:
    path = tmp_path / "fake-claude"
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def run(coro):
    return asyncio.run(coro)


def test_parse_success_envelope():
    envelope = {
        "type": "result", "is_error": False, "result": "{\"ok\": true}",
        "structured_output": {"ok": True}, "session_id": "abc", "total_cost_usd": 0.018,
    }
    rr = RunnerService._parse(0, json.dumps(envelope).encode(), b"")
    assert rr.ok and rr.session_id == "abc" and rr.structured == {"ok": True}
    assert rr.cost_usd == 0.018


def test_parse_error_envelope():
    envelope = {"type": "result", "is_error": True, "result": "budget exceeded", "session_id": "abc"}
    rr = RunnerService._parse(0, json.dumps(envelope).encode(), b"")
    assert not rr.ok and "budget" in rr.error


def test_parse_garbage():
    rr = RunnerService._parse(1, b"not json at all", b"stderr says boom")
    assert not rr.ok and rr.timed_out is False and "unparseable" in rr.error


def test_prompt_goes_via_stdin_and_envelope_roundtrips(tmp_path):
    fake = make_fake_claude(
        tmp_path,
        'input=$(cat)\n'
        'printf \'{"type":"result","is_error":false,"result":"%s","session_id":"s1","total_cost_usd":0.01}\' "$input"',
    )
    rr = run(RunnerService(fake).run("hello", model="m", max_budget_usd=1, timeout_s=10))
    assert rr.ok and rr.result_text == "hello" and rr.session_id == "s1"


def test_timeout_kills_process_group(tmp_path):
    fake = make_fake_claude(tmp_path, "cat > /dev/null\nsleep 30")
    start = time.monotonic()
    rr = run(RunnerService(fake).run("x", model="m", max_budget_usd=1, timeout_s=1))
    assert rr.timed_out and not rr.ok
    assert time.monotonic() - start < 15  # killed, not waited out


def test_timeout_charges_pessimistic_cost(tmp_path):
    """A killed run's envelope is lost; charging $0 would blind the breaker."""
    fake = make_fake_claude(tmp_path, "cat > /dev/null\nsleep 30")
    rr = run(RunnerService(fake).run("x", model="m", max_budget_usd=0.25, timeout_s=1))
    assert rr.timed_out and rr.cost_usd == 0.25


def test_fast_unparseable_failure_is_not_billed(tmp_path):
    """A run that dies immediately (bad flag, missing binary) bought no tokens.
    Billing it the ceiling tripped the daily breaker after a few failures."""
    fake = make_fake_claude(tmp_path, "cat > /dev/null\necho 'not json'")
    rr = run(RunnerService(fake).run("x", model="m", max_budget_usd=0.25, timeout_s=10))
    assert not rr.ok and rr.cost_usd == 0.0


def test_cancellation_kills_the_process_group(tmp_path):
    """Daemon shutdown cancels mid-run; the subprocess is in its own session,
    so nothing else will reap it."""
    marker = tmp_path / "grandchild-alive"
    fake = make_fake_claude(
        tmp_path,
        f"cat > /dev/null\n( while true; do touch {marker}; sleep 0.1; done ) &\nsleep 30",
    )

    async def scenario():
        runner = RunnerService(fake)
        task = asyncio.create_task(runner.run("x", model="m", max_budget_usd=1, timeout_s=60))
        await asyncio.sleep(1.5)  # let the group start
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # Let the child watcher reap the killed process, as the daemon's
        # shutdown grace period does, instead of closing the loop under it.
        await asyncio.sleep(0.3)

    run(scenario())
    time.sleep(0.5)
    marker.unlink(missing_ok=True)
    time.sleep(0.6)  # if the group survived, it would recreate the marker
    assert not marker.exists(), "process group survived cancellation"


def test_nonzero_exit_is_error(tmp_path):
    fake = make_fake_claude(tmp_path, "cat > /dev/null\necho '{\"is_error\": false}'\nexit 3")
    rr = run(RunnerService(fake).run("x", model="m", max_budget_usd=1, timeout_s=10))
    assert not rr.ok and rr.exit_code == 3


def recording_claude(tmp_path):
    """A fake CLI that writes each argument it was given on its own line."""
    args = tmp_path / "args"
    fake = make_fake_claude(
        tmp_path,
        f'cat > /dev/null\nfor a in "$@"; do printf "%s\\n" "$a"; done > {args}\n'
        'echo \'{"type":"result","is_error":false,"result":"ok","session_id":"s1"}\'',
    )
    return fake, args


def test_append_system_prompt_reaches_the_cli_as_one_argument(tmp_path):
    fake, args = recording_claude(tmp_path)
    text = 'I am wanda. "I" means me.'
    rr = run(RunnerService(fake).run("x", model="m", max_budget_usd=1, timeout_s=10,
                                     append_system_prompt=text))
    argv = args.read_text().splitlines()
    assert rr.ok and argv[argv.index("--append-system-prompt") + 1] == text


def test_no_append_system_prompt_unless_given(tmp_path):
    fake, args = recording_claude(tmp_path)
    run(RunnerService(fake).run("x", model="m", max_budget_usd=1, timeout_s=10))
    assert "--append-system-prompt" not in args.read_text().splitlines()


def test_a_whole_environment_replaces_the_daemons(tmp_path, monkeypatch):
    """inherit_env=False hands the child exactly what it was given."""
    monkeypatch.setenv("WANDA_SLACK_BOT_TOKEN", "xoxb-secret")
    seen = tmp_path / "env"
    fake = make_fake_claude(tmp_path, f"cat > /dev/null\nenv > {seen}\n"
                            'echo \'{"type":"result","is_error":false,"result":"ok","session_id":"s1"}\'')
    run(RunnerService(fake).run("x", model="m", max_budget_usd=1, timeout_s=10,
                                env={"PATH": "/usr/bin:/bin", "MEM_DATE": "2026-10-01"}, inherit_env=False))
    child = seen.read_text()
    assert "MEM_DATE=2026-10-01" in child and "xoxb-secret" not in child
    run(RunnerService(fake).run("x", model="m", max_budget_usd=1, timeout_s=10, env={"MEM_DATE": "d"}))
    assert "xoxb-secret" in seen.read_text(), "by default the child inherits the daemon's environment"


# A stand-in for claude that runs one Bash command with the CLI's environment,
# in a session of its own, as Claude Code's code asks (detached), or, with
# OWN_SESSION=0, in claude's own, where a command can also be left; ends
# only the command's shell after RUN_FOR seconds, as Claude Code ends a
# background command's after the session's result, or any command's at its
# Bash timeout with background tasks off, then waits THEN seconds and answers.
STAND_IN = """import json, os, subprocess, sys, time
sys.stdin.read()
shell = subprocess.Popen(["/bin/sh", "-c", os.environ["COMMAND"]],
                         start_new_session=os.environ.get("OWN_SESSION") != "0",
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(float(os.environ["RUN_FOR"]))
shell.kill()
shell.wait()
time.sleep(float(os.environ["THEN"]))
print(json.dumps({"type": "result", "is_error": False, "result": "ok", "session_id": "s1"}))
"""
# A `mem` write, as `mem` takes the vault for one: held exclusively, waited for.
WAITING_MEM = """import fcntl, os
vault = os.environ["MEM_VAULT"]
fcntl.flock(os.open(vault, os.O_RDONLY), fcntl.LOCK_EX)
open(os.path.join(vault, "node.md"), "w").write("written after its session")
"""
NO_PROC = pytest.mark.skipif(not Path("/proc").is_dir(),
                             reason="what a command started in a session of its own carries is read from /proc")


def stand_in(tmp_path) -> str:
    (tmp_path / "stand_in.py").write_text(STAND_IN)
    return make_fake_claude(tmp_path, f'exec "{sys.executable}" "{tmp_path / "stand_in.py"}"')


def running(pid: int) -> bool:
    """Not ended, a zombie counting as ended."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_file = Path(f"/proc/{pid}/stat")
    with contextlib.suppress(OSError):
        return stat_file.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    return True


def gone_soon(pid: int) -> bool:
    deadline = time.monotonic() + 3
    while running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not running(pid)


def session(tmp_path, command: str, ending: str, own_session: bool, mark: bool):
    """Runs the stand-in to `ending`, its command leaving a process behind
    that writes its pid to `pid`; returns that pid."""
    sid = str(uuid.uuid4())
    env = {"COMMAND": command, "MEM_SESSION": sid, "OWN_SESSION": "1" if own_session else "0",
           "RUN_FOR": "0.5", "THEN": "30" if ending in ("timed out", "cancelled") else "0",
           "MEM_VAULT": str(tmp_path / "vault")}

    async def go():
        runner = RunnerService(stand_in(tmp_path))
        task = asyncio.create_task(runner.run(
            "x", model="m", max_budget_usd=1, timeout_s=2 if ending == "timed out" else 60, env=env,
            mark=f"MEM_SESSION={sid}" if mark else None))
        if ending == "cancelled":
            await asyncio.sleep(1.5)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.3)
        else:
            rr = await task
            assert rr.ok == (ending == "finished")
            if ending == "finished":
                # what doctor counts: a session that left something running
                assert (rr.left_running > 0) == mark

    run(go())
    return int((tmp_path / "pid").read_text())


@pytest.mark.parametrize("ending", ["finished", "timed out", "cancelled"])
@pytest.mark.parametrize("own_session", [False, pytest.param(True, marks=NO_PROC)],
                         ids=["in claude's session", "in a session of its own"])
def test_what_a_session_leaves_running_is_ended_when_it_ends(tmp_path, ending, own_session):
    """Claude Code ends neither a command past its timeout nor what the
    command started; the session's end does, however the session ends."""
    left = session(tmp_path, f"sleep 60 & echo $! > {tmp_path / 'pid'}; wait", ending, own_session, mark=True)
    assert gone_soon(left)


@pytest.mark.parametrize("own_session", [False, pytest.param(True, marks=NO_PROC)],
                         ids=["in claude's session", "in a session of its own"])
def test_a_mem_waiting_for_the_vault_writes_nothing_after_its_session(tmp_path, own_session):
    """Held behind a snapshot or a hand-run write, a session's `mem` would
    otherwise write once the vault is free, after the session was recorded."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "mem.py").write_text(WAITING_MEM)
    command = f'"{sys.executable}" "{tmp_path / "mem.py"}" & echo $! > {tmp_path / "pid"}; wait'
    for mark in (False, True):
        held = os.open(vault, os.O_RDONLY)
        fcntl.flock(held, fcntl.LOCK_SH)  # as a snapshot holds it
        try:
            left = session(tmp_path, command, "finished", own_session, mark=mark)
            assert running(left) != mark
        finally:
            os.close(held)
        if not mark:
            # without the mark nothing ends it: it writes once the vault is free
            deadline = time.monotonic() + 3
            while not (vault / "node.md").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert (vault / "node.md").exists()
            (vault / "node.md").unlink()
            continue
        time.sleep(0.5)
        assert not (vault / "node.md").exists()


@NO_PROC
def test_only_what_the_session_started_is_ended(tmp_path):
    """Another session's processes, and the daemon's own, carry another mark
    and sit in other process sessions."""
    import subprocess
    other = subprocess.Popen(["sleep", "30"], start_new_session=True,
                             env=os.environ | {"MEM_SESSION": str(uuid.uuid4())})
    try:
        left = session(tmp_path, f"sleep 60 & echo $! > {tmp_path / 'pid'}; wait", "finished", True, mark=True)
        assert gone_soon(left) and running(other.pid)
    finally:
        other.kill()
        other.wait()


def test_what_will_not_stop_when_asked_is_killed(tmp_path):
    """SIGTERM first, then SIGKILL for whatever ignores it, each wait bounded."""
    import subprocess

    from wanda.runner import end_left_behind

    ready, pid_file = tmp_path / "ready", tmp_path / "pid"
    stubborn = (f'"{sys.executable}" -c "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                f"open('{ready}', 'w').write('x'); time.sleep(60)\" & echo $! > {pid_file}")
    # a session whose leader has ended, as a finished claude's has
    leader = subprocess.Popen(["/bin/sh", "-c", stubborn], start_new_session=True, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    leader.wait()
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    left = int(pid_file.read_text())
    try:
        t0 = time.monotonic()
        assert end_left_behind(leader.pid, f"MEM_SESSION={uuid.uuid4()}", grace_s=0.5) == 1
        # it outlasted SIGTERM's wait, and SIGKILL ended it
        assert gone_soon(left) and 0.5 <= time.monotonic() - t0 < 3
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(left, signal.SIGKILL)


# --- a session whose input stays open (tests/claude_standin.py replays the
# shapes the pinned CLI wrote for sessions with their input open) ---

STANDIN = Path(__file__).with_name("claude_standin.py")


def standin(tmp_path, monkeypatch, **behaviour) -> str:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("STANDIN", json.dumps(behaviour))
    return make_fake_claude(tmp_path, f'exec "{sys.executable}" "{STANDIN}" "$@"')


async def moment(at, vault_dir):
    """Waits for `at`: seconds, or (what, n, then): until the session's
    transcript holds n lines recording `what` (a tool's call or result, or
    the answer), and `then` seconds more."""
    if not isinstance(at, tuple):
        await asyncio.sleep(at)
        return
    what, n, then = at
    d = vault.transcripts_dir(vault_dir)
    for _ in range(800):
        lines = [x for f in d.glob("*.jsonl") for x in f.read_text().splitlines()] if d.exists() else []
        if sum(f'"{what}"' in x for x in lines) >= n:
            break
        await asyncio.sleep(0.025)
    await asyncio.sleep(then)


class Feed:
    """What the runner reads a session's added messages from: next(), close()
    and results, as the product's feed has them. Each text put on `waiting`
    is handed over in turn, indented as the product frames a message, after
    `frame_s`, until it is closed; `written` holds those it handed over."""

    def __init__(self, frame_s=0.0):
        self.waiting: list[str] = []
        self.written: list[str] = []
        self.results: list[dict] = []
        self.closed = False
        self.more = asyncio.Event()
        self.frame_s = frame_s

    def poke(self):
        self.more.set()

    def close(self):
        self.closed = True
        self.more.set()

    async def next(self):
        while not self.closed:
            if not self.waiting:
                self.more.clear()
                await self.more.wait()
                continue
            await asyncio.sleep(self.frame_s)
            if self.closed:
                break
            self.written.append(self.waiting.pop(0))
            return "    " + self.written[-1]
        return None


def streamed(tmp_path, fake, added=(), timeout_s=20, frame_s=0.0):
    """One session started with "    first", and each (moment, text) of
    `added` given to its feed at that moment."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    feed = Feed(frame_s)

    async def go():
        async def arrive():
            for at, text in added:
                await moment(at, vault_dir)
                feed.waiting.append(text)
                feed.poke()
        arriving = asyncio.create_task(arrive())
        rr = await RunnerService(fake).run("    first", model="m", max_budget_usd=2, timeout_s=timeout_s,
                                          session_id="s1", cwd=str(vault_dir), feed=feed)
        await arriving
        return rr
    return run(go()), feed


def test_a_message_added_while_a_tool_runs_is_in_the_one_answer(tmp_path, monkeypatch):
    fake = standin(tmp_path, monkeypatch, steps=[1.0, 0.1])
    rr, feed = streamed(tmp_path, fake, [(("tool_use", 1, 0.1), "and tell me too")])
    assert rr.ok and len(rr.results) == 1 and feed.written == ["and tell me too"]
    assert rr.structured["answer"] == "one answer to 2: first | and tell me too"


def test_one_added_after_the_last_step_gets_a_further_turn_whose_result_is_the_sessions(tmp_path, monkeypatch):
    fake = standin(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5)
    rr, feed = streamed(tmp_path, fake, [(("tool_result", 1, 0.2), "and tell me too")])
    assert rr.ok and len(rr.results) == 2 and feed.written == ["and tell me too"]
    assert rr.structured["answer"] == "one answer to 2: first | and tell me too"
    # each result's figure is the session's so far, as the CLI's are
    assert rr.cost_usd == 0.02


def opening_blocks(tmp_path):
    """The text blocks of the session's opening message, as its transcript
    records them."""
    f = vault.transcripts_dir(tmp_path / "vault") / "s1.jsonl"
    entries = [json.loads(x) for x in f.read_text().splitlines()]
    return next(e["message"]["content"] for e in entries if e.get("type") == "user")


def test_a_message_waiting_before_the_first_turn_is_not_taken_with_the_prompt(tmp_path, monkeypatch):
    """Claude Code takes every message waiting when a turn starts into that
    turn as one: one written before the first turn began would be part of
    the prompt."""
    fake = standin(tmp_path, monkeypatch, startup_s=1.0, steps=[0.6, 0.1])
    rr, feed = streamed(tmp_path, fake, [(0, "and tell me too")])
    assert rr.ok and feed.written == ["and tell me too"]
    assert rr.structured["answer"] == "one answer to 2: first | and tell me too"
    assert [b["text"] for b in opening_blocks(tmp_path)] == ["    first"]


def answered_at(tmp_path) -> float:
    """When the session gave its first answer, by its transcript's clock."""
    f = vault.transcripts_dir(tmp_path / "vault") / "s1.jsonl"
    entries = [json.loads(x) for x in f.read_text().splitlines()]
    return datetime.fromisoformat(next(e["timestamp"] for e in entries
                                       if (e.get("attachment") or {}).get("type") == "structured_output")).timestamp()


def test_the_input_closes_at_the_first_result_while_a_message_is_framed(tmp_path, monkeypatch):
    fake = standin(tmp_path, monkeypatch, steps=[0.3], reply_s=0.5)
    rr, feed = streamed(tmp_path, fake, [(("tool_use", 1, 0), "framed too slowly")], frame_s=15)
    assert rr.ok and len(rr.results) == 1 and time.time() - answered_at(tmp_path) < 1
    assert feed.written == [] and feed.waiting == ["framed too slowly"]


def test_a_notice_of_a_background_commands_end_is_not_a_message(tmp_path, monkeypatch):
    """Claude Code hands one at a turn's next step, or runs a turn for it after
    a result, in a mode of its own; a turn for it may say nothing."""
    results = {}
    for when in ("mid", "after"):
        home = tmp_path / when
        home.mkdir()
        rr, _ = streamed(home, standin(home, monkeypatch, steps=[0.2], notify=when))
        results[when] = [r["structured_output"]["answer"] for r in rr.results]
    assert results == {"mid": ["one answer to 1: first"], "after": ["one answer to 1: first", ""]}


def test_nothing_is_added_once_the_session_has_answered(tmp_path, monkeypatch):
    fake = standin(tmp_path, monkeypatch, steps=[0.1])
    rr, feed = streamed(tmp_path, fake, [(("structured_output", 1, 0.3), "too late for this one")])
    assert rr.ok and len(rr.results) == 1 and feed.written == [] and feed.waiting == ["too late for this one"]


def test_a_message_written_but_never_run_is_not_in_the_answer(tmp_path, monkeypatch):
    """Were the CLI to end at the end of its input without running what it had
    queued, the session would end with the first turn's result alone."""
    fake = standin(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, on_eof="drop")
    rr, feed = streamed(tmp_path, fake, [(("tool_result", 1, 0.2), "and tell me too")])
    assert rr.ok and len(rr.results) == 1 and feed.written == ["and tell me too"]
    assert rr.structured["answer"] == "one answer to 1: first"


def test_a_failed_last_turn_fails_the_session(tmp_path, monkeypatch):
    fake = standin(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, fail="later")
    rr, feed = streamed(tmp_path, fake, [(("tool_result", 1, 0.2), "and tell me too")])
    assert not rr.ok and len(rr.results) == 2 and "error_during_execution" in rr.error


def test_an_exit_after_a_successful_result_fails_the_session_without_its_report(tmp_path, monkeypatch):
    """Ended from outside, or by a crash, in a later turn that gave no result:
    the answer already given stays among the results, and the error names
    the exit, never the report a success carries."""
    fake = standin(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, crash="later")
    rr, feed = streamed(tmp_path, fake, [(("tool_result", 1, 0.2), "and tell me too")])
    assert not rr.ok and rr.envelope is None and rr.exit_code == 1
    assert rr.error == "claude exited 1 after its last result"
    assert [r["structured_output"]["answer"] for r in rr.results] == ["one answer to 1: first"]


@pytest.mark.parametrize("script, error", [
    ("echo 'Login expired · Please run /login' >&2\nexit 1",
     "no result (exit 1); Claude Code said: Login expired · Please run /login"),
    ("exit 1", "no result (exit 1)"),
], ids=["with its words", "without"])
def test_what_claude_code_wrote_on_its_error_output_is_named_as_its_words(tmp_path, script, error):
    """After the harness's own words for the failure, as the `failed` alert,
    which sessions are shown, has every word of Claude Code's."""
    rr, _ = streamed(tmp_path, make_fake_claude(tmp_path, script))
    assert not rr.ok and rr.error == error


def test_long_lines_and_a_loud_stderr_do_not_stall_it(tmp_path, monkeypatch):
    fake = standin(tmp_path, monkeypatch, steps=[0.1], big=300_000, stderr=300_000)
    rr, _ = streamed(tmp_path, fake)
    assert rr.ok and len(rr.results) == 1


def test_a_streamed_session_past_its_time_is_killed(tmp_path, monkeypatch):
    fake = standin(tmp_path, monkeypatch, hang=True)
    start = time.monotonic()
    rr, feed = streamed(tmp_path, fake, [(0.2, "never handed")], timeout_s=2)
    assert rr.timed_out and rr.cost_usd == 2 and time.monotonic() - start < 15
    assert feed.written == ["never handed"] and rr.results == []


def test_a_streamed_session_reads_its_input_and_output_as_streams(tmp_path):
    args = tmp_path / "args"
    fake = make_fake_claude(
        tmp_path,
        f'for a in "$@"; do printf "%s\\n" "$a"; done > {args}\n'
        'echo \'{"type":"result","subtype":"success","is_error":false,"result":"ok","total_cost_usd":0.1,'
        '"session_id":"s1"}\'\ncat > /dev/null',
    )
    rr = run(RunnerService(fake).run("x", model="m", max_budget_usd=1, timeout_s=10, feed=Feed()))
    argv = args.read_text().splitlines()
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert rr.ok and len(rr.results) == 1


def test_what_a_streamed_session_leaves_running_is_ended(tmp_path, monkeypatch):
    fake = standin(tmp_path, monkeypatch, steps=[0.1], leave=True)
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    rr = run(RunnerService(fake).run("    first", model="m", max_budget_usd=2, timeout_s=20, session_id="s1",
                                     cwd=str(vault_dir), feed=Feed(), mark="MEM_SESSION=s1"))
    left = int((tmp_path / "left.pid").read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(left, 0)
    assert rr.ok


def test_an_output_it_cannot_read_fails_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr("wanda.runner.STREAM_LINE_LIMIT", 64 * 1024)
    fake = standin(tmp_path, monkeypatch, steps=[0.1], big=300_000)
    rr, _ = streamed(tmp_path, fake)
    assert not rr.ok and rr.error.startswith("could not read the session's output")


@pytest.mark.parametrize("shape, said", [
    ({"event": {"type": "a_new_kind_of_event"}}, "an event of a type this runner does not know: 'a_new_kind_of_event'"),
    ({"result_without": "is_error"}, "a result without is_error"),
    ({"result_without": "result"}, "a result without result"),
    ({"event": {"type": "rate_limit_event", "rate_limit_info": {}}}, None),
])
def test_a_stream_shape_it_does_not_know_fails_the_session(tmp_path, monkeypatch, shape, said):
    """The harness reads Claude Code's output in the shapes of the version
    the product pins: an event or a result in another fails the session,
    which the household is told, rather than being read as if nothing had
    changed. An event of a type that version writes is read past."""
    fake = standin(tmp_path, monkeypatch, steps=[0.3, 0.1], **shape)
    rr, _ = streamed(tmp_path, fake)
    if said is None:
        assert rr.ok and len(rr.results) == 1
    else:
        assert not rr.ok and rr.error == f"could not read the session's output: {said}"


@pytest.mark.parametrize("error, said, why", [
    ("rate_limit", "You've hit your limit · resets 5pm (America/Los_Angeles)", "usage limit"),
    ("authentication_failed", "OAuth token revoked · Please run /login", "authentication"),
    # where the assistant event says what failed, its words are not read
    ("server_error", "API Error: 500 · Please run /login", None),
    (None, "OAuth token revoked · Please run /login", "authentication"),
], ids=["a usage limit", "a token refused", "another error", "no error given"])
def test_a_streamed_session_claude_code_refused_says_why(tmp_path, monkeypatch, error, said, why):
    fake = standin(tmp_path, monkeypatch, refuse={"turn": "first", "error": error, "said": said})
    rr, _ = streamed(tmp_path, fake)
    assert not rr.ok and rr.api_error == error and rr.error == said and refused(rr) == why


@pytest.mark.parametrize("said, why", [
    ("You've hit your limit · resets 5pm", "usage limit"),
    ("Usage limit reached ∙ resets at 5pm", "usage limit"),
    ("Claude AI usage limit reached|1759700000", "usage limit"),
    ("Login expired · Please run /login", "authentication"),
    ("Not logged in · Please run /login", "authentication"),
    ("OAuth token revoked · Please run /login", "authentication"),
    ("API Error: 401 Invalid API key · Please run /login", "authentication"),
    ('API Error: 401 {"type":"error","error":{"type":"authentication_error"}}', "authentication"),
    ("API Error: 4010 request req_240157 failed", None),
    ("Invalid API key · Fix external API key", "authentication"),
    ("Context limit reached · /compact or /clear to continue", None),
    ("API Error: 500 request req_240157 failed", None),
    ("error_during_execution", None),
])
def test_what_claude_code_says_when_it_will_not_run_is_read_where_no_error_is_given(said, why):
    """A session run with --output-format json, as the clock's are, prints no
    assistant event: its error's words are read, in any case."""
    assert refused(RunResult(ok=False, error=said)) == why
