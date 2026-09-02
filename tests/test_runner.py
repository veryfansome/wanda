import asyncio
import contextlib
import json
import stat
import time

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
