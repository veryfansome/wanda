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
from pathlib import Path

import pytest

from wanda.runner import RunnerService


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
