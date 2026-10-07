"""What was sent while she could not hear Slack, stopped or with her
connection down, read back from Slack and taken up: at a start, and when a
new connection opens while she runs."""

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError

from wanda import main, vault
from wanda.actions.slack import CallFailed
from wanda.events import Event
from wanda.main import Gate, Processor
from wanda.store import Settled, Store, utcnow
from wanda.watchers.slack_watcher import SlackWatcher

from test_processor import (  # noqa: F401
    AT, LATE_TURN_S, ConversationSlack, RecordingRunner, _scrub_env, a_start, answer, dm, fake_time, keep, kept,
    memory_processor, reactions_end, start_config,
)

# 19:40 in Los Angeles, three hours after AT, where a test of a running
# daemon stands; her last pong was an hour before
NOW = AT + 3 * 3600


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def said(ts: float, text: str, user: str = "U1", **kw) -> dict:
    """A message as conversations.history or conversations.replies gives it."""
    return {"type": "message", "user": user, "ts": f"{ts:.1f}", "text": text, **kw}


def heard(session: str = "s2", refresh: bool = False) -> Event:
    return Event(source="slack", dedupe_key=f"heard:{session}",
                 payload={"kind": "heard", "session": session, "refresh": refresh})


def slack_error(error):
    return SlackApiError("The request to the Slack API failed.", {"ok": False, "error": error})


class Conn:
    """The SDK's open connection, as `last_heard` reads it."""

    def __init__(self, session_id="s1", pong=None):
        self.session_id, self.last_ping_pong_time, self.open = session_id, pong, True

    def is_active(self):
        return self.open


class Slack(ConversationSlack):
    """ConversationSlack, whose read back calls can be held: `hold`, by a
    call's arguments, an event each such call waits for; `on_read`, called
    at each call with its arguments; and each add of her reaction answered
    by `react_errors`, by (channel, ts), an error to raise, a delay, or a
    delay and then an error, as (delay, error)."""

    def __init__(self, **kw):
        super().__init__(history=[], **kw)
        self.hold: dict[tuple, asyncio.Event] = {}
        self.on_read = None
        self.react_errors: dict[tuple[str, str], object] = {}
        # when each add was called, by time.monotonic()
        self.react_times: list[tuple[tuple[str, str], float]] = []
        # each read call as it is made, before any hold
        self.reaching: list[tuple] = []
        # a conversation's history given as one page just as it stands,
        # unsorted and unfiltered
        self.pages: dict[str, list] = {}

    async def _held(self, *call):
        self.reaching.append(call)
        if self.on_read is not None:
            self.on_read(*call)
        if (ev := self.hold.get(call[:2]) or self.hold.get(call[:3])) is not None:
            await ev.wait()

    async def conversations(self):
        await self._held("conversations")
        return await super().conversations()

    async def read_history(self, channel, oldest):
        await self._held("history", channel)
        if channel in self.pages:
            self._read("history", channel, oldest=oldest)
            yield self.pages[channel], False
            return
        async for page in super().read_history(channel, oldest):
            yield page

    async def read_thread(self, channel, ts, oldest):
        await self._held("thread", channel, ts)
        return await super().read_thread(channel, ts, oldest)

    async def react(self, channel, ts):
        self.react_times.append(((channel, ts), time.monotonic()))
        what = self.react_errors.get((channel, ts))
        if isinstance(what, tuple):
            delay, what = what
            await asyncio.sleep(delay)
        if isinstance(what, (int, float)):
            await asyncio.sleep(what)
        elif isinstance(what, BaseException):
            self.reacted.append((channel, ts))
            raise what
        await super().react(channel, ts)


def im(channel="D1"):
    return {"id": channel, "is_im": True}


def mpim(channel="G1"):
    return {"id": channel, "is_mpim": True}


def private(channel="P1"):
    return {"id": channel, "is_private": True}


def public(channel="C1"):
    return {"id": channel}


def running(tmp_path, monkeypatch, fake_time, slack=None, runner=None, sessions=1, late=False, late_adds=False):
    """A daemon running at NOW, her connection `s1` heard an hour ago: the
    processor, its store, its Slack, and its runner. `late` frames a turn
    late as the daemon does, and makes her reaction late as it does;
    `late_adds` only the second."""
    slack = slack or Slack()
    runner = runner or RecordingRunner()
    runner.agent_sem = asyncio.Semaphore(sessions)
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    fake_time.at = datetime.fromtimestamp(NOW, timezone.utc)
    store.set_meta("heard_from", iso(NOW - 3600))
    w = SlackWatcher(p.cfg, store, None, p.slack_queue)
    w.bot_user_id, w.bot_id = "UBOT", "BME"
    conn = Conn("s1", NOW - 3600 + 600)
    w.client = SimpleNamespace(current_session=conn, is_connected=lambda: w.client.current_session.is_active())
    p.watcher, p._heard_session = w, "s1"
    monkeypatch.setattr(main, "READ_WAIT_S", 0.5)
    monkeypatch.setattr(main, "READ_EVERY_S", 0.01)
    if late:
        monkeypatch.setattr(main, "LATE_TURN_S", LATE_TURN_S)
    if late or late_adds:
        monkeypatch.setattr(main, "LATE_ADD_S", 60)
    return p, store, slack, runner


def live(p, store, ev):
    """A line as the watcher keeps it and puts it on the queue."""
    keep(store, ev)
    p.slack_queue.put_nowait(ev)


async def until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.005)):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("never came")


async def settled(p):
    """Until nothing of hers is running: no handler, no read, no reaction."""
    for _ in range(2000):
        if not (p._bg or p._reading is not None or p._reacting):
            await asyncio.sleep(0.02)
            if not (p._bg or p._reading is not None or p._reacting):
                return
        await asyncio.sleep(0.005)
    raise AssertionError("never settled")


def with_loop(p, go):
    """Runs `go` with slack_loop running, until all it set going settles."""
    async def run():
        loop = asyncio.create_task(p.slack_loop())
        try:
            out = await go()
            await settled(p)
            return out
        finally:
            loop.cancel()
    return asyncio.run(run())


def frames(runner):
    return [prompt for prompt, _ in runner.calls]


def at_la(ts: float, now: float = NOW) -> str:
    from zoneinfo import ZoneInfo
    return vault.stamp(ts, datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles")))


# --- the gate ----------------------------------------------------------------

def test_a_later_close_of_the_gate_moves_its_opening_and_the_lines_waiting_wait_for_it():
    """One timer: a close while a line waits cancels the first close's timer
    and arms its own; the line goes on at the second's."""
    async def go():
        gate, loop = Gate(), asyncio.get_running_loop()
        t0 = loop.time()
        gate.close(0.2)
        waiter = asyncio.create_task(gate.wait("D1"))
        await asyncio.sleep(0.1)
        gate.close(0.2)
        await asyncio.wait_for(waiter, 1)
        return loop.time() - t0
    took = asyncio.run(go())
    assert 0.29 <= took < 0.4


def test_the_gate_opens_one_conversation_as_its_read_finishes_it_and_all_at_once():
    async def go():
        gate = Gate()
        gate.close(10)
        d1, c1 = asyncio.create_task(gate.wait("D1")), asyncio.create_task(gate.wait("C1"))
        await asyncio.sleep(0.01)
        gate.open("D1")
        await asyncio.sleep(0.01)
        first = (d1.done(), c1.done())
        gate.open_all()
        await asyncio.sleep(0.01)
        return first, c1.done(), gate.closed
    assert asyncio.run(go()) == ((True, False), True, False)


def test_lines_waiting_in_one_conversation_go_on_in_the_order_they_began_to_wait_whatever_opens_before_it():
    """W1 waits on D1; D2 opens in the loop step W2's first step is queued
    in, so that W2 waits on D1 before W1 could wait again."""
    async def go():
        gate, order = Gate(), []
        gate.close(10)

        async def waiter(name):
            await gate.wait("D1")
            order.append(name)
        w1 = asyncio.create_task(waiter("W1"))
        await asyncio.sleep(0)
        assert not w1.done()

        def same_step():
            asyncio.ensure_future(waiter("W2"))
            gate.open("D2")
        asyncio.get_running_loop().call_soon(same_step)
        await asyncio.sleep(0.01)
        gate.open("D1")
        await asyncio.sleep(0.01)
        return order
    assert asyncio.run(go()) == ["W1", "W2"]


def test_a_line_woken_by_its_conversations_opening_goes_on_though_the_gate_closes_again_in_that_step():
    async def go():
        gate = Gate()
        gate.close(10)
        waiter = asyncio.create_task(gate.wait("D1"))
        await asyncio.sleep(0.01)

        def same_step():
            gate.open("D1")
            gate.close(10)
        asyncio.get_running_loop().call_soon(same_step)
        await asyncio.sleep(0.01)
        return waiter.done(), gate.is_open("D1")
    assert asyncio.run(go()) == (True, False)


def test_a_close_in_the_step_of_an_opening_lets_no_later_line_past_one_that_waited():
    """L1 waits on D1. The gate opens for all, as a read's end does; in that
    loop step L2's first step finds it open and yields, as
    _run_memory_reply's does, and a new connection's `heard` then closes it
    again."""
    async def go():
        gate, order, loop = Gate(), [], asyncio.get_running_loop()
        gate.close(10)

        async def line(name):
            if not gate.is_open("D1"):
                await gate.wait("D1")
            else:
                await asyncio.sleep(0)
            order.append(name)
        asyncio.ensure_future(line("L1"))
        await asyncio.sleep(0)
        loop.call_soon(lambda: loop.call_soon(gate.open_all))
        loop.call_soon(lambda: asyncio.ensure_future(line("L2")))
        loop.call_soon(lambda: loop.call_soon(gate.close, 10))
        await asyncio.sleep(0.01)
        gate.open("D1")
        await asyncio.sleep(0.01)
        return order
    assert asyncio.run(go()) == ["L1", "L2"]


@pytest.mark.parametrize("opens", ["D1", "all"])
def test_a_line_after_a_later_close_waits_for_its_conversation_to_open_again(opens):
    """What a conversation's lines waited on goes as it opens, alone or with
    every other, so that a new line there after a later close waits for the
    next opening."""
    async def go():
        gate = Gate()
        gate.close(10)
        first = asyncio.create_task(gate.wait("D1"))
        await asyncio.sleep(0.01)
        if opens == "all":
            gate.open_all()
        else:
            gate.open("D1")
        await asyncio.sleep(0.01)
        gate.close(10)
        second = asyncio.create_task(gate.wait("D1"))
        await asyncio.sleep(0.01)
        held = not second.done()
        gate.open("D1")
        await asyncio.wait_for(second, 1)
        return first.done(), held
    assert asyncio.run(go()) == (True, True)


# --- while she runs: a new connection's read --------------------------------

def test_a_new_connections_read_hands_on_a_missed_line_and_a_new_one_waits_for_it_as_one_turn(
        tmp_path, monkeypatch, fake_time):
    """After a lost connection: the missed line and the new one are one
    turn, framed late by the missed line, with nothing said of her being
    stopped, since she was not; the new line's reaction is on before the
    read ends."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late=True)
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(NOW - 1800, "can you check whether the plumber replied?")]
    reacted_while_reading = []
    gate = slack.hold[("history", "D1")] = asyncio.Event()

    async def go():
        p.slack_queue.put_nowait(heard())
        live(p, store, dm(f"{NOW - 5:.1f}", "are you back?"))
        await until(lambda: ("D1", f"{NOW - 5:.1f}") in slack.reacted)
        reacted_while_reading.append(p._reading is not None)
        gate.set()
    with_loop(p, go)
    [prompt] = frames(runner)
    assert (f"What fan says below was sent at {at_la(NOW - 1800)} and reaches me only now.\n\n" in prompt
            and "I was not running" not in prompt)
    assert f"    {at_la(NOW - 1800)} fan: can you check whether the plumber replied?\n" in prompt
    assert "fan now says:\n\n    are you back?\n\n" in prompt
    assert reacted_while_reading == [True] and kept(store) == []


def test_a_line_whose_handler_is_made_in_the_step_its_conversation_is_handed_on_joins_the_turn(
        tmp_path, monkeypatch, fake_time):
    """slack_loop makes the live line's handler, and the read hands on the
    missed lines, in one step of the loop: the live line yields once past
    the gate, and the missed lines, which do not, take the turn it joins."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late_adds=True)
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(NOW - 1800, "one"), said(NOW - 1700, "two")]
    page = slack.hold[("history", "D1")] = asyncio.Event()

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: ("history", "D1") in slack.reaching)
        line = dm(f"{NOW - 5:.1f}", "three")
        keep(store, line)

        def one_step():
            p.slack_queue.put_nowait(line)
            page.set()
        asyncio.get_running_loop().call_soon(one_step)
    with_loop(p, go)
    [prompt] = frames(runner)
    assert "fan: one\n" in prompt and "fan: two\n" in prompt and "fan now says:\n\n    three\n\n" in prompt


