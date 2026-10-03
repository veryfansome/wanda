"""Additions, what a conversation's session takes in while it works, and
what it puts back for the conversation's next turn, by the session's
transcript (tests/claude_standin.py replays the pinned CLI's shapes)."""

import asyncio
import json
import stat
import sys
import time
from pathlib import Path

import pytest

from wanda import main, vault
from wanda.main import Additions
from wanda.runner import RunnerService

STANDIN = Path(__file__).with_name("claude_standin.py")


def run(coro):
    return asyncio.run(coro)


def standin(tmp_path, monkeypatch, **behaviour) -> str:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("STANDIN", json.dumps(behaviour))
    path = tmp_path / "fake-claude"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STANDIN}" "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


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


def streamed(tmp_path, fake, added=(), timeout_s=20, frame_s=0.0):
    """One session started with "    first", and each (moment, text) of
    `added` put on its conversation's waiting list at that moment, framed in
    `frame_s`; then what it was not handed given back."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    waiting = []

    async def frame(p):
        await asyncio.sleep(frame_s)
        return "    " + p["text"]

    async def go():
        more = Additions(waiting, frame)

        async def arrive():
            for at, text in added:
                await moment(at, vault_dir)
                waiting.append({"ts": f"{time.time():.6f}", "channel": "D1", "text": text})
                more.poke()
        arriving = asyncio.create_task(arrive())
        await RunnerService(fake).run("    first", model="m", max_budget_usd=2, timeout_s=timeout_s,
                                      session_id="s1", cwd=str(vault_dir), feed=more)
        await arriving
        return more.give_back(vault.handed(vault_dir, "s1"))
    handed = run(go())
    return handed, [m["text"] for m in waiting]


@pytest.mark.parametrize("behaviour, added, timeout_s, frame_s, handed, back", [
    # handed at the running turn's next step
    ({"steps": [1.0, 0.1]}, [(("tool_use", 1, 0.1), "and tell me too")], 20, 0, 1, []),
    # written after the last step, run as a further turn
    ({"steps": [0.2], "reply_s": 1.5}, [(("tool_result", 1, 0.2), "and tell me too")], 20, 0, 1, []),
    # waiting before the first turn began, handed once it had
    ({"startup_s": 1.0, "steps": [0.6, 0.1]}, [(0, "and tell me too")], 20, 0, 1, []),
    # written, and the CLI ending at the end of its input without running it
    ({"steps": [0.2], "reply_s": 1.5, "on_eof": "drop"}, [(("tool_result", 1, 0.2), "and tell me too")], 20, 0, 0,
     ["and tell me too"]),
    # written to a session killed at its timeout
    ({"hang": True}, [(0.2, "never handed")], 2, 0, 0, ["never handed"]),
    # being framed when the session answered
    ({"steps": [0.3], "reply_s": 0.5}, [(("tool_use", 1, 0), "framed too slowly")], 20, 15, 0, ["framed too slowly"]),
    # arriving once the session has answered
    ({"steps": [0.1]}, [(("structured_output", 1, 0.3), "too late for this one")], 20, 0, 0,
     ["too late for this one"]),
], ids=["at a step", "a further turn", "before the first turn", "never run", "timed out", "being framed",
        "after the answer"])
def test_what_the_session_was_not_handed_goes_back(tmp_path, monkeypatch, behaviour, added, timeout_s, frame_s,
                                                    handed, back):
    fake = standin(tmp_path, monkeypatch, **behaviour)
    assert streamed(tmp_path, fake, added, timeout_s, frame_s) == (handed, back)


def test_additions_stop_at_the_limit_and_the_time(monkeypatch):
    async def frame(p):
        return p["text"]

    async def go(limit, for_s, n):
        monkeypatch.setattr(main, "FOLD_LIMIT", limit)
        monkeypatch.setattr(main, "FOLD_FOR_S", for_s)
        waiting = [{"ts": f"{i}", "channel": "D1", "text": f"m{i}"} for i in range(n)]
        more = Additions(waiting, frame)
        got = []
        while (t := await more.next()) is not None:
            got.append(t)
        return got, [m["text"] for m in waiting]
    assert run(go(2, 60, 3)) == (["m0", "m1"], ["m2"])
    start = time.monotonic()
    assert run(go(3, 0.3, 0)) == ([], []) and time.monotonic() - start < 2


@pytest.mark.parametrize("not_framed", ["raises", "not this session's"])
def test_a_message_that_is_not_framed_waits_for_the_next_turn(not_framed):
    async def frame(p):
        if not_framed == "raises":
            raise RuntimeError("users.info failed")
        return None

    async def go():
        waiting = [{"ts": "1", "channel": "D1", "text": "m"}]
        more = Additions(waiting, frame)
        return await more.next(), waiting, more.closed
    got, waiting, closed = run(go())
    assert got is None and closed and [m["text"] for m in waiting] == ["m"]