def test_a_conversation_with_nothing_missed_opens_as_it_is_read_before_the_read_ends(
        tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    # the gate's timer out of reach, so that only G1's read can open it
    monkeypatch.setattr(main, "READ_WAIT_S", 30)
    slack.listed = [mpim("G1"), public("C1")]
    channel = slack.hold[("history", "C1")] = asyncio.Event()

    async def go():
        p.slack_queue.put_nowait(heard())
        live(p, store, dm(f"{NOW - 5:.1f}", "dinner at 7?", channel_type="mpim", channel="G1"))
        await until(lambda: runner.calls, timeout=2)
        assert p._reading is not None, "G1's session began while C1 was still being read"
        channel.set()
    with_loop(p, go)
    assert len(runner.calls) == 1


def test_a_missed_reply_and_a_live_one_in_a_thread_of_hers_are_one_turn(tmp_path, monkeypatch, fake_time):
    """A thread a session of hers ran in within 30 days is read whatever its
    first message's age, and holds a live reply there until it is read."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late_adds=True)
    task = store.create_task(None, "C1", f"{AT - 20 * 86400:.1f}", kind="mention")
    store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(NOW - 3 * 86400), exit_code=0,
                     cost_usd=0.0, status="ok")
    thread = f"{AT - 20 * 86400:.1f}"
    slack.listed = [public("C1")]
    slack.replies_of[("C1", thread)] = [said(AT - 20 * 86400, "<@UBOT> the boiler?", thread_ts=thread),
                                        said(NOW - 1800, "it is making the noise again", thread_ts=thread)]
    slack.hold[("thread", "C1", thread)] = asyncio.Event()

    async def go():
        p.slack_queue.put_nowait(heard())
        reply = dm(f"{NOW - 5:.1f}", "any idea?", channel_type="channel", channel="C1", thread=thread)
        reply.payload["kind"] = "task"
        live(p, store, reply)
        await until(lambda: ("C1", f"{NOW - 5:.1f}") in slack.reacted)
        await asyncio.sleep(0.05)
        assert runner.calls == []
        slack.hold[("thread", "C1", thread)].set()
    with_loop(p, go)
    [prompt] = frames(runner)
    assert "fan: it is making the noise again\n" in prompt and "fan now says:\n\n    any idea?\n\n" in prompt


def test_what_was_missed_is_answered_in_the_order_it_is_read(tmp_path, monkeypatch, fake_time):
    """A DM handed on holds the session's place before a fresh line in a
    channel read after it, which waits for its channel's read."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1"), public("C1")]
    slack.histories["D1"] = [said(NOW - 1800, "is the plumber coming?")]
    channel = slack.hold[("history", "C1")] = asyncio.Event()

    async def go():
        p.slack_queue.put_nowait(heard())
        live(p, store, dm(f"{NOW - 5:.1f}", "<@UBOT> note this", channel_type="channel", channel="C1"))
        await until(lambda: ("history", "C1") in slack.reaching)
        await asyncio.sleep(0.02)
        channel.set()
    with_loop(p, go)
    assert ["is the plumber coming?" in f for f in frames(runner)] == [True, False]
    assert "note this" in frames(runner)[1]


def never_reads(monkeypatch):
    """A read that never ends and whose end, were it to come, does nothing:
    nothing opens the gate but its timer."""
    async def read_back(self):
        await asyncio.sleep(100)
    monkeypatch.setattr(Processor, "read_back", read_back)
    monkeypatch.setattr(Processor, "_read_done", lambda self, t: None)


def test_with_no_opener_the_gate_opens_at_its_timer_and_lines_go_on_in_the_order_they_came(
        tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    never_reads(monkeypatch)
    began = []
    run = runner.run

    async def timed(prompt, **kw):
        began.append(time.monotonic())
        return await run(prompt, **kw)
    runner.run = timed

    async def go():
        t0 = time.monotonic()
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._reading is not None)
        live(p, store, dm(f"{NOW - 5:.1f}", "is the boiler fixed?", channel="D1"))
        live(p, store, dm(f"{NOW - 4:.1f}", "dinner at 7?", channel="D2", user="U2"))
        await until(lambda: len(runner.calls) == 2)
        p._reading.cancel()
        p._reading = None
        return t0
    t0 = with_loop(p, go)
    assert ["is the boiler fixed?" in f for f in frames(runner)] == [True, False]
    assert "dinner at 7?" in frames(runner)[1]
    assert 0.5 <= began[0] - t0 < 0.7


def two_lines_at_an_opening(p, store, opens):
    """fan's first line waits at D1's closed gate; his second is put on the
    queue so that slack_loop makes its handler in the loop step that opens
    the gate for `opens`, just before the opening. When that is another
    conversation, D1 opens a moment later. When it is every conversation,
    as at a read's end, the opening comes a step later, just before the
    second line's first step, and a new connection's `heard`, queued behind
    the second line, closes the gate again in that step."""
    p.gate.close(10)
    live(p, store, dm(f"{NOW - 20:.1f}", "L1, sent first"))
    second = dm(f"{NOW - 10:.1f}", "L2, sent second")
    keep(store, second)
    loop = asyncio.get_running_loop()

    def put():
        if opens == "all":
            loop.call_soon(lambda: loop.call_soon(p.gate.open_all))
            p.slack_queue.put_nowait(second)
            loop.call_soon(lambda: p.slack_queue.put_nowait(heard("s2")))
        else:
            p.slack_queue.put_nowait(second)
            loop.call_soon(p.gate.open, opens)

    async def go():
        await until(lambda: ("D1", f"{NOW - 20:.1f}") in p._eyed)
        await asyncio.sleep(0.02)
        loop.call_soon(put)
        if opens == "D2":
            await asyncio.sleep(0.05)
            p.gate.open("D1")
    return go()


@pytest.mark.parametrize("opens", ["D1", "D2", "all"])
def test_two_lines_past_the_gate_reach_the_working_session_in_the_order_they_came(tmp_path, monkeypatch, fake_time,
                                                                                  opens):
    runner = Taking(n=2)
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, runner=runner)

    async def go():
        live(p, store, dm(f"{NOW - 30:.1f}", "is the plumber coming?"))
        await asyncio.wait_for(runner.working.wait(), 3)
        await two_lines_at_an_opening(p, store, opens)
    with_loop(p, go)
    assert ["L1" in text for text in runner.added] == [True, False] and "L2" in runner.added[1]


@pytest.mark.parametrize("opens", ["D1", "D2", "all"])
def test_two_lines_past_the_gate_are_answered_in_the_order_they_came(tmp_path, monkeypatch, fake_time, opens):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)

    async def go():
        await two_lines_at_an_opening(p, store, opens)
    with_loop(p, go)
    assert [("L1" in f, "L2" in f) for f in frames(runner)] == [(True, False), (False, True)]


@pytest.mark.parametrize("a_pass", [False, True])
def test_a_line_whose_message_is_deleted_while_it_waits_at_the_gate_owes_nothing(tmp_path, monkeypatch, fake_time,
                                                                                 a_pass):
    """`a_pass`: with a pass of the processor while the line waits, which
    keeps the deletion, made well within GONE_KEPT_S."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    never_reads(monkeypatch)

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._reading is not None)
        live(p, store, dm(f"{NOW - 5:.1f}", "sent to the wrong person"))
        await until(lambda: ("D1", f"{NOW - 5:.1f}") in slack.reacted)
        p.slack_queue.put_nowait(Event(source="slack", dedupe_key="x", payload={
            "kind": "deleted", "channel": "D1", "ts": f"{NOW - 5:.1f}"}))
        await until(lambda: ("D1", f"{NOW - 5:.1f}") in p._gone)
        assert p.gate.closed
        if a_pass:
            await p.drain_mail()
        await until(lambda: not p.gate.closed)
        p._reading.cancel()
        p._reading = None
    with_loop(p, go)
    assert runner.calls == [] and kept(store) == [] and slack.replies == []


def test_a_pass_drops_a_deletion_older_than_its_bound_and_keeps_a_newer(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    monkeypatch.setattr(main, "GONE_KEPT_S", 0.2)

    async def go():
        p._withdraw("C1", "1.1")
        await asyncio.sleep(0.3)
        p._withdraw("C1", "2.2")
        await p.drain_mail()
        return set(p._gone)
    assert with_loop(p, go) == {("C1", "2.2")}


def test_a_deletion_the_store_cannot_record_posts_nothing(tmp_path, monkeypatch, fake_time, caplog):
    """Any message deleted where she is, with the store refusing writes: a
    deletion owes nothing, so no note goes to the conversation."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)

    def full(self, keys):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(Store, "forget", full)

    async def go():
        p.slack_queue.put_nowait(Event(source="slack", dedupe_key="C1:5.5:deleted",
                                       payload={"kind": "deleted", "channel": "C1", "ts": "5.5"}))
    with caplog.at_level(logging.ERROR, logger="wanda"):
        with_loop(p, go)
    assert slack.replies == [] and "could not forget 5.5 in C1, deleted" in caplog.text


def test_a_message_deleted_before_its_conversation_is_handed_on_is_not(tmp_path, monkeypatch, fake_time):
    """Past the gate's wait, the watcher records a deletion as seen and the
    message is withdrawn: kept by the read and not yet handed on, it is
    not."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(NOW - 1800, "sent to the wrong person", latest_reply=f"{NOW - 1700:.1f}")]
    slack.replies_of[("D1", f"{NOW - 1800:.1f}")] = []
    thread = slack.hold[("thread", "D1", f"{NOW - 1800:.1f}")] = asyncio.Event()

    async def go():
        p.watcher.loop = asyncio.get_running_loop()
        p.slack_queue.put_nowait(heard())
        await until(lambda: ("D1", f"{NOW - 1800:.1f}") in p._unhanded)
        await asyncio.sleep(0.6)  # the gate's wait has run out
        p.watcher._handle(SimpleNamespace(send_socket_mode_response=lambda r: None), SimpleNamespace(
            type="events_api", envelope_id="e", payload={"event": {
                "type": "message", "subtype": "message_deleted", "channel": "D1", "deleted_ts": f"{NOW - 1800:.1f}"}}))
        await until(lambda: kept(store) == [])
        thread.set()
    with_loop(p, go)
    assert runner.calls == []


def test_a_line_kept_again_after_its_deletion_was_handled_forgets_its_row(tmp_path, monkeypatch, fake_time):
    """Its deletion handled before the line was kept, as when the record of
    the deletion failed: the line runs nothing, and its row goes."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)

    async def go():
        p._withdraw("D1", f"{NOW - 5:.1f}")
        live(p, store, dm(f"{NOW - 5:.1f}", "sent to the wrong person"))
    with_loop(p, go)
    assert runner.calls == [] and kept(store) == []


def test_a_message_a_read_kept_whose_row_has_gone_is_not_handed_on(tmp_path, monkeypatch, fake_time):
    """Whatever let its row go, not only a deletion she was told of."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    ev = dm(f"{NOW - 1800:.1f}", "is the plumber coming?")
    key = keep(store, ev)
    p._unhanded[key] = ev.payload
    p._marked.add(key)
    store.forget([key])

    async def go():
        p._hand_on()
        await settled(p)
    asyncio.run(go())
    assert runner.calls == [] and p._unhanded == {}


class Working(RecordingRunner):
    """A session that works until `done` is set, taking in what is added to
    its conversation meanwhile."""

    def __init__(self, *reports):
        super().__init__(*reports)
        self.done = asyncio.Event()
        self.added: list[str] = []
        self.feeds = []

    async def run(self, prompt, **kw):
        feed = kw.get("feed")
        self.feeds.append(feed)
        if feed is not None and not self.done.is_set():
            take = asyncio.create_task(feed.next())
            await self.done.wait()
            feed.close()
            if (text := await take) is not None:
                self.added.append(text)
        return await super().run(prompt, **kw)


def test_a_refresh_reads_with_the_gate_open_and_a_follow_up_reaches_the_working_session_at_once(
        tmp_path, monkeypatch, fake_time):
    runner = Working(answer("Noted both."))
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, runner=runner)
    slack.listed = [im("D1")]
    reading = slack.hold[("conversations",)] = asyncio.Event()

    async def go():
        live(p, store, dm(f"{NOW - 30:.1f}", "is the plumber coming?"))
        await until(lambda: runner.feeds)
        p.slack_queue.put_nowait(heard(refresh=True))
        await until(lambda: p._reading is not None)
        assert not p.gate.closed
        live(p, store, dm(f"{NOW - 5:.1f}", "and when?"))
        await until(lambda: any(m["ts"] == f"{NOW - 5:.1f}" for m, _ in runner.feeds[0].taken))
        assert p._reading is not None
        runner.done.set()
        reading.set()
    with_loop(p, go)
    assert "is the plumber coming?" in runner.calls[0][0] and "and when?" in runner.added[0]


def test_a_new_connection_while_a_read_runs_leaves_a_read_needed_for_the_next_pass(tmp_path, monkeypatch, fake_time):
    """Its gap may begin after the first read read a conversation: the first
    read's success does not clear it, and the next pass reads with the gate
    open, moving nothing while it runs."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1")]
    first = slack.hold[("conversations",)] = asyncio.Event()

    async def go():
        p.slack_queue.put_nowait(heard("s2"))
        await until(lambda: p._reading is not None)
        p.slack_queue.put_nowait(heard("s3"))
        await until(lambda: p._read_needed)
        assert p._reading is not None and p.gate.closed
        p.watcher.client.current_session = Conn("s3", NOW - 60)
        first.set()
        await until(lambda: p._reading is None)
        assert p._read_needed and p._read_failing is None
        slack.hold[("conversations",)] = again = asyncio.Event()
        await p.drain_mail()
        assert p._reading is not None and not p.gate.closed
        assert store.get_meta("heard_from") == iso(NOW - 3600), "a read running holds it"
        again.set()
        await until(lambda: p._reading is None)
        await p.drain_mail()
    with_loop(p, go)
    assert store.get_meta("heard_from") == iso(NOW - 60 - 600)
    assert [r for r in slack.reads if r == ("conversations",)] == [("conversations",)] * 2


def test_a_new_connection_during_a_retry_that_succeeds_still_needs_a_read(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1")]
    slack.read_errors[("conversations",)] = CallFailed("users_conversations: network unreachable")

    async def go():
        p.slack_queue.put_nowait(heard("s2"))
        await until(lambda: p._read_failing is not None)
        del slack.read_errors[("conversations",)]
        p._read_failing = p._read_failing._replace(next_try=0)
        slack.hold[("conversations",)] = retry = asyncio.Event()
        await p.drain_mail()
        assert p._reading is not None
        p.slack_queue.put_nowait(heard("s3"))
        await until(lambda: p._read_needed)
        retry.set()
        await until(lambda: p._reading is None)
        assert p._read_needed and p._read_failing is None
        del slack.hold[("conversations",)]
        await p.drain_mail()
        await until(lambda: p._reading is None)
    with_loop(p, go)
    assert [r for r in slack.reads if r == ("conversations",)] == [("conversations",)] * 3


def test_a_pass_in_the_step_a_read_raises_moves_nothing(tmp_path, monkeypatch, fake_time):
    """The read counts as running until its end has been taken: a pass that
    resumes in the step its read raised in, before that, moves nothing."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    seen = []

    async def conversations():
        def a_pass():
            seen.append((p._reading.done(), p._reading is not None))
            p._read_if_needed()
            p._move_heard()
        asyncio.get_running_loop().call_soon(a_pass)
        raise CallFailed("users_conversations: network unreachable")
    slack.conversations = conversations

    async def go():
        p.slack_queue.put_nowait(heard("s1"))
        await until(lambda: seen and p._reading is None)
    with_loop(p, go)
    assert seen == [(True, True)] and store.get_meta("heard_from") == iso(NOW - 3600)
    assert p._read_needed and p._read_failing is not None


def test_a_read_the_store_fails_is_tried_again_after_one_two_four_and_five_minutes(
        tmp_path, monkeypatch, fake_time, caplog):
    """Its first_time raising, as on a full disk: the read ends, a read is
    needed, and a line queued behind is still handled."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(NOW - 1800, "can you check the boiler?")]
    real = store.first_time

    def full(key, payload=None):
        if key == f"D1:{NOW - 1800:.1f}":
            raise sqlite3.OperationalError("database or disk is full")
        return real(key, payload)
    monkeypatch.setattr(store, "first_time", full)
    waits = []

    async def go():
        p.slack_queue.put_nowait(heard())
        live(p, store, dm(f"{NOW - 5:.1f}", "hello?", channel="D2", user="U2"))
        await until(lambda: p._read_failing is not None and p._reading is None)
        for _ in range(5):
            waits.append(round((p._read_failing.next_try - time.monotonic()) / 60))
            p._read_failing = p._read_failing._replace(next_try=0)
            p._read_if_needed()
            await until(lambda: p._reading is None)
    with caplog.at_level(logging.WARNING, logger="wanda"):
        with_loop(p, go)
    assert waits == [1, 2, 4, 5, 5] and p._read_needed
    assert len(runner.calls) == 1 and "hello?" in runner.calls[0][0]
    assert (f"could not read back from Slack what was sent since {at_la(NOW - 3600)}: database or disk is full; "
            "tried again in 1 min") in caplog.text


def test_a_line_the_watcher_could_not_keep_is_read_back_once_the_store_takes_a_write(
        tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1")]
    line = said(NOW - 5, "is the plumber coming?")
    slack.histories["D1"] = [line]
    real = store.first_time

    def full(key, payload=None):
        raise sqlite3.OperationalError("database or disk is full")

    async def go():
        p.watcher.loop = asyncio.get_running_loop()
        monkeypatch.setattr(store, "first_time", full)
        with pytest.raises(sqlite3.OperationalError):
            p.watcher._handle(SimpleNamespace(send_socket_mode_response=lambda r: None), SimpleNamespace(
                type="events_api", envelope_id="e1", payload={"event": {**line, "channel": "D1",
                                                                        "channel_type": "im"}}))
        await until(lambda: p._read_needed)
        p.watcher.client.current_session.last_ping_pong_time = NOW
        monkeypatch.setattr(store, "first_time", real)
        await p.drain_mail()
        assert store.get_meta("heard_from") == iso(NOW - 3600), "held until the read succeeds"
        await until(lambda: p._reading is None)
        await p.drain_mail()
    with_loop(p, go)
    assert len(runner.calls) == 1 and "is the plumber coming?" in runner.calls[0][0]
    assert store.get_meta("heard_from") == iso(NOW - 600)


def test_a_read_held_past_its_time_ends_as_a_failure_and_is_alerted_after_ten_minutes(
        tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    monkeypatch.setattr(main, "READ_TIMEOUT_S", 0.1)
    slack.listed = [im("D1")]
    slack.hold[("history", "D1")] = asyncio.Event()

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._read_failing is not None and p._reading is None)
        assert p._read_needed and p._read_failing.error == "it took longer than 0.1 s"
        p._read_failing = p._read_failing._replace(next_try=time.monotonic() + 3600)
        await p.drain_mail()
        assert slack.alerts == []
        fake_time.at += timedelta(minutes=10)
        await p.drain_mail()
    with_loop(p, go)
    assert slack.alerts == [f"could not read back from Slack what was sent since {at_la(NOW - 3600)}: it took longer "
                            "than 0.1 s; tried again every few minutes until it can"]


@pytest.mark.parametrize("fault", ["_heard", "_start_read", "close"])
def test_a_fault_taking_a_new_connection_leaves_a_read_needed_and_the_next_line_handled(
        tmp_path, monkeypatch, fake_time, caplog, fault):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)

    def broken(*a, **kw):
        raise RuntimeError("a fault")
    if fault == "close":
        monkeypatch.setattr(p.gate, "close", broken)
    else:
        monkeypatch.setattr(Processor, fault, broken)

    async def go():
        p.slack_queue.put_nowait(heard())
        live(p, store, dm(f"{NOW - 5:.1f}", "hello?"))
        await until(lambda: runner.calls)
        p.watcher.client.current_session.last_ping_pong_time = NOW
        monkeypatch.setattr(Processor, "_start_read", lambda self, close=False: None)
        await p.drain_mail()
    with caplog.at_level(logging.ERROR, logger="wanda"):
        with_loop(p, go)
    assert "taking heard:s2 failed; the next pass reads back from Slack" in caplog.text
    assert p._read_needed and store.get_meta("heard_from") == iso(NOW - 3600)


@pytest.mark.parametrize("state, moves", [
    ("nothing needed", True), ("a new connection queued", False), ("a read running", False),
    ("a read needed", False), ("another session's pong", False), ("stopping", False), ("no pong yet", False),
    ("a pong from before heard_from", False)])
def test_a_pass_moves_heard_from_to_the_last_pong_less_the_margin_only_with_nothing_to_read(
        tmp_path, monkeypatch, fake_time, state, moves):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    p.watcher.client.current_session = Conn("s1", NOW - 30)
    monkeypatch.setattr(Processor, "_read_if_needed", lambda self: None)
    if state == "a new connection queued":
        p.watcher.client.current_session = Conn("s2", NOW - 30)
    elif state == "a read running":
        p._reading = SimpleNamespace(cancel=lambda: None)
    elif state == "a read needed":
        p._read_needed = True
    elif state == "another session's pong":
        p._heard_session = "s0"
    elif state == "stopping":
        p.stopping = True
    elif state == "no pong yet":
        p.watcher.client.current_session = Conn("s1", None)
    elif state == "a pong from before heard_from":
        p.watcher.client.current_session = Conn("s1", NOW - 7200)
    asyncio.run(p.drain_mail())
    assert store.get_meta("heard_from") == iso(NOW - 30 - 600 if moves else NOW - 3600)


def test_a_heard_from_that_cannot_be_read_holds_no_step_of_the_pass(tmp_path, monkeypatch, fake_time, caplog):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    store.set_meta("heard_from", "yesterday")
    p.watcher.client.current_session = Conn("s1", NOW - 30)
    took = []

    async def take_up_kept(self):
        took.append(1)
    monkeypatch.setattr(Processor, "_take_up_kept", take_up_kept)
    with caplog.at_level(logging.ERROR, logger="wanda"):
        asyncio.run(p.drain_mail())
    assert took == [1] and "could not move the time she last heard Slack" in caplog.text


def test_a_pass_whose_take_up_raises_still_moves_heard_from_and_starts_a_needed_read(tmp_path, monkeypatch,
                                                                                     fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    p.watcher.client.current_session = Conn("s1", NOW - 30)

    async def broken(self):
        raise RuntimeError("the take-up fails at every pass")
    monkeypatch.setattr(Processor, "_take_up_kept", broken)
    with pytest.raises(RuntimeError):
        asyncio.run(p.drain_mail())
    assert store.get_meta("heard_from") == iso(NOW - 30 - 600)
    p._read_needed = True
    started = []
    monkeypatch.setattr(Processor, "_start_read", lambda self, close=False: started.append(close))
    with pytest.raises(RuntimeError):
        asyncio.run(p.drain_mail())
    assert started == [False]


# --- a read that ends part way, and what it kept ------------------------------

def missed_in_d1(slack, thread_error=None):
    """Two lines in fan's DM missed, the second with a reply under it, whose
    thread call fails with `thread_error`."""
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(NOW - 1800, "one"), said(NOW - 1700, "two", latest_reply=f"{NOW - 1650:.1f}")]
    slack.replies_of[("D1", f"{NOW - 1700:.1f}")] = []
    if thread_error is not None:
        slack.read_errors[("thread", "D1", f"{NOW - 1700:.1f}")] = thread_error


def test_a_read_that_ends_on_the_network_hands_on_what_it_kept_with_a_line_waiting_there(
        tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late_adds=True)
    missed_in_d1(slack, CallFailed("conversations_replies: network unreachable"))
    reading = slack.hold[("conversations",)] = asyncio.Event()

    async def go():
        p.slack_queue.put_nowait(heard())
        live(p, store, dm(f"{NOW - 5:.1f}", "three"))
        await until(lambda: ("D1", f"{NOW - 5:.1f}") in slack.reacted)
        reading.set()
    with_loop(p, go)
    [prompt] = frames(runner)
    assert "fan: one\n" in prompt and "fan: two\n" in prompt and "fan now says:\n\n    three\n\n" in prompt
    assert p._read_needed and p._read_failing is not None and kept(store) == []


def failing_task_write(store, monkeypatch, failing):
    real = store.create_task

    def create_task(*a, **kw):
        if failing[0]:
            raise sqlite3.OperationalError("database or disk is full")
        return real(*a, **kw)
    monkeypatch.setattr(store, "create_task", create_task)


@pytest.mark.parametrize("next_read", ["the store back", "the store still failing", "deleted between"])
def test_a_line_whose_task_the_read_could_not_make_is_handed_on_first_by_the_next_read(
        tmp_path, monkeypatch, fake_time, next_read):
    """Kept, its task write raising: the row is seen, so no later read keeps
    it again, and is left with the Processor. The next read makes its task
    and hands it on before its first call; one whose write raises again
    leaves it, and one deleted meanwhile is not handed on."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(NOW - 1800, "can you check the boiler?")]
    failing = [True]
    failing_task_write(store, monkeypatch, failing)
    key = ("D1", f"{NOW - 1800:.1f}")
    at_second = []

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._read_failing is not None and p._reading is None)
        assert list(p._unhanded) == [key] and runner.calls == []
        if next_read == "the store still failing":
            p._read_failing = p._read_failing._replace(next_try=0)
            p._read_if_needed()
            await until(lambda: p._reading is None)
            assert list(p._unhanded) == [key] and slack.reacted == []
        if next_read == "deleted between":
            p.slack_queue.put_nowait(Event(source="slack", dedupe_key="x", payload={
                "kind": "deleted", "channel": "D1", "ts": key[1]}))
            await until(lambda: not p._unhanded)
        failing[0] = False
        slack.on_read = lambda *call: at_second.append(len(p._bg)) if call == ("conversations",) else None
        p._read_failing = p._read_failing._replace(next_try=0)
        p._read_if_needed()
        await until(lambda: p._reading is None)
    with_loop(p, go)
    if next_read == "deleted between":
        assert runner.calls == [] and kept(store) == []
        return
    assert at_second == [1], "handed on before the next read's first call"
    [prompt] = frames(runner)
    assert slack.reacted == [key]
    assert "can you check the boiler?" in prompt and kept(store) == []


def test_what_a_read_kept_waits_for_the_next_read_when_saying_what_is_still_kept_fails(
        tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late_adds=True)
    missed_in_d1(slack, CallFailed("conversations_replies: network unreachable"))
    reading = slack.hold[("conversations",)] = asyncio.Event()
    fail_kept = [False]
    real = store.kept

    def kept_rows(channel=None, task_key=None):
        if fail_kept[0] and channel is not None:
            fail_kept[0] = False
            raise sqlite3.OperationalError("disk I/O error")
        return real(channel, task_key)
    monkeypatch.setattr(store, "kept", kept_rows)
    slack.on_read = lambda *call: fail_kept.__setitem__(0, True) if call[0] == "thread" else None

    async def go():
        p.slack_queue.put_nowait(heard())
        live(p, store, dm(f"{NOW - 5:.1f}", "three"))
        await until(lambda: ("D1", f"{NOW - 5:.1f}") in slack.reacted)
        reading.set()
        await until(lambda: p._reading is None and runner.calls)
        assert sorted(p._unhanded) == [("D1", f"{NOW - 1800:.1f}"), ("D1", f"{NOW - 1700:.1f}")]
        slack.on_read = None
        del slack.read_errors[("thread", "D1", f"{NOW - 1700:.1f}")]
        p._read_failing = p._read_failing._replace(next_try=0)
        p._read_if_needed()
    with_loop(p, go)
    first, second = frames(runner)
    assert "fan says to me, in a direct message:\n\n    three\n\n" in first
    assert "fan: one\n" in second and "fan now says:\n\n    two\n\n" in second


def test_a_stop_during_a_read_owes_nothing_and_moves_nothing(tmp_path, monkeypatch, fake_time):
    """The stop cancels the read; a pass while it waits for the session
    running moves nothing, since that read may have left conversations
    unread, and the next start reads from the old time."""
    runner = Working(answer("Noted."))
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, runner=runner)
    slack.listed = [im("D1")]
    slack.hold[("conversations",)] = asyncio.Event()
    p.watcher.client.current_session = Conn("s1", NOW - 30)

    async def go():
        live(p, store, dm(f"{NOW - 30:.1f}", "is the plumber coming?"))
        await until(lambda: runner.feeds)
        p.slack_queue.put_nowait(heard("s1", refresh=True))
        await until(lambda: p._reading is not None)
        stopping = asyncio.create_task(p.let_finish(5))
        await until(lambda: p._reading is None)
        await p.drain_mail()
        runner.done.set()
        await stopping
    with_loop(p, go)
    assert slack.replies == ["Noted."]
    assert store.get_meta("heard_from") == iso(NOW - 3600) and not p._read_needed and p._read_failing is None


@pytest.mark.parametrize("kind", ["heard", "owed"])
def test_a_new_connection_or_a_line_not_kept_while_stopping_reads_nothing(tmp_path, monkeypatch, fake_time, kind):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    p.stopping = True

    async def go():
        p.slack_queue.put_nowait(heard() if kind == "heard" else Event("slack", "owed:e", {"kind": "owed"}))
        await until(lambda: p.slack_queue.empty())
        await asyncio.sleep(0.02)
    with_loop(p, go)
    assert slack.reads == [] and not p._read_needed and not p.gate.closed


def test_shutdown_cancels_a_read_still_running_and_waits_for_it(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1")]
    slack.hold[("conversations",)] = asyncio.Event()

    async def go():
        p._read_needed = True
        p._read_if_needed()
        reading = p._reading
        await until(lambda: ("conversations",) in slack.reaching)
        await p.shutdown(grace_s=1)
        return reading.cancelled()
    assert asyncio.run(go())


def test_shutdown_passes_over_a_new_connection_and_a_line_not_kept(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    p.slack_queue.put_nowait(heard())
    p.slack_queue.put_nowait(Event("slack", "owed:e", {"kind": "owed"}))
    asyncio.run(p.shutdown(grace_s=1))
    assert p.slack_queue.empty() and store._query("SELECT id FROM runs") == []


def test_shutdown_waits_for_a_post_of_what_is_owed_that_its_turn_went_on_without(tmp_path, monkeypatch, fake_time):
    """Not cancelling it, so that the run Slack has is not left owed for the
    next start to post again."""
    runner = RecordingRunner(answer("Coming Tuesday."))
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, runner=runner)
    monkeypatch.setattr(main, "OWED_IN_SLOT_S", 0.1)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(NOW - 3700), exit_code=0,
                     cost_usd=0.0, status="ok", result_text="An answer owed.", notified=0)
    posted = slack.reply

    async def reply(thread_ts, text, channel=None):
        if text == "An answer owed.":
            await asyncio.sleep(0.3)
        await posted(thread_ts, text, channel)
    slack.reply = reply

    async def go():
        live(p, store, dm(f"{NOW - 5:.1f}", "is the plumber coming?"))
        await until(lambda: runner.calls, timeout=2)
        assert p._owed_posts, "what was owed was posted inside its bound"
        await p.shutdown(grace_s=1)
        return [r["result_text"] for r in store.pending_deliveries()]
    assert "An answer owed." not in with_loop(p, go) and slack.replies[:1] == ["An answer owed."]


# --- at a start ------------------------------------------------------------------

def la_now():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles"))


def at_start(ts: float) -> str:
    """A time as a frame made now says it."""
    return vault.stamp(ts, la_now())


class Lateness:
    """monkeypatch, leaving the lateness of a turn and of her reaction as the
    test set them where a_start would set them aside."""

    def __init__(self, mp):
        self.mp = mp

    def setattr(self, target, name, value=None, raising=True):
        if target is main and name in ("LATE_TURN_S", "LATE_ADD_S"):
            return
        if value is None:
            return self.mp.setattr(target, name, raising=raising)
        return self.mp.setattr(target, name, value, raising=raising)


def store_down_an_hour(tmp_path, t):
    """The run store a start opens: she last ran, and last heard Slack, an
    hour before `t`."""
    store = Store(start_config(tmp_path).db_path)
    store.set_meta("up_at", iso(t - 3600))
    store.set_meta("heard_from", iso(t - 3600))
    return store


def starting(tmp_path, monkeypatch, runner, slack, late=True, **kw):
    """A start through run_daemon (a_start), the gate's wait short, her
    reaction's pace too, and turns late as the daemon makes them."""
    monkeypatch.setattr(main, "READ_WAIT_S", 0.5)
    monkeypatch.setattr(main, "GATE_SLACK_S", 0.1)
    monkeypatch.setattr(main, "READ_EVERY_S", 0.01)
    monkeypatch.setattr(main, "LATE_TURN_S", LATE_TURN_S if late else 10 ** 9)
    monkeypatch.setattr(main, "LATE_ADD_S", 60 if late else 10 ** 9)
    runner.agent_sem = asyncio.Semaphore(1)
    a_start(tmp_path, Lateness(monkeypatch), runner, slack, **kw)


def handle_live(slack, event):
    """A line as Slack sends it to her live, while she runs."""
    slack.watcher._handle(SimpleNamespace(send_socket_mode_response=lambda r: None),
                          SimpleNamespace(type="events_api", envelope_id="e", payload={"event": event}))


def test_a_start_reads_back_what_was_sent_while_she_was_down_and_takes_each_conversation_up_late(
        tmp_path, monkeypatch):
    """Everything sent to her in the hour she was down, in every kind of
    place: each kept, her reaction on, each conversation's oldest first and
    whoever waited longest first, one turn for each conversation, framed late
    with when she was not running."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    keep(store, dm(f"{t - 3000:.1f}", "the stop left this"))
    keep(store, dm(f"{t - 700:.1f}", "and this, in mei's DM", channel="D2", user="U2"))
    alert, older, answered = f"{t - 2 * 86400:.1f}", f"{t - 20 * 86400:.1f}", f"{t - 2 * 86400 + 60:.1f}"
    store.create_task(None, "C1", alert, kind="mention")
    task = store.create_task(None, "C1", older, kind="mention")
    store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(t - 3 * 86400), exit_code=0,
                     cost_usd=0.0, status="ok")
    slack = Slack()
    slack.listed = [public("C1"), private("P1"), mpim("G1"), im("D1"), im("D2")]
    one, mention, theirs = f"{t - 1800:.1f}", f"{t - 1300:.1f}", f"{t - 2000:.1f}"
    slack.histories["D1"] = [
        said(t - 2 * 86400 + 60, "Here it is.", user="UBOT", bot_id="BME", latest_reply=f"{t - 1200:.1f}"),
        said(t - 1800, "dm one", latest_reply=f"{t - 1500:.1f}", thread_ts=one), said(t - 1700, "dm two")]
    slack.replies_of[("D1", one)] = [said(t - 1800, "dm one", thread_ts=one),
                                     said(t - 1500, "a threaded DM reply", thread_ts=one)]
    slack.replies_of[("D1", answered)] = [said(t - 1150, "thanks, and the other one?", thread_ts=answered)]
    slack.histories["G1"] = [said(t - 1600, "a group DM line", user="U2")]
    slack.histories["P1"] = [said(t - 1400, "<@UBOT> a private channel's mention")]
    slack.histories["C1"] = [
        said(t - 2 * 86400, "a vault snapshot failed", user="UBOT", bot_id="BME", latest_reply=f"{t - 1100:.1f}"),
        said(t - 2000, "who knows a plumber?", user="U2", latest_reply=f"{t - 1000:.1f}", thread_ts=theirs),
        said(t - 1300, "<@UBOT> a channel mention", latest_reply=f"{t - 1250:.1f}", thread_ts=mention)]
    slack.replies_of[("C1", mention)] = [said(t - 1300, "<@UBOT> a channel mention", thread_ts=mention),
                                         said(t - 1250, "a reply under it", thread_ts=mention)]
    slack.replies_of[("C1", alert)] = [said(t - 1100, "what does this mean?", thread_ts=alert)]
    slack.replies_of[("C1", theirs)] = [said(t - 1000, "<@UBOT> do you?", thread_ts=theirs)]
    slack.replies_of[("C1", older)] = [said(t - 900, "back to this one", thread_ts=older)]
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    order = ["the stop left this", "a group DM line", "a threaded DM reply", "a private channel's mention",
             "a reply under it", "thanks, and the other one?", "what does this mean?", "do you?",
             "back to this one", "and this, in mei's DM"]
    assert [next(i for i, text in enumerate(order) if text in f) for f in frames(runner)] == list(range(10))
    for f in frames(runner):
        assert "reaches me only now. I was not running from " in f
    assert "dm one" in frames(runner)[0] and "dm two" in frames(runner)[0]
    assert "In a Slack channel that" in frames(runner)[3] and "anyone in this Slack can read" not in frames(runner)[3]
    assert "a channel mention" in frames(runner)[4]
    assert store.get_task_by_thread("C1", mention)["kind"] == "mention"
    assert store.get_task_by_thread("C1", theirs)["kind"] == "mention_guest"
    expected = [("D1", f"{t - 3000:.1f}"), ("D1", one), ("D1", f"{t - 1700:.1f}"), ("G1", f"{t - 1600:.1f}"),
                ("D1", f"{t - 1500:.1f}"), ("P1", f"{t - 1400:.1f}"), ("C1", mention), ("C1", f"{t - 1250:.1f}"),
                ("D1", f"{t - 1150:.1f}"), ("C1", f"{t - 1100:.1f}"), ("C1", f"{t - 1000:.1f}"),
                ("C1", f"{t - 900:.1f}"), ("D2", f"{t - 700:.1f}")]
    assert slack.reacted == expected, "each conversation's oldest first, whoever waited longest first"
    assert kept(store) == []


@pytest.mark.parametrize("owed", [None, "D1"], ids=["nothing owed", "an answer owed in the first"])
def test_backlogs_take_the_session_in_the_order_of_their_oldest_rows_before_a_line_sent_as_she_starts(
        tmp_path, monkeypatch, owed):
    """fan's DM and the group DM, their rows interleaved in time, and a line
    in mei's DM during the read: the backlogs hold the session's place in
    the order of their oldest rows, their reaction going on fan's DM's then
    the group DM's, each oldest first, and the line sent as she starts is
    answered after both, its reaction on at once. Posting what is owed in
    the first conversation, inside its place, moves nothing."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    if owed:
        task = store.create_task(None, "D1", "conversation", kind="dm")
        store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(t - 3700), exit_code=0,
                         cost_usd=0.0, status="ok", result_text="An answer owed.", notified=0)
    slack = Slack()
    posted = slack.reply

    async def reply(thread_ts, text, channel=None):
        # a post gives the loop to the next turn as a real one does
        await asyncio.sleep(0.01)
        await posted(thread_ts, text, channel)
    slack.reply = reply
    slack.listed = [im("D1"), mpim("G1"), im("D2")]
    slack.histories["D1"] = [said(t - 1000, "fan one"), said(t - 800, "fan three")]
    slack.histories["G1"] = [said(t - 900, "mei two", user="U2"), said(t - 700, "mei four", user="U2")]
    slack.on_read = lambda *call: handle_live(slack, {
        "type": "message", "user": "U2", "channel": "D2", "channel_type": "im", "ts": f"{time.time():.6f}",
        "text": "is anyone there?"}) if call == ("history", "D2") else None
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    assert ["fan three" in f for f in frames(runner)] == [True, False, False]
    assert "mei four" in frames(runner)[1] and "is anyone there?" in frames(runner)[2]
    reacted = slack.reacted
    assert reacted[0][0] == "D2"
    assert reacted[1:] == [("D1", f"{t - 1000:.1f}"), ("D1", f"{t - 800:.1f}"), ("G1", f"{t - 900:.1f}"),
                           ("G1", f"{t - 700:.1f}")]
    assert slack.replies[:1] == (["An answer owed."] if owed else [])


def test_what_is_owed_that_slack_is_slow_to_take_holds_a_sessions_place_no_longer_than_its_bound(
        tmp_path, monkeypatch, fake_time):
    """The turn's session runs while the post goes on, which is not cut
    short, since Slack may already have it; the session's answer is posted
    once, its call begun after the owed post's has returned."""
    runner = RecordingRunner(answer("Coming Tuesday."))
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, runner=runner)
    monkeypatch.setattr(main, "OWED_IN_SLOT_S", 0.1)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(NOW - 3700), exit_code=0,
                     cost_usd=0.0, status="ok", result_text="An answer owed.", notified=0)
    posted, at = slack.reply, {}

    async def reply(thread_ts, text, channel=None):
        # when each post's call begins, and when the owed one's returns
        at.setdefault(text, []).append(time.monotonic())
        await posted(thread_ts, text, channel)
        if text == "An answer owed.":
            await asyncio.sleep(0.5)  # Slack has it, and is slow to say so
            at[text].append(time.monotonic())
    slack.reply = reply

    async def go():
        live(p, store, dm(f"{NOW - 5:.1f}", "is the plumber coming?"))
        await until(lambda: runner.calls, timeout=2)
        posting = bool(p._owed_posts)
        await until(lambda: not p._owed_posts)
        await p.drain_mail()
        return posting
    assert with_loop(p, go), "the session ran while what was owed was still posting"
    assert slack.replies == ["An answer owed.", "Coming Tuesday."]
    assert at["Coming Tuesday."][0] >= at["An answer owed."][1], "the answer went while the owed post was in flight"


def test_a_post_of_what_is_owed_that_fails_after_its_turn_went_on_is_logged(tmp_path, monkeypatch, fake_time,
                                                                             caplog):
    runner = RecordingRunner(answer("Coming Tuesday."))
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, runner=runner)
    monkeypatch.setattr(main, "OWED_IN_SLOT_S", 0.1)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(NOW - 3700), exit_code=0,
                     cost_usd=0.0, status="ok", result_text="An answer owed.", notified=0)

    async def fails_late(task_id=None):
        await asyncio.sleep(0.3)
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(p, "deliver_pending", fails_late)

    async def go():
        live(p, store, dm(f"{NOW - 5:.1f}", "is the plumber coming?"))
        await until(lambda: runner.calls, timeout=2)
        await until(lambda: not p._owed_posts)
    with caplog.at_level(logging.ERROR, logger="wanda"):
        with_loop(p, go)
    assert "could not post what was owed" in caplog.text and "database or disk is full" in caplog.text


def test_a_row_whose_task_the_starts_read_could_not_make_is_taken_up_with_the_rest(tmp_path, monkeypatch):
    """The read ends on the store at its second conversation, the group DM's
    row seen and its task unmade: the start's step makes the task and takes
    it up."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    slack = Slack()
    slack.listed = [im("D1"), mpim("G1")]
    slack.histories["D1"] = [said(t - 1000, "fan's line")]
    slack.histories["G1"] = [said(t - 900, "mei's line", user="U2")]
    real, failed = Store.create_task, []

    def create_task(self, message_pk, channel, *a, **kw):
        if channel == "G1" and not failed:
            failed.append(1)
            raise sqlite3.OperationalError("database or disk is full")
        return real(self, message_pk, channel, *a, **kw)
    monkeypatch.setattr(Store, "create_task", create_task)
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    assert failed and ["fan's line" in f for f in frames(runner)] == [True, False]
    assert "mei's line" in frames(runner)[1] and kept(store) == []


def test_what_starts_nothing_is_not_taken(tmp_path, monkeypatch, caplog):
    """One at the time she last heard Slack, one seen, a bot's, hers, an id
    not let in, chatter, a join, a tombstone, a thread's first message older
    than that time, which Slack gives with its replies, and in a channel, or
    a private channel of older payloads, a mention before her joining it."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    store.first_time(f"D1:{t - 1000:.1f}")
    first = f"{t - 2 * 86400:.1f}"
    slack = Slack()
    slack.listed = [im("D1"), public("C1"), private("P1"), public("C2")]
    slack.histories["D1"] = [said(t - 3600, "at the bound"), said(t - 1000, "seen"),
                             said(t - 900, "a bot's", bot_id="B9"), said(t - 800, "hers", user="UBOT"),
                             said(t - 700, "not let in", user="U9")]
    slack.histories["C1"] = [said(t - 1500, "<@UBOT> before she joined"),
                             said(t - 1400, "", user="UBOT", subtype="channel_join"),
                             said(t - 600, "morning all"), said(t - 500, "joined", subtype="channel_join"),
                             said(t - 400, "This message was deleted.", subtype="tombstone", user="USLACKBOT")]
    slack.histories["P1"] = [said(t - 1500, "<@UBOT> before she joined, privately"),
                             said(t - 1400, "", user="UBOT", subtype="group_join")]
    slack.histories["C2"] = [said(t - 2 * 86400, "<@UBOT> an old mention", latest_reply=f"{t - 1000:.1f}",
                                  thread_ts=first, user="U2")]
    slack.replies_of[("C2", first)] = [said(t - 2 * 86400, "<@UBOT> an old mention", thread_ts=first, user="U2"),
                                       said(t - 1000, "agreed", thread_ts=first, user="U2")]
    runner = RecordingRunner()
    with caplog.at_level(logging.INFO, logger="wanda"):
        starting(tmp_path, monkeypatch, runner, slack)
    assert runner.calls == [] and kept(store) == [] and slack.reacted == []
    assert "read back from Slack since " in caplog.text and ": 0 message(s) to her in 0 conversation(s)" in caplog.text


def test_another_members_joining_a_channel_hides_nothing_before_it(tmp_path, monkeypatch):
    t = float(int(time.time()))
    store_down_an_hour(tmp_path, t)
    slack = Slack()
    slack.listed = [public("C1")]
    slack.histories["C1"] = [said(t - 1500, "<@UBOT> are you there?"),
                             said(t - 1400, "", user="U2", subtype="channel_join")]
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    [prompt] = frames(runner)
    assert "are you there?" in prompt


def test_a_kept_row_slack_says_is_gone_is_withdrawn_and_runs_nothing(tmp_path, monkeypatch):
    """Kept before the stop and deleted while she was down: her reaction,
    late, meets message_not_found, and the take-up, its place free, frames
    nothing."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    keep(store, dm(f"{t - 3000:.1f}", "sent to the wrong person"))
    slack = Slack()
    slack.react_errors[("D1", f"{t - 3000:.1f}")] = slack_error("message_not_found")
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    assert runner.calls == [] and kept(store) == [] and slack.replies == []


@pytest.mark.parametrize("owed", ["an answer a stop left", "her note Slack refused", "two stops cut it short",
                                  "her note on the cap", "her note on a hold"])
def test_a_kept_row_an_owed_run_answers_slack_says_is_gone_gets_nothing_posted(tmp_path, monkeypatch, owed):
    """Answered before the stop, or given her note at the start, and deleted
    while she was down: her reaction, put on again and late, meets
    message_not_found before the run is posted, and nothing is."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    line = keep(store, dm(f"{t - 3000:.1f}", "sent to the wrong person"))
    task = store.create_task(None, "D1", "conversation", kind="dm")
    if owed == "an answer a stop left":
        store.record_run(kind="agent", task_id=task, session_id="s1", started_at=utcnow(), exit_code=None,
                         cost_usd=0.0, status="ok", result_text="Noted, the 14th.", notified=0,
                         settled=Settled(answered=(line,)))
    elif owed == "her note Slack refused":
        store.record_run_and_note(main.FAILED, noted=Settled(answered=(line,)), kind="agent", task_id=task,
                                  session_id="s1", started_at=utcnow(), exit_code=1, cost_usd=0.0, status="error")
    elif owed == "two stops cut it short":
        store.took("s1", [line], {line})
        store.took("s2", [line], {line})
    else:
        kept_as = (Settled(capped=(line,)) if owed == "her note on the cap" else Settled(held=((line, "s1"),)))
        store.record_run(kind="note", task_id=task, session_id=None, started_at=utcnow(), exit_code=None,
                         cost_usd=0.0, status="ok", notified=0, settled=kept_as,
                         result_text=main.CAPPED_NOTE if owed == "her note on the cap" else main.HELD)
    slack = Slack()
    slack.react_errors[line] = slack_error("message_not_found")
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    assert runner.calls == [] and slack.replies == [] and kept(store) == [] and store.pending_deliveries() == []


def test_a_hold_confirmed_after_a_start_tells_no_conversation_whose_held_messages_were_deleted(
        tmp_path, monkeypatch, fake_time):
    """Two conversations held across a stop, the hold not yet confirmed; one
    message deleted while she was down. The hold's try a pass makes is
    refused again, which confirms the hold: her note goes where a held
    message stands, and her reaction, put on again and late, meets
    message_not_found where none does, so nothing is posted there."""
    from test_processor import Limited
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, runner=Limited(), late_adds=True)
    stands = keep(store, dm(f"{NOW - 8 * 3600:.1f}", "is it paid?"))
    gone = keep(store, dm(f"{NOW - 7 * 3600:.1f}", "sent to the wrong person", channel="D2", user="U2"))
    store.create_task(None, "D1", "conversation", kind="dm")
    store.create_task(None, "D2", "conversation", kind="dm")
    store._exec("UPDATE unanswered SET state='held'")
    store.set_meta("held_since", iso(NOW - 300))
    slack.react_errors[gone] = slack_error("message_not_found")

    async def go():
        await p.drain_mail()
    with_loop(p, go)
    assert len(runner.calls) == 1 and slack.replies == [main.HELD] and slack.channels == ["D1"]
    assert [(r["ts"], r["state"]) for r in store.kept()] == [(stands[1], "held")]


def test_an_owed_answer_to_two_messages_both_gone_while_she_was_down_is_not_posted(tmp_path, monkeypatch):
    """Her reaction goes on again on each message the answer answers, and
    Slack saying each is gone leaves nothing to post."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    first = keep(store, dm(f"{t - 3100:.1f}", "remind me at 5"))
    second = keep(store, dm(f"{t - 3000:.1f}", "and the dentist"))
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.record_run(kind="agent", task_id=task, session_id="s1", started_at=utcnow(), exit_code=None,
                     cost_usd=0.0, status="ok", result_text="Both noted.", notified=0,
                     settled=Settled(answered=(first, second)))
    slack = Slack()
    slack.react_errors[first] = slack.react_errors[second] = slack_error("message_not_found")
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    assert runner.calls == [] and slack.replies == [] and kept(store) == [] and store.pending_deliveries() == []


def test_an_owed_answer_with_none_of_its_own_is_checked_by_her_note_after_it_at_a_start(tmp_path, monkeypatch):
    """The answer's own message was deleted while she worked, and her note
    after it asks for the one added since, which is deleted while she is
    down: neither is posted."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    first = keep(store, dm(f"{t - 3100:.1f}", "remind me at 5"))
    added = keep(store, dm(f"{t - 3000:.1f}", "and the dentist"))
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.forget([first], deleted=True)
    store.record_run_and_note(main.FAILED_REST, settled=Settled(answered=()), noted=Settled(answered=(added,)),
                              kind="agent", task_id=task, session_id="s1", started_at=utcnow(), exit_code=1,
                              cost_usd=0.0, status="ok", result_text="At 5, then.", notified=0)
    slack = Slack()
    slack.react_errors[added] = slack_error("message_not_found")
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    assert runner.calls == [] and slack.replies == [] and kept(store) == [] and store.pending_deliveries() == []


def test_an_owed_answer_asks_slack_again_only_about_its_messages_not_known_deleted(tmp_path, monkeypatch):
    """One of the answer's two messages was deleted before the stop, so she
    already knows it is gone: her reaction goes on again on the other alone,
    and the answer is posted for it."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    first = keep(store, dm(f"{t - 3100:.1f}", "remind me at 5"))
    second = keep(store, dm(f"{t - 3000:.1f}", "and the dentist"))
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.record_run(kind="agent", task_id=task, session_id="s1", started_at=utcnow(), exit_code=None,
                     cost_usd=0.0, status="ok", result_text="Both noted.", notified=0,
                     settled=Settled(answered=(first, second)))
    store.forget([first], deleted=True)
    slack = Slack()
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    assert slack.replies == ["Both noted."] and [k for k, _ in slack.react_times] == [second]


def test_an_edited_message_is_taken_as_it_stands(tmp_path, monkeypatch):
    t = float(int(time.time()))
    store_down_an_hour(tmp_path, t)
    slack = Slack()
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(t - 1000, "is the plumber coming on Tuesday?", edited={"ts": f"{t - 900:.1f}"})]
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    [prompt] = frames(runner)
    assert "    is the plumber coming on Tuesday?" in prompt


def test_a_new_store_reads_nothing_back(tmp_path, monkeypatch, caplog):
    """A store's first start: what was sent before it was never hers."""
    t = time.time()
    slack = Slack()
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(t - 600, "hello?")]
    runner = RecordingRunner()
    with caplog.at_level(logging.INFO, logger="wanda"):
        starting(tmp_path, monkeypatch, runner, slack)
    store = Store(start_config(tmp_path).db_path)
    started = datetime.fromisoformat(store.get_meta("heard_from"))
    assert abs(started.timestamp() - t) < 5 and runner.calls == []
    assert f"read back from Slack since {at_start(started.timestamp())}: 0 message(s)" in caplog.text
    assert slack.oldest["D1"] == pytest.approx(t - 6 * 86400, abs=5)


async def until_read(self):
    """The mail loop of a start: once the read and what it set going have
    ended, a pass, and the stop."""
    import os
    import signal
    while self._bg or self._reading is not None:
        await asyncio.sleep(0.01)
    await self.drain_mail()
    os.kill(os.getpid(), signal.SIGTERM)


def release_once(slack, call, cond, after=0.0):
    """Holds the read's `call` until `cond` holds, and `after` seconds more."""
    held = slack.hold[call] = asyncio.Event()

    async def release():
        await until(cond)
        await asyncio.sleep(after)
        held.set()
    slack.on_read = (lambda prev: lambda *c: (prev and prev(*c), c == call and asyncio.get_running_loop()
                                               .create_task(release())))(slack.on_read)


@pytest.mark.parametrize("how", ["ends", "raises", "past the wait", "same step"])
def test_a_line_sent_as_she_starts_waits_for_what_was_missed_there_and_joins_its_turn(tmp_path, monkeypatch, how):
    """fan writes in his DM as she starts, where two lines were missed: his
    line gets her reaction at once, before the start's step unless it comes
    in that step's own loop step, and waits, the start's read ending, or
    raising at the next conversation, or running past the wait having read
    his DM, or his line coming in the step's own loop step, just before it;
    in each, one turn there, the missed lines first, framed late by the
    first of them."""
    t = float(int(time.time()))
    store_down_an_hour(tmp_path, t)
    slack = Slack()
    slack.listed = [im("D1"), public("C1")]
    slack.histories["D1"] = [said(t - 1800, "one"), said(t - 1700, "two")]
    now = {}
    started = Processor.read_at_start

    def read_at_start(self, watcher):
        now["processor"] = self
        return started(self, watcher)
    monkeypatch.setattr(Processor, "read_at_start", read_at_start)
    reacted = slack.react

    async def react(channel, ts):
        if (channel, ts) == ("D1", now.get("ts")):
            now["before the step"] = now["processor"]._collecting
        await reacted(channel, ts)
    slack.react = react

    def line():
        now["ts"] = f"{time.time():.6f}"
        handle_live(slack, {"type": "message", "user": "U1", "channel": "D1", "channel_type": "im",
                            "ts": now["ts"], "text": "three"})
    if how == "same step":
        # put as the read makes its last call, so that slack_loop makes its
        # handler in the loop step the start's step runs in, just before it
        slack.on_read = lambda *call: call == ("history", "C1") and line()
    else:
        slack.on_read = lambda *call: call == ("history", "D1") and line()
        release_once(slack, ("history", "C1"), lambda: ("D1", now.get("ts")) in slack.reacted,
                     after=0.8 if how == "past the wait" else 0.0)
    if how == "raises":
        slack.read_errors[("history", "C1")] = slack_error("ratelimited")
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    [prompt] = frames(runner)
    assert (f"What fan says below was sent at {at_start(t - 1800)} and reaches me only now. I was not running from "
            in prompt)
    assert f"{at_start(t - 1800)} fan: one\n" in prompt and f"{at_start(t - 1700)} fan: two\n" in prompt
    assert "fan now says:\n\n    three\n\n" in prompt
    # his line put in the step's own loop step has its reaction made after it
    assert now["before the step"] == (how != "same step")


class Taking(RecordingRunner):
    """The first session takes in `n` messages added while it works, in the
    order it is handed them."""

    def __init__(self, *reports, n=1):
        super().__init__(*reports)
        self.n = n
        self.added: list[str] = []
        self.working = asyncio.Event()

    async def run(self, prompt, **kw):
        if (feed := kw.get("feed")) is not None and not self.calls:
            self.working.set()
            for _ in range(self.n):
                if (text := await asyncio.wait_for(feed.next(), 2)) is None:
                    break
                self.added.append(text)
            feed.close()
        return await super().run(prompt, **kw)


def test_a_line_sent_after_a_quick_restart_reaches_the_session_its_backlog_began(tmp_path, monkeypatch):
    """What was missed is under a minute old, so its turn is framed at once,
    as nothing is late, and the line waiting at the gate is offered to its
    session."""
    t = time.time()
    store_down_an_hour(tmp_path, t)
    slack = Slack()
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(t - 40, "one"), said(t - 30, "two")]
    slack.on_read = lambda *call: call == ("history", "D1") and handle_live(slack, {
        "type": "message", "user": "U1", "channel": "D1", "channel_type": "im", "ts": f"{time.time():.6f}",
        "text": "are you back?"})
    runner = Taking()
    starting(tmp_path, monkeypatch, runner, slack)
    first = frames(runner)[0]
    assert "fan: one\n" in first and "fan now says:\n\n    two\n\n" in first and "are you back?" not in first
    assert len(runner.added) == 1 and "are you back?" in runner.added[0]


def test_a_connection_replaced_as_she_starts_is_taken_as_a_later_ones_with_no_second_read(tmp_path, monkeypatch):
    """A `heard` already queued when the start's read begins: a read is
    needed once it ends, and no second one ran alongside it."""
    import test_processor
    t = float(int(time.time()))
    store_down_an_hour(tmp_path, t)
    slack = Slack()
    slack.listed = [im("D1")]
    connected = test_processor.connected

    def replaced(watcher):
        connected(watcher)
        watcher._heard("s2", False)
    monkeypatch.setattr(test_processor, "connected", replaced)
    seen = {}

    async def loop(self):
        import os
        import signal
        while self._bg or self._reading is not None:
            await asyncio.sleep(0.01)
        seen["needed"], seen["lists"] = self._read_needed, slack.reads.count(("conversations",))
        os.kill(os.getpid(), signal.SIGTERM)
    starting(tmp_path, monkeypatch, RecordingRunner(), slack, loop=loop)
    assert seen == {"needed": True, "lists": 1}


def test_the_starts_connection_is_the_one_heard_from_moves_with(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    p._heard_session = None
    p.watcher.client.current_session = Conn("s1", NOW - 30)

    async def go():
        p.read_at_start(p.watcher)
        await until(lambda: p._reading is None)
        p.collected([])
        p.gate.open_all()
        await p.drain_mail()
    with_loop(p, go)
    assert store.get_meta("heard_from") == iso(NOW - 30 - 600)


def test_a_start_whose_read_runs_past_the_wait_hands_on_what_it_finds_after(tmp_path, monkeypatch):
    """What it read by then is taken up; what it reads after is handed on as
    while she runs; each runs once."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    slack = Slack()
    slack.listed = [im("D1"), public("C1")]
    slack.histories["D1"] = [said(t - 1800, "fan's line")]
    slack.histories["C1"] = [said(t - 1700, "<@UBOT> a channel's mention", user="U2")]
    release_once(slack, ("history", "C1"), lambda: True, after=0.8)
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack, loop=until_read)
    assert ["fan's line" in f for f in frames(runner)] == [True, False]
    assert "a channel's mention" in frames(runner)[1] and kept(store) == []


def test_a_stop_during_the_starts_read_runs_nothing_and_loses_nothing(tmp_path, monkeypatch, caplog):
    """The step runs without its take-ups, so that the time she was down is
    recorded: no session, nothing posted by the start's recovery, every row
    still due, `heard_from` as it was, and the next start told of the time
    up to this one and of the time after it."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    keep(store, dm(f"{t - 3000:.1f}", "the stop left this"))
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(t - 3700), exit_code=0,
                     cost_usd=0.0, status="ok", result_text="An answer owed.", notified=0)
    slack = Slack()
    slack.listed = [im("D1"), public("C1")]
    slack.histories["D1"] = [said(t - 1800, "fan's line")]
    slack.hold[("history", "C1")] = asyncio.Event()

    def stop_and_write(*call):
        if call == ("history", "D1"):
            import os
            import signal
            for user, channel in (("U1", "D1"), ("U2", "D2")):
                handle_live(slack, {"type": "message", "user": user, "channel": channel, "channel_type": "im",
                                    "ts": f"{time.time():.6f}", "text": "are you there?"})
            os.kill(os.getpid(), signal.SIGTERM)
    slack.on_read = stop_and_write
    runner = RecordingRunner()
    with caplog.at_level(logging.INFO, logger="wanda"):
        starting(tmp_path, monkeypatch, runner, slack)
    assert runner.calls == [] and slack.replies == []
    assert [state for _, state, _, _ in kept(store)] == ["due"] * 4
    assert sorted(ch for ch, _ in slack.reacted) == ["D1", "D2"], "only the lines sent then"
    assert store.get_meta("heard_from") == iso(t - 3600) and "wanda running (" in caplog.text
    [(down_from, down_until)] = store.down()
    assert down_from.timestamp() == t - 3600
    slack, runner = Slack(), RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack)
    first, second = store.down()
    assert first == (down_from, down_until) and second[0] >= down_until and len(runner.calls) == 2


def test_with_no_start_step_a_line_goes_on_at_the_gates_timer(tmp_path, monkeypatch, fake_time):
    """The start's gate waits READ_WAIT_S and GATE_SLACK_S more, so that its
    timer never comes before the step; with no step, it is what opens it."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    monkeypatch.setattr(main, "GATE_SLACK_S", 0.2)
    never_reads(monkeypatch)
    began = []
    run = runner.run

    async def timed(prompt, **kw):
        began.append(time.monotonic())
        return await run(prompt, **kw)
    runner.run = timed

    async def go():
        t0 = time.monotonic()
        p.read_at_start(p.watcher)
        live(p, store, dm(f"{NOW - 5:.1f}", "hello?"))
        await until(lambda: runner.calls)
        p._reading.cancel()
        p._reading = None
        return t0
    t0 = with_loop(p, go)
    assert 0.7 <= began[0] - t0 < 0.9


def test_a_start_whose_read_fails_part_way_takes_up_what_it_read_and_reads_again_a_minute_on(
        tmp_path, monkeypatch):
    """Failing at its third conversation: the first two are taken up, a read
    is needed and tried a minute on, and `heard_from` moves at the pass
    after the one that succeeds."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    slack = Slack()
    slack.listed = [im("D1"), mpim("G1"), public("C1")]
    slack.histories["D1"] = [said(t - 1800, "fan's line")]
    slack.histories["G1"] = [said(t - 1700, "mei's line", user="U2")]
    slack.histories["C1"] = [said(t - 1600, "<@UBOT> the channel's mention")]
    slack.read_errors[("history", "C1")] = CallFailed("conversations_history: network unreachable")
    seen = {}

    async def passes(self):
        while self._bg:
            await asyncio.sleep(0.01)
        seen["sessions"] = len(runner.calls)
        seen["needed"], seen["wait"] = self._read_needed, round((self._read_failing.next_try - time.monotonic()) / 60)
        del slack.read_errors[("history", "C1")]
        self._read_failing = self._read_failing._replace(next_try=0)
        self.watcher.client = SimpleNamespace(current_session=Conn("s1", time.time() - 5), is_connected=lambda: True)
        self._heard_session = "s1"
        await self.drain_mail()
        seen["during"] = store.get_meta("heard_from")
        while self._reading is not None or self._bg:
            await asyncio.sleep(0.01)
        await self.drain_mail()
        import os
        import signal
        os.kill(os.getpid(), signal.SIGTERM)
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, slack, loop=passes)
    assert seen == {"sessions": 2, "needed": True, "wait": 1, "during": iso(t - 3600)}
    assert "the channel's mention" in frames(runner)[2]
    assert datetime.fromisoformat(store.get_meta("heard_from")).timestamp() > t - 700


def test_a_message_kept_past_its_record_of_being_seen_is_still_taken_up(tmp_path, monkeypatch):
    """Kept eight days, its record of being seen pruned by the start: still
    listed, and taken up."""
    t = float(int(time.time()))
    store = store_down_an_hour(tmp_path, t)
    keep(store, dm(f"{t - 8 * 86400:.1f}", "from last week"))
    store._exec("UPDATE slack_events SET received_at=?", (iso(t - 8 * 86400),))
    runner = RecordingRunner()
    starting(tmp_path, monkeypatch, runner, Slack())
    assert store._query("SELECT * FROM slack_events") == []
    assert "from last week" in frames(runner)[0] and kept(store) == []


def test_a_store_from_before_heard_from_was_kept_reads_back_from_its_last_running_time(tmp_path):
    """A new store hears Slack from its first start; one from before reads
    back from the last time it ran, less the margin, once."""
    from wanda.main import open_store

    c = start_config(tmp_path)
    store = asyncio.run(open_store(c))
    new = store.get_meta("heard_from")
    assert abs(datetime.fromisoformat(new).timestamp() - time.time()) < 5
    assert datetime.fromisoformat(new).utcoffset() == timedelta(0) and len(new) == len(utcnow())
    store._exec("DELETE FROM meta WHERE key='heard_from'")
    store.set_meta("up_at", "2026-10-01T15:00:00+00:00")
    store = asyncio.run(open_store(c))
    assert store.get_meta("heard_from") == "2026-10-01T14:50:00+00:00"
    store.set_meta("up_at", "2026-10-02T15:00:00+00:00")
    store = asyncio.run(open_store(c))
    assert store.get_meta("heard_from") == "2026-10-01T14:50:00+00:00", "once"


# --- what a read reads --------------------------------------------------------------

class Web:
    """Slack's Web API as a read back calls it, its lists in pages of `per`,
    each page naming the next by its cursor."""

    def __init__(self, conversations, histories, per=2):
        self.conversations, self.histories, self.per = conversations, histories, per
        self.calls: list[tuple] = []

    def _page(self, items, kw):
        at = int(kw.get("cursor") or 0)
        more = at + self.per < len(items)
        return items[at:at + self.per], {"next_cursor": str(at + self.per) if more else ""}

    def users_conversations(self, **kw):
        self.calls.append(("users_conversations", kw.get("cursor")))
        page, meta = self._page(self.conversations, kw)
        return {"channels": page, "response_metadata": meta}

    def conversations_history(self, **kw):
        self.calls.append(("conversations_history", kw["channel"], kw.get("cursor"), kw["oldest"]))
        msgs = sorted((m for m in self.histories.get(kw["channel"], []) if float(m["ts"]) >= float(kw["oldest"])),
                      key=lambda m: float(m["ts"]), reverse=True)
        page, meta = self._page(msgs, kw)
        return {"messages": page, "response_metadata": meta}

    def conversations_replies(self, **kw):
        self.calls.append(("conversations_replies", kw["channel"], kw["ts"]))
        return {"messages": [], "has_more": False}


def test_a_read_pages_by_cursor_to_the_bound_and_older_pages_to_the_page_count(
        tmp_path, monkeypatch, fake_time, caplog, listed):
    """Every page reaching after the time she last heard Slack is read, and
    older ones until MAX_CONTEXT_PAGES in all; the part of the six days not
    read is logged."""
    from wanda.actions import slack as actions

    p, store, _, _ = running(tmp_path, monkeypatch, fake_time)
    monkeypatch.setattr(actions, "READ_EVERY_S", 0)
    monkeypatch.setattr(main, "MAX_CONTEXT_PAGES", 3)
    sa = actions.SlackActions(p.cfg, store)
    after = [said(NOW - 3000 + i * 100, f"line {i}") for i in range(5)]
    before = [said(NOW - 4000 - i * 1000, f"old {i}") for i in range(5)]
    sa.names_web = Web([{"id": "C9", "is_im": True}, {"id": "D1", "is_im": True}, {"id": "D2", "is_im": True}],
                       {"D1": after + before})
    p.slack = sa
    p._collecting = True
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(p.read_back())
    assert sorted(p._unhanded) == sorted(("D1", m["ts"]) for m in after)
    pages = [c for c in sa.names_web.calls if c[0] == "conversations_history" and c[1] == "D1"]
    assert [c[2] for c in pages] == [None, "2", "4", "6"], "the fourth only because the third reached after it"
    assert all(float(c[3]) == pytest.approx(NOW - 6 * 86400) for c in pages)
    assert [c for c in sa.names_web.calls if c[0] == "users_conversations"] == [
        ("users_conversations", None), ("users_conversations", "2")]
    assert "read back D1 from " in caplog.text
    assert "only: a reply under an older message there is not read back" in caplog.text
    assert sa.read_calls == 2 + 1 + 4 + 1


def reading_through(tmp_path, monkeypatch, fake_time, web, every=0.0):
    """SlackActions as a read back calls it, on `web`."""
    from wanda.actions import slack as actions

    p, store, _, _ = running(tmp_path, monkeypatch, fake_time)
    monkeypatch.setattr(actions, "READ_EVERY_S", every)
    sa = actions.SlackActions(p.cfg, store)
    sa.names_web = web
    return sa


def test_a_reads_calls_go_one_every_read_every_s(tmp_path, monkeypatch, fake_time, listed):
    web = Web([im("D1"), im("D2"), im("D3")], {}, per=1)
    sa = reading_through(tmp_path, monkeypatch, fake_time, web, every=0.1)
    made = []
    listing = web.users_conversations

    def timed(**kw):
        made.append(time.monotonic())
        return listing(**kw)
    web.users_conversations = timed
    asyncio.run(sa.conversations())
    assert len(made) == 3 and all(b - a >= 0.09 for a, b in zip(made, made[1:]))


def test_a_reads_call_that_fails_but_by_slacks_answer_is_a_call_failed(tmp_path, monkeypatch, fake_time, listed):
    """Whatever the client raises, so that the read ends on it rather than
    passing a conversation over; Slack's own answer stays SlackApiError."""
    class Failing(Web):
        def users_conversations(self, **kw):
            raise RuntimeError("the client gave up")

        def conversations_replies(self, **kw):
            raise slack_error("thread_not_found")
    sa = reading_through(tmp_path, monkeypatch, fake_time, Failing([], {}))
    with pytest.raises(CallFailed, match="^users_conversations: the client gave up$"):
        asyncio.run(sa.conversations())
    with pytest.raises(SlackApiError):
        asyncio.run(sa.read_thread("C1", "10.1", 0.0))


def test_a_list_of_conversations_cut_short_is_refused(tmp_path, monkeypatch, fake_time, listed):
    from wanda.actions import slack as actions

    monkeypatch.setattr(actions, "MAX_CONTEXT_PAGES", 2)
    sa = reading_through(tmp_path, monkeypatch, fake_time, Web([im("D1"), im("D2"), im("D3")], {}, per=1))
    with pytest.raises(RuntimeError, match="she is in more conversations than one read lists"):
        asyncio.run(sa.conversations())


def test_dms_are_read_first_and_each_conversation_with_its_threads_at_every_read(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    for channel, thread, kind in (("D1", "10.1", "dm"), ("C1", "20.1", "mention")):
        task = store.create_task(None, channel, thread, kind=kind, reply_thread=thread)
        store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(NOW - 86400), exit_code=0,
                         cost_usd=0.0, status="ok")
    slack.listed = [public("C1"), mpim("G1"), im("D1")]

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._reading is None and slack.reads)
        p._read_needed = True
        p._read_if_needed()
    with_loop(p, go)
    one = [("conversations",), ("history", "D1"), ("thread", "D1", "10.1"), ("history", "G1"), ("history", "C1"),
           ("thread", "C1", "20.1")]
    assert slack.reads == one + one


def test_a_thread_of_hers_its_history_names_is_read_once(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    thread = f"{NOW - 2 * 86400:.1f}"
    task = store.create_task(None, "C1", thread, kind="mention")
    store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(NOW - 86400), exit_code=0,
                     cost_usd=0.0, status="ok")
    slack.listed = [public("C1")]
    slack.histories["C1"] = [said(NOW - 2 * 86400, "<@UBOT> the boiler?", latest_reply=f"{NOW - 1800:.1f}")]
    slack.replies_of[("C1", thread)] = [said(NOW - 1800, "and the radiator?", thread_ts=thread)]

    async def go():
        p.slack_queue.put_nowait(heard())
    with_loop(p, go)
    assert slack.reads.count(("thread", "C1", thread)) == 1 and len(runner.calls) == 1


def test_a_read_from_more_than_six_days_back_is_cut_logged_and_alerted_once(tmp_path, monkeypatch, fake_time,
                                                                           caplog):
    """Six days are read, and only they are taken, whatever a thread of hers
    brings from before them."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    store.set_meta("heard_from", iso(NOW - 9 * 86400))
    thread = f"{NOW - 20 * 86400:.1f}"
    task = store.create_task(None, "C1", thread, kind="mention")
    store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(NOW - 3 * 86400), exit_code=0,
                     cost_usd=0.0, status="ok")
    slack.listed = [im("D1"), public("C1")]
    slack.histories["D1"] = [said(NOW - 7 * 86400, "a week ago"), said(NOW - 5 * 86400, "five days ago")]
    slack.replies_of[("C1", thread)] = [said(NOW - 6.5 * 86400, "a reply six and a half days old", thread_ts=thread)]
    slack.read_errors[("conversations",)] = CallFailed("users_conversations: network unreachable")
    cut = (f"she did not hear Slack from {at_la(NOW - 9 * 86400)}: what was sent to her before "
           f"{at_la(NOW - 6 * 86400)} was not read back")

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._read_failing is not None and p._reading is None)
        del slack.read_errors[("conversations",)]
        p._read_failing = p._read_failing._replace(next_try=0)
        await p.drain_mail()
        await until(lambda: p._reading is None)
        await p.drain_mail()
    with caplog.at_level(logging.WARNING, logger="wanda"):
        with_loop(p, go)
    assert slack.alerts == [cut] and caplog.text.count(cut) == 2
    assert slack.oldest["D1"] == pytest.approx(NOW - 6 * 86400)
    [prompt] = frames(runner)
    assert "five days ago" in prompt and "a week ago" not in prompt
    assert ("thread", "C1", thread) in slack.reads, "the thread was read, and its reply not taken"


def test_a_conversation_slack_says_is_gone_is_read_as_having_nothing(tmp_path, monkeypatch, fake_time, caplog):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1"), public("C1")]
    slack.read_errors[("history", "D1")] = slack_error("channel_not_found")
    slack.histories["C1"] = [said(NOW - 1800, "<@UBOT> still here")]

    async def go():
        p.slack_queue.put_nowait(heard())
    with caplog.at_level(logging.WARNING, logger="wanda"):
        with_loop(p, go)
    assert "could not read back" not in caplog.text and p._read_failing is None and not p._read_needed
    assert len(runner.calls) == 1 and store.get_meta("slack_passed_alert_pending") is None


@pytest.mark.parametrize("error", ["ratelimited", "invalid_auth"])
def test_slacks_trouble_or_a_token_it_no_longer_takes_ends_the_read(tmp_path, monkeypatch, fake_time, error):
    """Not one conversation's: the read ends, to be tried again, and nothing
    is passed over."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1"), public("C1")]
    slack.read_errors[("history", "C1")] = slack_error(error)

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._reading is None and slack.reads)
        await p.drain_mail()
    with_loop(p, go)
    assert p._read_needed and p._read_failing is not None and p._read_failing.error == error
    assert store.get_meta("slack_passed_alert_pending") is None and slack.alerts == []


def test_a_thread_slack_says_is_gone_is_read_as_having_nothing_and_the_next_is_read(tmp_path, monkeypatch,
                                                                                    fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    for thread in ("10.1", "20.1"):
        task = store.create_task(None, "C1", thread, kind="mention")
        store.record_run(kind="agent", task_id=task, session_id="s", started_at=iso(NOW - 86400), exit_code=0,
                         cost_usd=0.0, status="ok")
    slack.listed = [public("C1")]
    slack.read_errors[("thread", "C1", "10.1")] = slack_error("thread_not_found")
    slack.replies_of[("C1", "20.1")] = [said(NOW - 1800, "and the boiler?", thread_ts="20.1")]

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._reading is None and slack.reads)
        await p.drain_mail()
    with_loop(p, go)
    [prompt] = frames(runner)
    assert "and the boiler?" in prompt and not p._read_needed
    assert store.get_meta("slack_passed_alert_pending") is None and slack.alerts == []


def test_a_conversation_slack_refuses_or_gives_a_page_the_read_cannot_take_is_passed_over_and_read_again(
        tmp_path, monkeypatch, fake_time, caplog):
    """Each logged, counted in the alert, and read again by the next read;
    the read goes on and succeeds."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [mpim("G1"), public("C1"), im("D1")]
    slack.pages["G1"] = ["not a message"]
    slack.read_errors[("history", "C1")] = slack_error("missing_scope")
    slack.histories["D1"] = [said(NOW - 1800, "is the plumber coming?")]

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._reading is None and slack.reads)
        await p.drain_mail()
        p._read_needed = True
        p._read_if_needed()
    with caplog.at_level(logging.WARNING, logger="wanda"):
        with_loop(p, go)
    assert f"could not read back from C1 what was sent there since {at_la(NOW - 3600)}: missing_scope; some of it " \
           "may not have been read back" in caplog.text
    assert f"could not read back from G1 what was sent there since {at_la(NOW - 3600)}: " in caplog.text
    assert slack.alerts == ["2 conversation(s) could not be read back from Slack in full: 'str' object has no "
                            "attribute 'get'; some of what was sent there while she could not hear Slack may not have "
                            "been read back (the log names them)"]
    assert [r for r in slack.reads if r[0] == "history"] == [("history", "D1"), ("history", "G1"),
                                                             ("history", "C1")] * 2
    assert len(runner.calls) == 1 and p._read_failing is None


def test_what_a_conversation_passed_over_kept_first_is_taken_up(tmp_path, monkeypatch, fake_time):
    """One of a DM's threads refused, and a channel's thread call failing:
    the line each history kept is taken up, and each is counted."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1"), public("C1")]
    slack.histories["D1"] = [said(NOW - 1800, "fan's line", latest_reply=f"{NOW - 1700:.1f}")]
    slack.read_errors[("thread", "D1", f"{NOW - 1800:.1f}")] = slack_error("missing_scope")
    slack.histories["C1"] = [said(NOW - 1600, "<@UBOT> the channel's mention", latest_reply=f"{NOW - 1500:.1f}")]
    slack.read_errors[("thread", "C1", f"{NOW - 1600:.1f}")] = slack_error("invalid_cursor")

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._reading is None and slack.reads)
        await p.drain_mail()
    with_loop(p, go)
    assert ["fan's line" in f for f in frames(runner)] == [True, False]
    assert "the channel's mention" in frames(runner)[1]
    assert slack.alerts[0].startswith("2 conversation(s) could not be read back from Slack in full: missing_scope; ")


def test_a_message_the_read_cannot_classify_is_logged_once_and_passed_over(tmp_path, monkeypatch, fake_time,
                                                                            caplog):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(NOW - 1800, "odd"), said(NOW - 1700, "is the plumber coming?")]
    real = p.watcher.trigger

    def trigger(event):
        if event["text"] == "odd":
            raise KeyError("blocks")
        return real(event)
    p.watcher.trigger = trigger

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._reading is None and slack.reads)
        p._read_needed = True
        p._read_if_needed()
    with caplog.at_level(logging.WARNING, logger="wanda"):
        with_loop(p, go)
    assert caplog.text.count(f"could not read back D1:{NOW - 1800:.1f}: 'blocks'") == 1
    assert len(runner.calls) == 1 and p._read_failing is None


def test_a_read_whose_list_of_conversations_fails_ends_and_is_needed(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.read_errors[("conversations",)] = slack_error("missing_scope")

    async def go():
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._read_failing is not None and p._reading is None)
    with_loop(p, go)
    assert p._read_needed and p._read_failing.error == "missing_scope"


# --- Slack sending again, and her reaction ---------------------------------------------

def handle(p, event):
    """A line as Slack sends it live, through the watcher."""
    p.watcher._handle(SimpleNamespace(send_socket_mode_response=lambda r: None),
                      SimpleNamespace(type="events_api", envelope_id="e", payload={"event": event}))


def event(ts: float, text: str, channel="D1", user="U1", **kw) -> dict:
    return {"type": "message", "user": user, "channel": channel, "channel_type": "im", "ts": f"{ts:.1f}",
            "text": text, **kw}


def test_slack_sending_again_what_a_read_took_runs_nothing_twice(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    slack.listed = [im("D1")]
    slack.histories["D1"] = [said(NOW - 1800, "is the plumber coming?")]

    async def go():
        p.watcher.loop = asyncio.get_running_loop()
        p.slack_queue.put_nowait(heard())
        await until(lambda: runner.calls)
        handle(p, event(NOW - 1800, "is the plumber coming?"))
        await asyncio.sleep(0.05)
    with_loop(p, go)
    assert len(runner.calls) == 1


def test_a_reply_slack_sends_an_hour_late_that_no_read_found_is_taken_late(tmp_path, monkeypatch, fake_time):
    """A reply under a first message seven days old, which no read reads, as
    Delayed Events may bring it."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late=True)
    slack.listed = [im("D1")]
    first = f"{NOW - 7 * 86400:.1f}"

    async def go():
        p.watcher.loop = asyncio.get_running_loop()
        p.slack_queue.put_nowait(heard())
        await until(lambda: p._reading is None and slack.reads)
        handle(p, event(NOW - 3600, "and did it come?", thread_ts=first))
    with_loop(p, go)
    [prompt] = frames(runner)
    assert f"What fan says below was sent at {at_la(NOW - 3600)} and reaches me only now." in prompt


@pytest.mark.parametrize("ago, runs", [(3600, False), (120, False), (2, True)],
                         ids=["an hour late", "two minutes late", "seconds old"])
def test_a_message_slack_says_is_gone_at_her_late_reaction_is_withdrawn(tmp_path, monkeypatch, fake_time, ago, runs):
    """Sent again by Slack after it was deleted, or deleted unseen: at an
    add a minute or more after the message, Slack's message_not_found can
    only mean it is gone. On a message seconds old it is not known to, and
    the message is run."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late_adds=True)
    slack.react_errors[("D1", f"{NOW - ago:.1f}")] = slack_error("message_not_found")

    async def go():
        p.watcher.loop = asyncio.get_running_loop()
        handle(p, event(NOW - ago, "sent to the wrong person"))
        await asyncio.sleep(0.05)
    with_loop(p, go)
    assert bool(runner.calls) == runs and slack.replies == []
    # withdrawn, or answered
    assert kept(store) == []


def test_a_message_slack_says_is_gone_is_not_handed_to_the_session_working_there(tmp_path, monkeypatch, fake_time):
    """Slack answers the late add a moment on, as a real call does, once the
    message is past the gate and offered to the session: the offer waits
    for that answer."""
    runner = Working(answer("Noted."))
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, runner=runner, late_adds=True)
    gone = ("D1", f"{NOW - 3600:.1f}")
    slack.react_errors[gone] = (0.05, slack_error("message_not_found"))

    async def go():
        p.watcher.loop = asyncio.get_running_loop()
        handle(p, event(NOW - 5, "is the plumber coming?"))
        await until(lambda: runner.feeds)
        handle(p, event(NOW - 3600, "sent to the wrong person"))
        await until(lambda: gone in runner.feeds[0].withdrawn)
        runner.done.set()
    with_loop(p, go)
    assert len(runner.calls) == 1 and runner.added == [] and kept(store) == []


def test_a_late_add_tried_again_at_a_pass_is_paced_and_withdraws_what_slack_says_is_gone(
        tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late_adds=True)
    monkeypatch.setattr(main, "READ_EVERY_S", 0.2)
    one, two = keep(store, dm(f"{NOW - 3600:.1f}", "one")), keep(store, dm(f"{NOW - 3500:.1f}", "two"))
    slack.react_errors[one] = slack_error("internal_error")
    slack.react_errors[two] = 0.3

    async def go():
        p._react(one)
        await reactions_end(p)
        assert one in p._react_again
        slack.react_errors[one] = slack_error("message_not_found")
        p._react(two)
        await asyncio.sleep(0)
        p._retry_reactions()
        await reactions_end(p)
    asyncio.run(go())
    (_, t1), (_, t2), (_, t3) = slack.react_times
    assert [k for k, _ in slack.react_times] == [one, two, one] and t3 - t2 >= 0.19
    assert [ts for ts, *_ in kept(store)] == [two[1]] and one in slack.unreacted


def test_a_capped_row_deleted_while_she_was_down_is_withdrawn_at_its_take_up(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late_adds=True)
    key = keep(store, dm(f"{NOW - 8 * 3600:.1f}", "sent to the wrong person"))
    store.create_task(None, "D1", "conversation", kind="dm")
    store._exec("UPDATE unanswered SET state='capped'")
    slack.react_errors[key] = slack_error("message_not_found")

    async def go():
        await p.drain_mail()
    with_loop(p, go)
    assert runner.calls == [] and kept(store) == []


def test_a_capped_row_deleted_while_she_was_down_is_withdrawn_from_a_fresh_lines_turn(tmp_path, monkeypatch,
                                                                                      fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late_adds=True)
    key = keep(store, dm(f"{NOW - 8 * 3600:.1f}", "sent to the wrong person"))
    store.create_task(None, "D1", "conversation", kind="dm")
    store._exec("UPDATE unanswered SET state='capped'")
    slack.react_errors[key] = slack_error("message_not_found")

    async def go():
        live(p, store, dm(f"{NOW - 5:.1f}", "are you back?"))
    with_loop(p, go)
    [prompt] = frames(runner)
    assert "sent to the wrong person" not in prompt and "are you back?" in prompt


def test_a_message_slack_says_is_gone_runs_nothing_when_its_row_cannot_be_forgotten(tmp_path, monkeypatch,
                                                                                    fake_time, caplog):
    """The store refusing the write as Slack's answer comes: the message is
    withdrawn from its turn all the same, and its row stays due, for the
    next start's late add to meet message_not_found again."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late=True)
    gone = ("D1", f"{NOW - 1800:.1f}")
    slack.react_errors[gone] = (0.05, slack_error("message_not_found"))
    forget = Store.forget

    def full(self, keys):
        if gone in (keys := list(keys)):
            raise sqlite3.OperationalError("database or disk is full")
        return forget(self, keys)
    monkeypatch.setattr(Store, "forget", full)

    async def go():
        live(p, store, dm(gone[1], "sent to the wrong person"))
    with caplog.at_level(logging.ERROR, logger="wanda"):
        with_loop(p, go)
    assert runner.calls == [] and [(ts, state) for ts, state, *_ in kept(store)] == [(gone[1], "due")]
    assert f"could not forget {gone[1]} in D1, gone from Slack" in caplog.text


def test_a_late_add_slack_does_not_answer_holds_the_turn_its_wait_from_when_it_was_made(
        tmp_path, monkeypatch, fake_time):
    """Ten seconds from when each add was made, its time on the pace lock
    included: the second, made behind the first, holds the frame no longer."""
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late_adds=True)
    monkeypatch.setattr(main, "READ_EVERY_S", 0.2)
    monkeypatch.setattr(main, "LATE_ADD_WAIT_S", 0.3)
    for ago in (3600, 3500):
        slack.react_errors[keep(store, dm(f"{NOW - ago:.1f}", f"line {ago}"))] = 5
    [(task, keys)] = p.kept()
    began = []
    run = runner.run

    async def timed(prompt, **kw):
        began.append(time.monotonic())
        return await run(prompt, **kw)
    runner.run = timed

    async def go():
        t0 = time.monotonic()
        await p.take_up(task, keys)
        for t in list(p._reacting):
            t.cancel()
        return t0
    t0 = asyncio.run(go())
    assert 0.29 <= began[0] - t0 < 0.45
    assert "line 3600" in runner.calls[0][0] and "line 3500" in runner.calls[0][0]


def test_a_live_lines_reaction_goes_on_at_once_while_a_backlogs_are_paced(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, late_adds=True)
    monkeypatch.setattr(main, "READ_EVERY_S", 0.1)
    for ago in (3600, 3500, 3400):
        keep(store, dm(f"{NOW - ago:.1f}", f"line {ago}"))
    [(task, keys)] = p.kept()

    async def go():
        p.take_up(task, keys)
        await asyncio.sleep(0.05)
        live(p, store, dm(f"{NOW - 2:.1f}", "hello?", channel="D2", user="U2"))
    with_loop(p, go)
    assert [k[0] for k, _ in slack.react_times] == ["D1", "D2", "D1", "D1"]


def test_a_backlogs_turn_behind_another_session_frames_as_soon_as_it_holds_the_slot(tmp_path, monkeypatch,
                                                                                  fake_time):
    runner = Working(answer("Noted."))
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time, runner=runner, late_adds=True)
    for ago in (3600, 3500):
        keep(store, dm(f"{NOW - ago:.1f}", f"line {ago}", channel="D2", user="U2"))
    began = []
    run = runner.run

    async def timed(prompt, **kw):
        began.append(time.monotonic())
        return await run(prompt, **kw)
    runner.run = timed

    async def go():
        live(p, store, dm(f"{NOW - 5:.1f}", "is the plumber coming?"))
        await until(lambda: runner.feeds)
        [(task, keys)] = [(t, k) for t, k in p.kept() if t["slack_channel"] == "D2"]
        p.take_up(task, keys)
        await until(lambda: len(slack.react_times) == 3)
        await asyncio.sleep(0.1)
        released = time.monotonic()
        runner.done.set()
        return released
    released = with_loop(p, go)
    assert began[1] - released < 0.05


# --- alerts and doctor -------------------------------------------------------------

def test_the_reads_alerts_go_once_a_day_each_and_name_no_conversation(tmp_path, monkeypatch, fake_time):
    p, store, slack, runner = running(tmp_path, monkeypatch, fake_time)
    store.set_meta("heard_from", iso(NOW - 9 * 86400))
    slack.listed = [im("D1"), public("C1")]
    slack.read_errors[("history", "C1")] = slack_error("missing_scope")

    async def go():
        for _ in range(2):
            p._read_needed = True
            p._read_failing = None
            p._read_if_needed()
            await until(lambda: p._reading is None)
            await p.drain_mail()
        slack.read_errors[("conversations",)] = CallFailed("users_conversations: network unreachable")
        p._read_needed = True
        p._read_if_needed()
        await until(lambda: p._reading is None)
        fake_time.at += timedelta(minutes=10)
        for _ in range(2):
            await p.drain_mail()
            await until(lambda: p._reading is None)
    with_loop(p, go)
    assert sorted(a.split(" ")[0] for a in slack.alerts) == ["1", "could", "she"]
    assert not any(c in a for a in slack.alerts for c in ("D1", "C1"))


@pytest.mark.parametrize("by", ["heard_from", "started_at"])
@pytest.mark.parametrize("past, failing", [(11, True), (9, False)])
def test_doctor_says_when_she_has_not_heard_from_slack(tmp_path, capsys, past, failing, by):
    """Ten minutes from the later of `heard_from` plus its margin and the
    last start."""
    from wanda.config import Config
    from wanda.main import run_doctor

    c = Config(_env_file=None, data_dir=tmp_path, claude_bin="/bin/true", email_triage=False)
    store = Store(c.db_path)
    now = datetime.now(timezone.utc)
    heard_from = now - timedelta(hours=1)
    started = heard_from + main.HEARD_MARGIN + (timedelta(minutes=30) if by == "started_at" else -timedelta(hours=1))
    later = max(heard_from + main.HEARD_MARGIN, started)
    store.set_meta("heard_from", heard_from.isoformat(timespec="seconds"))
    store.set_meta("started_at", started.isoformat(timespec="seconds"))
    store.set_meta("up_at", (later + timedelta(minutes=past)).isoformat(timespec="seconds"))
    asyncio.run(run_doctor(c, smoke=False))
    out = capsys.readouterr().out
    since = vault.stamp((heard_from + main.HEARD_MARGIN).timestamp(), datetime.now(timezone.utc))
    assert (f"✗ she has not heard from Slack since {since} (the log says why)\n" in out) == failing
    assert (f"✓ heard from Slack — {since}\n" in out) != failing


def test_doctor_names_no_time_still_to_come_as_when_she_heard_from_slack(tmp_path, capsys):
    """`heard_from` set to now, as on a new store and by the step that moves
    it by hand: the time shown is now, not its margin on."""
    from wanda.config import Config
    from wanda.main import run_doctor

    c = Config(_env_file=None, data_dir=tmp_path, claude_bin="/bin/true", email_triage=False)
    store = Store(c.db_path)
    now = datetime.now(timezone.utc)
    store.set_meta("heard_from", now.isoformat(timespec="seconds"))
    store.set_meta("up_at", now.isoformat(timespec="seconds"))
    asyncio.run(run_doctor(c, smoke=False))
    out = capsys.readouterr().out
    # either side of a minute's turn while doctor ran
    shown = {vault.stamp(t.timestamp(), datetime.now(timezone.utc)) for t in (now, datetime.now(timezone.utc))}
    assert any(f"✓ heard from Slack — {stamp}\n" in out for stamp in shown)


def test_doctor_counts_a_message_read_back_from_when_it_was_kept(tmp_path, capsys):
    from wanda.config import Config
    from wanda.main import run_doctor

    c = Config(_env_file=None, data_dir=tmp_path, claude_bin="/bin/true", email_triage=False)
    store = Store(c.db_path)
    now = datetime.now(timezone.utc)
    # sent and started long past what a turn can take; kept now
    keep(store, dm(f"{(now - timedelta(hours=5)).timestamp():.1f}", "is the plumber coming?"))
    store.set_meta("started_at", (now - timedelta(hours=6)).isoformat(timespec="seconds"))
    asyncio.run(run_doctor(c, smoke=False))
    assert "✓ messages taken and not yet answered — 1\n" in capsys.readouterr().out


# --- how a late turn is framed ---------------------------------------------------------

def taken_up_at_1940(tmp_path, monkeypatch, fake_time, lines, channel_type="im"):
    """The lines kept, as (seconds before 19:40, text, sender), taken up at
    19:40 after she was stopped from 16:41 until 19:39: the head of each
    session's frame."""
    runner = RecordingRunner()
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    monkeypatch.setattr(main, "LATE_TURN_S", LATE_TURN_S)
    fake_time.at = datetime.fromtimestamp(NOW, timezone.utc)
    store.set_meta("down", json.dumps([[iso(AT + 60), iso(NOW - 60)]]))
    for ago, text, user in lines:
        keep(store, dm(f"{NOW - ago:.1f}", text, channel_type=channel_type, user=user))

    async def go():
        for task, keys in p.kept():
            p.take_up(task, keys)
        while p._bg:
            await asyncio.sleep(0.01)
    asyncio.run(go())
    return [prompt.split("Do three things")[0].split("Today is 2026-10-01.\n\n")[1] for prompt, _ in runner.calls]


OLD = 3 * 3600
LEAVING = "I'm leaving in ten minutes, anything to pick up?"


@pytest.mark.parametrize("lines, channel_type, head", [
    ([(OLD, "can you check whether the plumber replied?", "U1"), (70 * 60, LEAVING, "U1")], "im",
     "In a direct message that fan and I read. What fan says below was sent at 18:30 and reaches me only now. I was "
     "not running from 16:41 until 19:39.\n\nThe conversation so far:\n\n    16:40 fan: can you check whether the "
     f"plumber replied?\n\nfan now says:\n\n    {LEAVING}\n\n"),
    ([(OLD, "is the plumber coming tomorrow?", "U2"), (70 * 60, LEAVING, "U1")], "mpim",
     "In a group direct message that fan, mei and I read. Everyone in it sees what I say there. I could not find out "
     "who else is in it. What fan says below was sent at 18:30 and reaches me only now. I was not running from 16:41 "
     "until 19:39.\n\nThe conversation so far:\n\n    16:40 mei: is the plumber coming tomorrow?\n\nfan now says, "
     f"after mei:\n\n    {LEAVING}\n\n"),
    ([(OLD, "can you check whether the plumber replied?", "U1"), (5, "are you back?", "U1")], "im",
     "In a direct message that fan and I read. What fan says below was sent at 16:40 and reaches me only now. I was "
     "not running from 16:41 until 19:39.\n\nThe conversation so far:\n\n    16:40 fan: can you check whether the "
     "plumber replied?\n\nfan now says:\n\n    are you back?\n\n"),
    ([(OLD, "is the plumber coming tomorrow?", "U2"), (5, "are you back?", "U1")], "mpim",
     "In a group direct message that fan, mei and I read. Everyone in it sees what I say there. I could not find out "
     "who else is in it. What mei says below was sent at 16:40 and reaches me only now. I was not running from 16:41 "
     "until 19:39.\n\nThe conversation so far:\n\n    16:40 mei: is the plumber coming tomorrow?\n\nfan now says, "
     "after mei:\n\n    are you back?\n\n"),
    ([(300, "is the boiler fixed?", "U1"), (120, "and the radiator?", "U1")], "im",
     "In a direct message that fan and I read.\n\nThe conversation so far:\n\n    19:35 fan: is the boiler fixed?\n\n"
     "fan now says:\n\n    and the radiator?\n\n"),
], ids=["a backlog", "a backlog in a group DM", "a missed line and a fresh one",
        "a missed line and a fresh one in a group DM", "under ten minutes"])
def test_a_turn_is_late_by_its_first_message_and_dates_the_one_it_answers_when_that_one_is_late(
        tmp_path, monkeypatch, fake_time, lines, channel_type, head):
    """LATE_TURN names the message the session answers when it is late, and
    the first when it is new, the conversation so far showing the other
    with its time; a turn whose first message waited under ten minutes is
    framed as now."""
    assert taken_up_at_1940(tmp_path, monkeypatch, fake_time, lines, channel_type) == [head]


def test_a_fresh_line_joining_a_row_the_cap_kept_is_framed_late_by_that_row(tmp_path, monkeypatch, fake_time):
    runner = RecordingRunner()
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    monkeypatch.setattr(main, "LATE_TURN_S", LATE_TURN_S)
    fake_time.at = datetime.fromtimestamp(NOW, timezone.utc)
    keep(store, dm(f"{AT:.1f}", "can you check whether the plumber replied?"))
    store.create_task(None, "D1", "conversation", kind="dm")
    store._exec("UPDATE unanswered SET state='capped'")
    asyncio.run(p.handle_slack(dm(f"{NOW - 5:.1f}", "are you back?")))
    [(prompt, _)] = runner.calls
    assert ("In a direct message that fan and I read. What fan says below was sent at 16:40 and reaches me only now."
            "\n\nThe conversation so far:\n\n    16:40 fan: can you check whether the plumber replied?\n\nfan now "
            "says:\n\n    are you back?\n\n") in prompt
