"""Processor behaviors the adversarial review found broken: execution-time
trash caps, time-gated retries, and budget saturation vs. a tripped breaker."""

import asyncio
import contextlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError

from wanda.config import Config
from wanda.events import Event
from wanda import clock, main, vault
from wanda.household import NAMES_EVERY_S, Household
from wanda.main import ANCHOR, MAX_APPLY_ATTEMPTS, RETRY_BASE_S, Additions, Processor
from wanda.runner import RunResult, RunnerService
from wanda.store import Settled, Store, utcnow
from wanda.triage import Verdict


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("WANDA_"):
            monkeypatch.delenv(key, raising=False)


class FakeSlack:
    def __init__(self, fail=False):
        self.fail = fail
        self.tasks, self.digests, self.alerts, self.replies = [], [], [], []
        self.channels, self.threads = [], []
        # each call putting her reaction on a message and taking it off, by
        # (channel, ts), in order
        self.reacted, self.unreacted = [], []

    async def post_task(self, row, v):
        if self.fail:
            raise RuntimeError("slack 503")
        self.tasks.append(row["dedupe_key"])
        return f"ts-{row['id']}"

    async def find_task_post(self, key):
        if self.fail:
            raise RuntimeError("slack 503")
        return None

    async def digest_entry(self, row, v, action, note):
        if self.fail:
            raise RuntimeError("slack 503")
        self.digests.append((row["dedupe_key"], action, note))

    async def alert(self, text):
        self.alerts.append(text)

    async def reply(self, thread_ts, text, channel=None):
        self.replies.append(text)
        self.channels.append(channel)
        self.threads.append(thread_ts)

    async def react(self, channel, ts):
        self.reacted.append((channel, ts))

    async def unreact(self, channel, ts):
        self.unreacted.append((channel, ts))


def cfg(**kw) -> Config:
    return Config(_env_file=None, email_triage_slack_channel_id="C1", **kw)


@pytest.fixture
def fake_time(monkeypatch):
    """The time as wanda.main and wanda.store read it, standing at `at`,
    08:00 on Thursday 1 October 2026 in Los Angeles, until a test moves it."""
    class Clock(datetime):
        at = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.at.astimezone(tz) if tz else cls.at.astimezone().replace(tzinfo=None)
    monkeypatch.setattr("wanda.main.datetime", Clock)
    monkeypatch.setattr("wanda.store.datetime", Clock)
    return Clock


def make(tmp_path, slack=None, **kw):
    store = Store(tmp_path / "p.db")
    # a pass of the mail loop looks after the data directory's snapshots,
    # from a local hour in the household's zone
    c = cfg(**({"data_dir": tmp_path, "tz": "America/Los_Angeles"} | kw))
    p = Processor(c, store, asyncio.Queue(), slack or FakeSlack(), RunnerService("/bin/true"))
    return p, store


def ingest_triaged(store, key, action, uid=1, confidence=0.95):
    store.ingest_message(dedupe_key=key, message_id=f"<{key}>", folder="INBOX", uidvalidity=1,
                         uid=uid, from_addr="spam@x.example", subject="s", date_hdr="d", snippet="b")
    v = Verdict(id="e1", action="trash" if action in ("trash", "shadow_trash") else "attention",
                summary="s", reason="r", urgency="low", confidence=confidence)
    store.set_triaged(key, v.model_dump() | {"guard_note": ""}, action)
    return store.get_message_by_key(key)


def test_trash_cap_is_rechecked_at_move_time(tmp_path, monkeypatch):
    """The whole batch is guarded in one pass before any move happens, so the
    cap only binds if it is re-checked here."""
    slack = FakeSlack()
    p, store = make(tmp_path, slack, enforcement="live", trash_cap_hourly=2)
    moves = []
    monkeypatch.setattr("wanda.main.move_to_trash", lambda cfg, uid, uidv: moves.append(uid) or "moved")

    for i in range(5):
        ingest_triaged(store, f"k{i}", "trash", uid=i + 1)
    asyncio.run(p.apply_pending())

    assert len(moves) == 2, f"cap of 2 should bind, got {len(moves)} moves"
    # The rest are deferred, not retired: a rate cap means "not yet".
    assert store.count_by_status("deferred") == 3
    assert store.count_by_status("done") == 2
    assert len(slack.alerts) == 1 and "cap" in slack.alerts[0]


def test_deferred_rows_move_once_the_window_reopens(tmp_path, monkeypatch):
    slack = FakeSlack()
    p, store = make(tmp_path, slack, enforcement="live", trash_cap_hourly=2)
    moves = []
    monkeypatch.setattr("wanda.main.move_to_trash", lambda cfg, uid, uidv: moves.append(uid) or "moved")
    for i in range(4):
        ingest_triaged(store, f"k{i}", "trash", uid=i + 1)
    asyncio.run(p.apply_pending())
    assert len(moves) == 2 and store.count_by_status("deferred") == 2

    # Age out both the defer timer and the moves that consumed the cap.
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    store._exec("UPDATE messages SET deferred_until=? WHERE status='deferred'", (past,))
    store._exec("UPDATE messages SET moved_at=? WHERE moved_at IS NOT NULL", (past,))
    asyncio.run(p.apply_pending())
    assert len(moves) == 4, "deferred spam should be trashed once the cap window reopens"


def test_completed_move_is_never_relabelled(tmp_path, monkeypatch):
    """A digest failure used to re-guard an already-trashed message and report
    it to the owner as 'WOULD trash'."""
    slack = FakeSlack()
    p, store = make(tmp_path, slack, enforcement="live", trash_cap_hourly=1)
    monkeypatch.setattr("wanda.main.move_to_trash", lambda cfg, uid, uidv: "moved")
    ingest_triaged(store, "k1", "trash")
    slack.fail = True
    asyncio.run(p.apply_pending())          # moves, then the digest post fails
    row = store.get_message_by_key("k1")
    assert row["moved_at"] and row["status"] == "acting"

    slack.fail = False
    store._exec("UPDATE messages SET updated_at=? WHERE dedupe_key='k1'",
                ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"),))
    asyncio.run(p.apply_pending())
    assert [d[1] for d in slack.digests] == ["trash"], "an executed move must stay labelled trash"
    assert store.get_message_by_key("k1")["applied_action"] == "trash"


def test_allowlist_added_after_triage_stops_the_move(tmp_path, monkeypatch):
    """The full guard chain re-runs at move time, not just enforcement+caps."""
    slack = FakeSlack()
    p, store = make(tmp_path, slack, enforcement="live", never_trash=["x.example"])
    monkeypatch.setattr("wanda.main.move_to_trash", lambda *a: pytest.fail("allowlisted sender must not move"))
    ingest_triaged(store, "k1", "trash")  # from spam@x.example
    asyncio.run(p.apply_pending())
    assert slack.digests == [("k1", "ignore", "never-trash allowlist")]


def test_flipping_back_to_shadow_stops_queued_moves(tmp_path, monkeypatch):
    slack = FakeSlack()
    p, store = make(tmp_path, slack, enforcement="shadow")
    monkeypatch.setattr("wanda.main.move_to_trash", lambda *a: pytest.fail("must not move in shadow mode"))
    ingest_triaged(store, "k1", "trash")
    asyncio.run(p.apply_pending())
    assert slack.digests == [("k1", "shadow_trash", "shadow mode")]
    # Shadow mode is not a cap event; alerting here would burn the day's alert.
    assert slack.alerts == []


def test_one_pass_burns_one_attempt(tmp_path):
    """A failing row used to be retried again inside the same pass, burning the
    whole attempt budget during a brief outage."""
    p, store = make(tmp_path, FakeSlack(fail=True))
    ingest_triaged(store, "k1", "attention")
    asyncio.run(p.apply_pending())
    row = store.get_message_by_key("k1")
    assert row["attempts"] == 1 and row["status"] == "acting"


def test_retry_is_time_gated(tmp_path):
    p, store = make(tmp_path, FakeSlack(fail=True))
    ingest_triaged(store, "k1", "attention")
    for _ in range(5):
        asyncio.run(p.apply_pending())
    row = store.get_message_by_key("k1")
    # Backoff means repeated immediate passes cannot exhaust the budget.
    assert row["attempts"] == 1 and row["status"] == "acting"


def test_retry_due_respects_backoff():
    class R(dict):
        def __getitem__(self, k):
            return self.get(k)

    now = datetime.now(timezone.utc)
    assert Processor._retry_due(R(attempts=0, updated_at=now.isoformat()))
    assert not Processor._retry_due(R(attempts=3, updated_at=now.isoformat()))
    old = (now - timedelta(seconds=RETRY_BASE_S * 8)).isoformat()
    assert Processor._retry_due(R(attempts=3, updated_at=old))
    assert Processor._retry_due(R(attempts=2, updated_at="not-a-date"))


def test_row_retires_to_error_and_can_be_requeued(tmp_path):
    p, store = make(tmp_path, FakeSlack(fail=True))
    ingest_triaged(store, "k1", "attention")
    for _ in range(MAX_APPLY_ATTEMPTS + 2):
        store._exec("UPDATE messages SET updated_at=? WHERE dedupe_key='k1'",
                    ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds"),))
        asyncio.run(p.apply_pending())
    row = store.get_message_by_key("k1")
    assert row["status"] == "error" and row["attempts"] >= MAX_APPLY_ATTEMPTS
    assert store.requeue_errors() == 1
    assert store.get_message_by_key("k1")["status"] == "acting"


def test_malformed_verdict_does_not_wedge_the_pipeline(tmp_path):
    """One unparseable row used to raise out of every drain, starving the rest."""
    slack = FakeSlack()
    p, store = make(tmp_path, slack)
    store.ingest_message(dedupe_key="bad", message_id="<b>", folder="INBOX", uidvalidity=1, uid=1,
                         from_addr="a@b.c", subject="s", date_hdr="d", snippet="b")
    store._exec("UPDATE messages SET status='triaged', verdict_json=?, applied_action='attention' "
                "WHERE dedupe_key='bad'", (json.dumps({"action": "attention"}),))  # no id/summary/...
    ingest_triaged(store, "good", "attention", uid=2)

    asyncio.run(p.apply_pending())

    assert "good" in slack.tasks, "healthy row must still be delivered"
    assert store.get_message_by_key("bad")["attempts"] == 1


def test_budget_distinguishes_busy_from_breaker(tmp_path):
    slack = FakeSlack()
    p, store = make(tmp_path, slack, daily_cost_cap_usd=5.0, agent_expected_usd=0.4)

    assert asyncio.run(p.check_budget(0.4)) == "ok"

    # In-flight reservations alone must not trip the breaker or burn its alert.
    with p._reserve(2.0), p._reserve(2.0):
        assert asyncio.run(p.check_budget(2.0)) == "busy"
    assert slack.alerts == []

    store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(),
                     exit_code=0, cost_usd=6.0, status="ok")
    assert asyncio.run(p.check_budget(0.4)) == "breaker"
    assert len(slack.alerts) == 1


def test_no_silent_busy_dead_band(tmp_path):
    """Recorded spend that leaves no room is the breaker (alerted), not 'busy'
    — reporting busy stalled triage silently until UTC midnight."""
    slack = FakeSlack()
    p, store = make(tmp_path, slack, daily_cost_cap_usd=5.0)
    store.record_run(kind="triage", task_id=None, session_id=None, started_at=utcnow(),
                     exit_code=0, cost_usd=4.90, status="ok")
    assert p._inflight_runs == 0
    assert asyncio.run(p.check_budget(0.40)) == "breaker"
    assert len(slack.alerts) == 1


def test_undelivered_agent_answer_is_replayed(tmp_path):
    slack = FakeSlack()
    p, store = make(tmp_path, slack)
    store.ingest_message(dedupe_key="k1", message_id="<k1>", folder="INBOX", uidvalidity=1, uid=1,
                         from_addr="a@b.c", subject="s", date_hdr="d", snippet="b")
    pk = store.get_message_by_key("k1")["id"]
    task_id = store.create_task(pk, "C1", "ts-1")
    store.record_run(kind="agent", task_id=task_id, session_id="s1", started_at=utcnow(),
                     exit_code=0, cost_usd=0.4, status="ok",
                     result_text="the invoice is due Friday", notified=0)

    asyncio.run(p.deliver_pending())
    assert slack.replies == ["the invoice is due Friday"]
    asyncio.run(p.deliver_pending())
    assert len(slack.replies) == 1, "a delivered answer must not be re-posted"


def test_undeliverable_answer_stays_pending(tmp_path):
    p, store = make(tmp_path, FakeSlack())
    store.ingest_message(dedupe_key="k1", message_id="<k1>", folder="INBOX", uidvalidity=1, uid=1,
                         from_addr="a@b.c", subject="s", date_hdr="d", snippet="b")
    pk = store.get_message_by_key("k1")["id"]
    tid = store.create_task(pk, "C1", "ts-1")
    store.record_run(kind="agent", task_id=tid, session_id="s", started_at=utcnow(), exit_code=0,
                     cost_usd=0.4, status="ok", result_text="answer", notified=0)

    class Boom(FakeSlack):
        async def reply(self, thread_ts, text, channel=None):
            raise RuntimeError("slack down")

    p.slack = Boom()
    asyncio.run(p.deliver_pending())
    assert len(store.pending_deliveries()) == 1, "must stay pending until it lands"


def test_cap_at_triage_time_defers_rather_than_retiring(tmp_path, monkeypatch):
    """Rows triaged while the cap is already saturated used to be retired to
    shadow_trash permanently, so identical spam got opposite fates depending on
    which side of a batch boundary it landed on."""
    from wanda.triage import evaluate_guards

    slack = FakeSlack()
    p, store = make(tmp_path, slack, enforcement="live", trash_cap_hourly=2)
    moves = []
    monkeypatch.setattr("wanda.main.move_to_trash", lambda cfg, uid, uidv: moves.append(uid) or "moved")

    for i in range(2):
        ingest_triaged(store, f"a{i}", "trash", uid=i + 1)
    asyncio.run(p.apply_pending())
    assert len(moves) == 2  # cap now saturated

    # A later batch is guarded with the cap already consumed.
    v = Verdict(id="e1", action="trash", summary="s", reason="r", urgency="low", confidence=0.99)
    gd = evaluate_guards(v, "spam@x.example", p.cfg, store, check_caps=False)
    assert gd.applied_action == "trash", "triage must not decide caps"
    ingest_triaged(store, "b0", "trash", uid=99)
    asyncio.run(p.apply_pending())
    assert store.get_message_by_key("b0")["status"] == "deferred"

    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    store._exec("UPDATE messages SET deferred_until=? WHERE status='deferred'", (past,))
    store._exec("UPDATE messages SET moved_at=? WHERE moved_at IS NOT NULL", (past,))
    asyncio.run(p.apply_pending())
    assert 99 in moves, "deferred spam must be trashed when the window reopens"


def test_deliver_pending_waits_on_a_delivery_in_flight(tmp_path):
    p, store = make(tmp_path, FakeSlack())
    store.ingest_message(dedupe_key="k1", message_id="<k1>", folder="INBOX", uidvalidity=1, uid=1,
                         from_addr="a@b.c", subject="s", date_hdr="d", snippet="b")
    pk = store.get_message_by_key("k1")["id"]
    tid = store.create_task(pk, "C1", "ts-1")
    run_id = store.record_run(kind="agent", task_id=tid, session_id="s", started_at=utcnow(),
                              exit_code=0, cost_usd=0.4, status="ok", result_text="answer", notified=0)

    async def go():
        posting = p._delivering[run_id] = asyncio.Event()
        delivery = asyncio.create_task(p.deliver_pending())
        await asyncio.sleep(0.05)
        assert not delivery.done(), "waits for the post another task is making"
        store.mark_run_notified(run_id)  # as that post does once Slack takes it
        del p._delivering[run_id]
        posting.set()
        await delivery
    asyncio.run(go())
    assert p.slack.replies == [], "must not post an answer another task delivered"


def test_alert_is_not_suppressed_by_a_failed_post(tmp_path):
    """The suppression key used to be stamped before the post, so an outage
    silenced the breaker for the rest of the day."""
    class Flaky(FakeSlack):
        def __init__(self):
            super().__init__()
            self.up = False

        async def alert(self, text):
            if not self.up:
                raise RuntimeError("slack down")
            self.alerts.append(text)

    slack = Flaky()
    p, store = make(tmp_path, slack, daily_cost_cap_usd=1.0)
    store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(),
                     exit_code=0, cost_usd=2.0, status="ok")
    assert asyncio.run(p.check_budget()) == "breaker"
    assert slack.alerts == []
    slack.up = True
    asyncio.run(p._flush_alert("breaker"))
    assert len(slack.alerts) == 1
    asyncio.run(p._flush_alert("breaker"))
    assert len(slack.alerts) == 1, "delivered alert must not repeat"


def test_answered_here_requires_the_triggering_conversation(tmp_path):
    """The post marker records where the agent posted. A post elsewhere (e.g.
    'put this in #eng') must not suppress the reply the asker is owed."""
    p, _ = make(tmp_path)
    marker = tmp_path / "m.posted"

    marker.write_text("C_ASKED\t99.1")
    assert p._answered_here(marker, "C_ASKED", "99.1") is True
    assert p._answered_here(marker, "C_OTHER", "99.1") is False   # wrong channel
    assert p._answered_here(marker, "C_ASKED", "77.7") is False   # wrong thread

    marker.write_text("D5\t")                                     # untreaded DM reply
    assert p._answered_here(marker, "D5", None) is True

    marker.unlink()
    assert p._answered_here(marker, "C_ASKED", "99.1") is False   # never posted


def test_pending_delivery_goes_to_its_own_conversation(tmp_path):
    """A DM answer that failed to post must not be replayed into the triage
    channel."""
    slack = FakeSlack()
    p, store = make(tmp_path, slack)
    tid = store.create_task(None, "D_PRIVATE", "conversation", kind="dm")
    store.record_run(kind="agent", task_id=tid, session_id="s", started_at=utcnow(),
                     exit_code=0, cost_usd=0.4, status="ok", result_text="private answer",
                     notified=0)
    asyncio.run(p.deliver_pending())
    assert slack.channels == ["D_PRIVATE"], "must post back to the DM, not the triage channel"


def test_reservation_released_on_exception(tmp_path):
    p, _ = make(tmp_path)
    with pytest.raises(ValueError):
        with p._reserve(2.0):
            raise ValueError("boom")
    assert p._inflight_usd == 0.0 and p._inflight_runs == 0


def test_marker_matches_any_post_to_the_asker(tmp_path):
    """Last-write-wins made suppression depend on the order the agent posted
    in: answering the asker then copying to #eng duplicated the answer."""
    p, _ = make(tmp_path)
    m = tmp_path / "m.posted"

    m.write_text("C_ASKED\t99.1\nC_ENG\t\n")          # answered, then copied elsewhere
    assert p._answered_here(m, "C_ASKED", "99.1") is True
    m.write_text("C_ENG\t\nC_ASKED\t99.1\n")          # other order, same outcome
    assert p._answered_here(m, "C_ASKED", "99.1") is True

    m.write_text("C_ASKED\t\n")                        # --no-thread in the right channel counts
    assert p._answered_here(m, "C_ASKED", "99.1") is True

    m.write_text("C_ENG\t\n")                          # only posted elsewhere
    assert p._answered_here(m, "C_ASKED", "99.1") is False


def test_dm_recovery_posts_untreaded(tmp_path):
    """tasks.thread_ts holds a sentinel for DMs; sending it as a Slack thread
    id made the delivery fail forever."""
    slack = FakeSlack()
    p, store = make(tmp_path, slack)
    tid = store.create_task(None, "D5", "conversation", kind="dm", reply_thread=None)
    store.record_run(kind="agent", task_id=tid, session_id="s", started_at=utcnow(),
                     exit_code=0, cost_usd=0.4, status="ok", result_text="answer", notified=0)
    asyncio.run(p.deliver_pending())
    assert slack.threads == [None], "a DM answer must post untreaded, not to 'conversation'"
    assert slack.channels == ["D5"]


def test_conversation_kinds_open_a_task():
    """The watcher mints these kinds; every one must open a task, or the
    trigger dead-ends in a false 'still starting up' reply."""
    from wanda.main import CONVERSATION_KINDS
    from wanda.watchers.slack_watcher import DM_TASK_KEY  # noqa: F401
    assert set(CONVERSATION_KINDS) == {"mention", "mention_guest", "dm"}


def test_answered_run_that_then_timed_out_is_not_double_posted(tmp_path):
    """A session that posted its answer and was then killed by the timeout has
    still answered; posting 'my run failed' under it is noise."""
    p, _ = make(tmp_path)
    marker = tmp_path / "m.posted"
    marker.write_text("C9\t100.1\n")
    assert p._answered_here(marker, "C9", "100.1") is True


def test_legacy_tasks_get_reply_thread_backfilled(tmp_path):
    """A bare ADD COLUMN left NULL, which posts recovery answers at channel top
    level instead of in the task's thread."""
    import sqlite3
    db = tmp_path / "legacy.db"
    c = sqlite3.connect(db)
    c.executescript("""
      CREATE TABLE messages(id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
        folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE TABLE tasks(id INTEGER PRIMARY KEY, message_pk INTEGER REFERENCES messages(id),
        slack_channel TEXT NOT NULL, thread_ts TEXT NOT NULL, claude_session_id TEXT,
        status TEXT NOT NULL DEFAULT 'open', kind TEXT NOT NULL DEFAULT 'email',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(slack_channel, thread_ts));
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      INSERT INTO tasks(slack_channel, thread_ts, kind, created_at, updated_at)
        VALUES ('C1','111.1','email','t','t'), ('D5','conversation','dm','t','t');
    """)
    c.commit(); c.close()

    s = Store(db)
    assert s.get_task_by_thread("C1", "111.1")["reply_thread"] == "111.1"
    assert s.get_task_by_thread("D5", "conversation")["reply_thread"] is None, \
        "a DM key is a sentinel, not a thread id"
    s.close()


def test_answered_then_failed_surfaces_the_failure(tmp_path):
    """A run that posted something and then died must not be recorded as
    delivered — the post may be a holding note or half an answer."""
    p, _ = make(tmp_path)
    marker = tmp_path / "m.posted"
    marker.write_text("C9\t100.1\n")
    assert p._answered_here(marker, "C9", "100.1") is True   # it did post
    # The harness decides using rr.ok as well; see _run_task_reply.


def test_delivery_gives_up_and_stops_blocking(tmp_path, fake_time):
    """An answer for a channel wanda was removed from used to retry forever,
    blocking every later delivery behind it."""
    class Boom(FakeSlack):
        async def reply(self, thread_ts, text, channel=None):
            raise RuntimeError("not_in_channel")

    p, store = make(tmp_path, Boom())
    tid = store.create_task(None, "C_GONE", "1.1", kind="mention")
    store.record_run(kind="agent", task_id=tid, session_id="s", started_at=utcnow(),
                     exit_code=0, cost_usd=0.4, status="ok", result_text="answer", notified=0)
    asyncio.run(p.deliver_pending())
    fake_time.at += timedelta(minutes=122)
    asyncio.run(p.deliver_pending())
    assert store.pending_deliveries() == [], "must stop retrying and free the queue"
    assert store.get_meta("abandoned_alert_pending") == "1", "and tell the owner"
    assert [sorted(g) for g in json.loads(store.get_meta("given_up_runs"))] == [["at", "how", "id"]]


def test_an_answer_given_up_on_is_alerted_and_none_is_dropped(tmp_path, fake_time):
    """Delivery gives up only while Slack refuses posts, so the alert waits
    for Slack too; later give-ups join it, and one after the day's alert is
    named the next day. It names each run and when, never where or what."""
    class Down(FakeSlack):
        up = False

        async def reply(self, thread_ts, text, channel=None):
            raise RuntimeError("slack down")

        async def alert(self, text):
            if not self.up:
                raise RuntimeError("slack down")
            await super().alert(text)

    slack = Down()
    p, store = make(tmp_path, slack, email_triage=False)

    def owe(channel, text):
        tid = store.create_task(None, channel, f"{channel}.1", kind="dm")
        run = store.record_run(kind="agent", task_id=tid, session_id="s", started_at=utcnow(),
                               exit_code=0, cost_usd=0.4, status="ok", result_text=text, notified=0)
        asyncio.run(p.drain_mail())
        fake_time.at += timedelta(minutes=122)
        asyncio.run(p.drain_mail())
        return run

    r1 = owe("D1", "the plumber is at 5")
    r2 = owe("D2", "mei's present is in the shed")
    assert slack.alerts == [] and len(json.loads(store.get_meta("given_up_runs"))) == 2
    slack.up = True
    asyncio.run(p.drain_mail())
    assert len(slack.alerts) == 1 and "2 answer(s)" in slack.alerts[0]
    assert f"run {r1}, from " in slack.alerts[0] and f"run {r2}, from " in slack.alerts[0]
    assert "D1" not in slack.alerts[0] and "D2" not in slack.alerts[0]
    assert "plumber" not in slack.alerts[0] and "present" not in slack.alerts[0]
    slack.up = False
    r3 = owe("D3", "the third")
    slack.up = True
    asyncio.run(p.drain_mail())
    assert len(slack.alerts) == 1, "at most one a day"
    store.set_meta("given_up_alert_date", "2026-01-01")  # the next day
    asyncio.run(p.drain_mail())
    assert len(slack.alerts) == 2 and "1 answer(s)" in slack.alerts[1] and f"run {r3}, from " in slack.alerts[1]
    assert json.loads(store.get_meta("given_up_runs")) == []


def owed(store, text, channel="D1", kind="agent", session="s"):
    """A run recorded in a DM, owed to it."""
    tid = store.create_task(None, channel, "conversation", kind="dm")
    return store.record_run(kind=kind, task_id=tid, session_id=session, started_at=utcnow(), exit_code=0,
                            cost_usd=0.4, status="ok", result_text=text, notified=0)


def test_an_answer_slack_refuses_is_tried_at_every_pass_for_two_hours_of_her_running(tmp_path, fake_time, caplog):
    """Each pass, a minute apart, tries it, what was recorded after it there
    waiting behind it, and no try counts; the first that fails past 121
    minutes gives it up, and what waited is posted. The first refusal and
    the give-up are logged, once each."""
    import logging

    class Blocked(FakeSlack):
        """A Slack that takes no post about the plumber."""
        refused = []

        async def reply(self, thread_ts, text, channel=None):
            if "plumber" in text:
                self.refused.append(text)
                raise RuntimeError("ratelimited")
            await super().reply(thread_ts, text, channel)

    slack = Blocked()
    p, store = make(tmp_path, slack, email_triage=False)
    first, _ = owed(store, "The plumber is at 5."), owed(store, "And it's paid.")
    with caplog.at_level(logging.WARNING, logger="wanda"):
        for _ in range(122):  # the last at 121 minutes
            asyncio.run(p.deliver_pending())
            fake_time.at += timedelta(minutes=1)
        assert len(slack.refused) == 122 and slack.replies == [] and len(store.pending_deliveries()) == 2
        asyncio.run(p.drain_mail())
    assert len(slack.refused) == 123 and store.pending_deliveries() == []
    assert slack.replies == ["_(I wrote this at 08:00; it couldn't be sent until now.)_\nAnd it's paid."]
    assert slack.alerts == [f"1 answer(s) could not be posted and were given up: run {first}, from 08:00, after "
                            "two hours. `wanda doctor` lists where each was due (README, State)."]
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith(("could not post", "gave up"))]
    assert [s.split(":")[0] for s in said] == [f"could not post run {first} in D1 yet",
                                               f"gave up posting run {first} in D1 after two hours"]


def test_each_pass_marks_her_as_running(tmp_path, fake_time):
    """A start after a crash, with no stop to mark when she last ran, counts
    her down from the last pass, not from the stop before."""
    p, store = make(tmp_path, email_triage=False)
    store.set_meta("up_at", "2026-09-28T15:00:00+00:00")  # the last stop
    asyncio.run(p.drain_mail())
    fake_time.at += timedelta(minutes=5)
    store.came_up(utcnow())  # the start after a crash
    assert json.loads(store.get_meta("down")) == [["2026-10-01T15:00:00+00:00", "2026-10-01T15:05:00+00:00"]]


def test_an_answer_owed_across_a_stop_is_tried_for_two_hours_of_her_running(tmp_path, fake_time):
    """The time she was stopped is not counted against it: refused at the
    first pass after a three-hour stop, it is tried again."""
    p, store = make(tmp_path, Refusing())
    owed(store, "The plumber is at 5.")
    asyncio.run(p.deliver_pending())
    fake_time.at += timedelta(minutes=1)
    store.set_meta("up_at", utcnow())  # the stop's end
    fake_time.at += timedelta(hours=3)
    store.came_up(utcnow())  # the start
    asyncio.run(p.deliver_pending())
    assert len(store.pending_deliveries()) == 1, "refused after three hours, and kept"
    fake_time.at += timedelta(minutes=119)
    asyncio.run(p.deliver_pending())
    assert len(store.pending_deliveries()) == 1, "two hours of her running"
    fake_time.at += timedelta(minutes=2)
    asyncio.run(p.deliver_pending())
    assert store.pending_deliveries() == []


@pytest.mark.parametrize("error, given_up", [("channel_not_found", True), ("restricted_action", True),
                                             ("internal_error", False)])
def test_what_slack_refuses_there_for_good_is_given_up_at_once(tmp_path, fake_time, error, given_up):
    class Says(FakeSlack):
        async def reply(self, thread_ts, text, channel=None):
            raise SlackApiError("The request to the Slack API failed.", {"ok": False, "error": error})

    p, store = make(tmp_path, Says(), email_triage=False)
    run = owed(store, "The plumber is at 5.")
    asyncio.run(p.drain_mail())
    assert len(store.pending_deliveries()) == (0 if given_up else 1)
    assert p.slack.alerts == ([f"1 answer(s) could not be posted and were given up: run {run}, from 08:00, at once "
                               f"({error}). `wanda doctor` lists where each was due (README, State)."]
                              if given_up else [])


@pytest.mark.parametrize("later, kind, mark", [
    (timedelta(minutes=3), "agent", "_(I wrote this at 08:00; it couldn't be sent until now.)_\n"),
    (timedelta(days=1, minutes=3), "agent", "_(I wrote this on Thursday at 08:00; it couldn't be sent until now.)_\n"),
    (timedelta(minutes=1), "agent", ""),
    (timedelta(minutes=3), "note", ""),
], ids=["at +3", "the next day", "at +1", "a note"])
def test_an_answer_posted_late_says_when_she_wrote_it(tmp_path, fake_time, later, kind, mark):
    p, store = make(tmp_path)
    owed(store, "The plumber is at 5.", kind=kind)
    fake_time.at += later
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == [mark + "The plumber is at 5."]


def test_a_note_after_an_answer_given_up_on_goes_with_it(tmp_path):
    """Her note follows the answer it was recorded with, under its session,
    and means nothing without it; another session's answer there is tried."""
    class Gone(FakeSlack):
        async def reply(self, thread_ts, text, channel=None):
            self.replies.append(text)
            raise SlackApiError("The request to the Slack API failed.", {"ok": False, "error": "is_archived"})

    p, store = make(tmp_path, Gone())
    tid = store.create_task(None, "D1", "conversation", kind="dm")
    answered, note = store.record_run_and_note(
        main.FAILED_REST, kind="agent", task_id=tid, session_id="s1", started_at=utcnow(), exit_code=0,
        cost_usd=0.4, status="ok", error="error_during_execution", result_text="The plumber is at 5.", notified=0)
    later = owed(store, "Yes.", session="s2")
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == ["The plumber is at 5.", "Yes."] and store.pending_deliveries() == []
    assert [g["id"] for g in json.loads(store.get_meta("given_up_runs"))] == [answered, later]
    assert store.run(note)["notified"] == 1


def test_the_snapshots_are_looked_after_once_a_day(tmp_path, monkeypatch):
    """git's housekeeping, which the snapshots leave out, runs from the mail
    loop on its first pass of a local day from the quiet hour on, and what
    goes wrong with it is alerted."""
    from wanda import main

    slack = FakeSlack()
    p, store = make(tmp_path, slack, email_triage=False, tz="America/Los_Angeles")
    calls = []
    monkeypatch.setattr("wanda.vault.housekeep", lambda cfg: calls.append(cfg.snapshots_dir) or None)
    monkeypatch.setattr(main, "HOUSEKEEPING_HOUR", 0)
    asyncio.run(p.drain_mail())
    p.cfg.snapshots_dir.mkdir()
    asyncio.run(p.drain_mail())
    assert calls == [], "no snapshots (a directory without its HEAD), nothing to look after"
    (p.cfg.snapshots_dir / "HEAD").write_text("ref: refs/heads/master\n")
    now = datetime.now(p.cfg.zone)
    monkeypatch.setattr(main, "HOUSEKEEPING_HOUR", now.hour + 1)
    asyncio.run(p.drain_mail())
    assert calls == [], "not before the quiet hour"
    monkeypatch.setattr(main, "HOUSEKEEPING_HOUR", now.hour)
    asyncio.run(p.drain_mail())
    asyncio.run(p.drain_mail())
    assert calls == [p.cfg.snapshots_dir]
    assert store.get_meta("snapshots_housekept") == datetime.now(p.cfg.zone).date().isoformat(), "the local date"
    store.set_meta("snapshots_housekept", "2026-01-01")  # the next day
    monkeypatch.setattr("wanda.vault.housekeep", lambda cfg: "housekeeping of snapshots.git: exit 128: fatal")
    asyncio.run(p.drain_mail())
    assert slack.alerts == ["vault snapshots: housekeeping of snapshots.git: exit 128: fatal"]


def test_the_look_at_the_snapshots_holds_up_nothing(tmp_path, monkeypatch):
    """It goes over the Mac's mount: only from the quiet hour on a day not yet
    looked after, off the event loop, given up on after its time, and never a
    second while one has not come back."""
    import threading

    from wanda import main

    p, store = make(tmp_path, email_triage=False, tz="America/Los_Angeles")
    looks, answer = [], threading.Event()

    def stalled(cfg):
        looks.append(cfg)
        answer.wait(10)
        return True
    monkeypatch.setattr("wanda.vault.has_snapshots", stalled)
    monkeypatch.setattr("wanda.vault.housekeep", lambda cfg: None)
    now = datetime.now(p.cfg.zone)
    monkeypatch.setattr(main, "HOUSEKEEPING_HOUR", now.hour + 1)
    asyncio.run(p.drain_mail())
    assert looks == [], "not before the quiet hour"
    monkeypatch.setattr(main, "HOUSEKEEPING_HOUR", now.hour)
    monkeypatch.setattr(main, "SNAPSHOTS_LOOK_S", 0.2)

    async def two_passes():
        t0 = time.monotonic()
        await p.drain_mail()
        await p.drain_mail()
        took = time.monotonic() - t0
        answer.set()
        await p._snapshots_look
        return took
    assert asyncio.run(two_passes()) < 2, "the loop went on without it"
    assert len(looks) == 1, "one look at a time"
    assert store.get_meta("snapshots_housekept") is None, "the housekeeping waits for a later pass"
    asyncio.run(p.drain_mail())
    assert len(looks) == 2 and store.get_meta("snapshots_housekept") == datetime.now(p.cfg.zone).date().isoformat()


def connected(watcher) -> None:
    """SlackWatcher.start, as auth.test names her: her bot user, and her bot."""
    watcher.bot_user_id, watcher.bot_id = "UBOT", "BME"


def slack_names(monkeypatch, answers=None) -> list[str]:
    """A start's reads of the household's names: fan and mei unless
    `answers` says what Slack gives each id, a record or an error to raise.
    Each start, with `vault.prepare` faked, finds no snapshot either."""
    read = []
    answers = answers or {"U1": {"profile": {"display_name": "fan"}}, "U2": {"profile": {"display_name": "mei"}}}

    async def user_now(self, uid):
        read.append(uid)
        got = answers[uid]
        if isinstance(got, Exception):
            raise got
        return got
    monkeypatch.setattr("wanda.actions.slack.SlackActions.user_now", user_now)
    monkeypatch.setattr("wanda.vault.last_snapshot", lambda cfg: "none")
    return read


def test_a_good_start_clears_a_failed_starts_alert(tmp_path, monkeypatch):
    """A start that fails while Slack is unreachable keeps its alert for a
    retry; a later start that passes must not post it while running."""
    from wanda import main

    posted, made = [], []
    slack_up = {"up": False}

    async def alert(self, text):
        if not slack_up["up"]:
            raise RuntimeError("no network")
        posted.append(text)

    async def one_pass(self):
        await self.drain_mail()  # the mail loop's first pass
        os.kill(os.getpid(), signal.SIGTERM)

    class Runner(RunnerService):
        def __init__(self, claude_bin, **kw):
            made.append(kw["agent_sem"]._value)
            super().__init__(claude_bin, **kw)

    monkeypatch.setattr("wanda.actions.slack.SlackActions.alert", alert)
    monkeypatch.setattr("wanda.main.SlackWatcher.start", connected)
    monkeypatch.setattr("wanda.main.SlackWatcher.stop", lambda self: None)
    monkeypatch.setattr("wanda.main.Processor.loop", one_pass)
    monkeypatch.setattr("wanda.main.RunnerService", Runner)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)  # one process, two starts
    slack_names(monkeypatch)
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y",
               alert_channel="C9", slack_owner_user_ids="U1,U2",
               tz="America/Los_Angeles", email_triage=False, claude_bin="/bin/true", memory_sessions=1)
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: "mem recall me: timed out")
    with pytest.raises(SystemExit):
        asyncio.run(main.run_daemon(c))
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: None)
    slack_up["up"] = True
    asyncio.run(main.run_daemon(c))
    assert not any("is not running" in t for t in posted)
    assert made == [1, 1], "the setting is how many sessions run at once"


def test_a_start_that_cannot_open_the_run_store_says_so_and_waits(tmp_path, monkeypatch):
    """A full disk of the VM fills the run store too: the start says so, tries
    its alert until Slack takes it, at most once a day, and goes on once the
    store opens, where exiting would restart it about once a minute."""
    from wanda import main

    posted, tries, opened = [], {"alert": 0}, {"n": 0}
    real = main.Store

    def store(path):
        opened["n"] += 1
        if opened["n"] <= 3:
            raise sqlite3.OperationalError("disk I/O error")
        return real(path)

    async def alert(self, text):
        tries["alert"] += 1
        if tries["alert"] == 1:
            raise RuntimeError("no network")
        posted.append(text)

    async def one_pass(self):
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr("wanda.main.Store", store)
    monkeypatch.setattr("wanda.main.STORE_RETRY_S", 0)
    monkeypatch.setattr("wanda.actions.slack.SlackActions.alert", alert)
    monkeypatch.setattr("wanda.main.SlackWatcher.start", connected)
    monkeypatch.setattr("wanda.main.SlackWatcher.stop", lambda self: None)
    monkeypatch.setattr("wanda.main.Processor.loop", one_pass)
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: None)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)
    slack_names(monkeypatch)
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y",
               alert_channel="C9", slack_owner_user_ids="U1,U2",
               tz="America/Los_Angeles", email_triage=False, claude_bin="/bin/true")
    asyncio.run(main.run_daemon(c))
    assert opened["n"] == 4 and tries["alert"] == 2
    assert posted == [f"wanda is not running: the run store {c.db_path} could not be opened or written: "
                      "disk I/O error (README, State)"]


def test_a_start_whose_run_store_opens_and_takes_no_write_says_so_and_waits(tmp_path, monkeypatch):
    """A store a stopped run left with its WAL opens on a full disk, and with
    nothing old enough to prune only a write fails: the start says so and
    alerts, where it died recording its first alert."""
    from wanda import main

    posted, tries, full = [], {"alert": 0}, {"writes": 0}
    real = Store.set_meta

    def set_meta(self, key, value):
        if key == "started_at" and full["writes"] < 3:
            full["writes"] += 1
            raise sqlite3.OperationalError("database or disk is full")
        real(self, key, value)

    async def alert(self, text):
        tries["alert"] += 1
        if tries["alert"] == 1:
            raise RuntimeError("no network")
        posted.append(text)

    async def one_pass(self):
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr("wanda.store.Store.set_meta", set_meta)
    monkeypatch.setattr("wanda.main.STORE_RETRY_S", 0)
    monkeypatch.setattr("wanda.actions.slack.SlackActions.alert", alert)
    monkeypatch.setattr("wanda.main.SlackWatcher.start", connected)
    monkeypatch.setattr("wanda.main.SlackWatcher.stop", lambda self: None)
    monkeypatch.setattr("wanda.main.Processor.loop", one_pass)
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: None)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)
    slack_names(monkeypatch)
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y",
               alert_channel="C9", slack_owner_user_ids="U1,U2",
               tz="America/Los_Angeles", email_triage=False, claude_bin="/bin/true")
    asyncio.run(main.run_daemon(c))
    assert full["writes"] == 3 and tries["alert"] == 2
    assert posted == [f"wanda is not running: the run store {c.db_path} could not be opened or written: "
                      "database or disk is full (README, State)"]
    store = Store(c.db_path)
    assert store.get_meta("started_at") and store.get_meta("sessions_left_running") == "0"


def test_starts_that_die_before_she_runs_leave_one_interval_she_was_down(tmp_path, monkeypatch):
    """Her running time, which delivery's two hours count: she was down from
    the last time she was known to run, at a pass or a stop's end, until a
    start got her running. Starts that die before then, and one that dies
    after it before a pass, as in a restart loop, leave one interval."""
    from wanda import main

    async def one_pass(self):
        await self.drain_mail()  # the mail loop's first pass
        os.kill(os.getpid(), signal.SIGTERM)

    async def alert(self, text):
        pass

    recoveries = []
    real = main.Processor.startup_recovery

    async def recovery(self):
        recoveries.append(1)
        if len(recoveries) == 1:
            raise RuntimeError("the store went away")
        await real(self)

    monkeypatch.setattr("wanda.actions.slack.SlackActions.alert", alert)
    monkeypatch.setattr("wanda.main.SlackWatcher.start", connected)
    monkeypatch.setattr("wanda.main.SlackWatcher.stop", lambda self: None)
    monkeypatch.setattr("wanda.main.Processor.loop", one_pass)
    monkeypatch.setattr("wanda.main.Processor.startup_recovery", recovery)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)
    slack_names(monkeypatch)
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y",
               alert_channel="C9", slack_owner_user_ids="U1,U2",
               tz="America/Los_Angeles", email_triage=False, claude_bin="/bin/true")
    last = "2026-10-01T15:00:00+00:00"
    Store(c.db_path).set_meta("up_at", last)
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: "mem recall me: timed out")
    for _ in range(3):
        with pytest.raises(SystemExit):
            asyncio.run(main.run_daemon(c))
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: None)
    with pytest.raises(RuntimeError, match="the store went away"):
        asyncio.run(main.run_daemon(c))
    started = utcnow()
    asyncio.run(main.run_daemon(c))
    store = Store(c.db_path)
    (since, until), = json.loads(store.get_meta("down"))
    assert since == last and until >= started
    assert store.get_meta("up_at") >= until, "moved on by the pass and the stop"


@pytest.mark.parametrize("auth", [RuntimeError("invalid_auth"), {"ok": True, "bot_id": "BME"},
                                  {"ok": True, "user_id": "UBOT", "bot_id": "BME"}],
                         ids=["auth.test fails", "it names no user", "it names both"])
def test_a_start_knows_her_own_ids_before_any_session_or_exits_saying_why(tmp_path, monkeypatch, auth):
    """Every frame tells her own posts and mentions by her ids, which the
    watcher's auth.test gives at the start, before any session: without them
    the start exits, saying why; with them, they are what every frame is
    given, with no call to Slack for them again."""
    from wanda.watchers import slack_watcher

    class Web:
        def __init__(self, **kw):
            pass

        def auth_test(self):
            if isinstance(auth, Exception):
                raise auth
            return auth

    class Socket:
        current_session = None

        def __init__(self, **kw):
            self.socket_mode_request_listeners = []

        def connect(self):
            pass

        def is_connected(self):
            return False

        def close(self):
            pass

    held, calls, sessions = [], [], []

    async def one_pass(self):
        held.append(await self.slack.own_ids())
        os.kill(os.getpid(), signal.SIGTERM)

    async def recovery(self):
        sessions.append("startup recovery")

    async def call(self, method, /, **kw):
        calls.append(method)
        return {}

    monkeypatch.setattr(slack_watcher, "WebClient", Web)
    monkeypatch.setattr(slack_watcher, "Connections", Socket)
    monkeypatch.setattr("wanda.actions.slack.SlackActions._call", call)
    monkeypatch.setattr("wanda.main.Processor.loop", one_pass)
    monkeypatch.setattr("wanda.main.Processor.startup_recovery", recovery)
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: None)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)
    slack_names(monkeypatch)
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y", alert_channel="C9",
               slack_owner_user_ids="U1,U2", tz="America/Los_Angeles", email_triage=False, claude_bin="/bin/true")
    if isinstance(auth, dict) and auth.get("user_id"):
        asyncio.run(main.run_daemon(c))
        assert held == [{"UBOT", "BME"}] and sessions == ["startup recovery"] and "auth_test" not in calls
        return
    with pytest.raises(SystemExit, match="could not connect to Slack: (invalid_auth|auth.test named no bot user)"):
        asyncio.run(main.run_daemon(c))
    assert held == [] and sessions == []


def test_doctor_lists_the_answers_given_up_on(tmp_path, capsys):
    """The give-up alert names each answer by its run and time only; where
    it was due is for whoever runs doctor through exec."""
    from wanda.main import MAX_DELIVERY_ATTEMPTS, run_doctor

    c = Config(_env_file=None, data_dir=tmp_path, claude_bin="/bin/true", email_triage=False)
    store = Store(c.db_path)
    dm_task = store.create_task(None, "D0MEI", "conversation", kind="dm")
    thread_task = store.create_task(None, "C0KITCHEN", "1767225600.000100", kind="mention")

    def run(task):
        return store.record_run(kind="agent", task_id=task, session_id="s", started_at=utcnow(),
                                exit_code=0, cost_usd=0.4, status="ok", result_text="x", notified=0)
    given_up, kept, in_thread = run(dm_task), run(dm_task), run(thread_task)
    for r in (given_up, in_thread):
        store.give_up(r, MAX_DELIVERY_ATTEMPTS)
    store.first_refusal(kept)
    asyncio.run(run_doctor(c, smoke=False))
    out = capsys.readouterr().out
    assert "answers given up on — 2, newest first" in out
    assert out.index(f"run {in_thread}, from ") < out.index(f"run {given_up}, from "), "newest first"
    assert ": C0KITCHEN, thread 1767225600.000100" in out and ": D0MEI\n" in out
    assert f"run {kept}, from " not in out


def test_doctor_says_what_happened_since_the_last_start(tmp_path, capsys, monkeypatch):
    """What to look for after a session's leftovers were ended or a `mem`
    gave up on a busy vault, neither of which is alerted; and whether the
    run store takes a write, which on a full disk it opens without."""
    from wanda.main import run_doctor

    c = Config(_env_file=None, data_dir=tmp_path, claude_bin="/bin/true", email_triage=False)
    store = Store(c.db_path)
    store.set_meta("started_at", "2026-10-02T12:00:00+00:00")
    store.set_meta("sessions_left_running", "2")
    start = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("wanda.vault.refused_for_a_busy_vault", lambda cfg, since: 3 if since == start else 0)
    asyncio.run(run_doctor(c, smoke=False))
    out = capsys.readouterr().out
    assert "✓ takes a write\n" in out
    assert ("since the last start — 2026-10-02T12:00:00+00:00: 2 session(s) left processes running; "
            "3 mem call(s) refused for a busy vault") in out
    real = Store.set_meta

    def full(self, key, value):
        if key == "doctor_ran":
            raise sqlite3.OperationalError("database or disk is full")
        real(self, key, value)
    monkeypatch.setattr("wanda.store.Store.set_meta", full)
    asyncio.run(run_doctor(c, smoke=False))
    assert "✗ takes a write — database or disk is full (README, State)" in capsys.readouterr().out


def test_a_session_that_left_processes_running_is_counted(tmp_path, monkeypatch):
    """For doctor, since the start; the runner's log names each."""
    class Leaving(RecordingRunner):
        async def run(self, prompt, **kw):
            rr = await super().run(prompt, **kw)
            rr.left_running = 2 if len(self.calls) == 1 else 0
            return rr

    p, store, _ = memory_processor(tmp_path, ConversationSlack(), Leaving(), monkeypatch)
    store.set_meta("sessions_left_running", "0")
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "the plumber comes Tuesday")))
    asyncio.run(p.handle_slack(dm(f"{AT + 60:.1f}", "at nine")))
    assert store.get_meta("sessions_left_running") == "1"


def test_each_session_logs_its_wait_its_time_and_its_looks_back(tmp_path, monkeypatch, caplog):
    """A session's time, weighed against the transcripts every look back
    reads, is apart from its wait for a slot, which with one session at a
    time is another conversation's session; the look backs' own time is
    given beside it."""
    import logging
    import re
    from pathlib import Path

    from wanda import vault
    monkeypatch.setenv("HOME", str(tmp_path / "h"))

    class LookingBack(RecordingRunner):
        async def run(self, prompt, **kw):
            d = vault.transcripts_dir(Path(kw["cwd"]))
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{kw['session_id']}.jsonl").write_text("\n".join([
                json.dumps({"type": "assistant", "timestamp": "2026-10-01T23:40:00.000Z", "message": {
                    "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                 "input": {"command": "mem session --with mei --last 5; mem recall mei"}}]}}),
                json.dumps({"type": "user", "timestamp": "2026-10-01T23:40:01.500Z", "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "..."}]}})]) + "\n")
            await asyncio.sleep(0.3)
            return await super().run(prompt, **kw)

    runner = LookingBack()
    runner.agent_sem = asyncio.Semaphore(1)
    p, _, _ = memory_processor(tmp_path, ConversationSlack(), runner, monkeypatch)

    async def two():
        meis = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "out Tuesday", channel="D2", user="U2")))
        await asyncio.sleep(0.05)
        await asyncio.gather(meis, p.handle_slack(dm(f"{AT + 1:.1f}", "the plumber comes Tuesday")))
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(two())
    said = {r.getMessage().split(" in ")[1].split(":")[0]: r.getMessage() for r in caplog.records
            if r.getMessage().startswith("memory session ")}
    times = {ch: re.search(r": waited ([\d.]+) s for a slot, ran ([\d.]+) s, 1 mem session call\(s\) over every "
                           r"transcript, as written in its commands, and ([\d.]+) s in the commands holding them; ",
                           line) for ch, line in said.items()}
    assert sorted(times) == ["D1", "D2"] and all(times.values())
    assert all(m[3] == "1.5" for m in times.values()), "each look back's own time"
    # mei's session came first; fan's waited for its slot
    assert float(times["D2"][1]) < 0.1 and float(times["D1"][1]) >= 0.2
    assert all(float(m[2]) >= 0.3 for m in times.values())


def test_reply_requires_an_explicit_channel():
    """A defaulted channel published a DM answer in the triage channel the one
    time a caller forgot it. Keyword-only and required makes that a TypeError."""
    import inspect

    from wanda.actions.slack import SlackActions

    sig = inspect.signature(SlackActions.reply)
    channel = sig.parameters["channel"]
    assert channel.default is inspect.Parameter.empty, "channel must have no default"
    assert channel.kind is inspect.Parameter.KEYWORD_ONLY, "and must be passed by name"


# 16:40 on Thursday 1 October 2026 in Los Angeles
AT = 1790898000.0


class ConversationSlack(FakeSlack):
    """A DM in which fan asked something a minute ago and wanda answered.
    `paged` reads its history as fetch_context does, newest first back to
    the time it is given, until EARLIER of the lines read pass `counted`,
    as if each page held one."""

    def __init__(self, members=None, workspace=None, history=None, paged=False, **kw):
        super().__init__(**kw)
        self.paged = paged
        self.member_ids = members
        self.people = workspace or [{"id": "U1"}, {"id": "U2"}, {"id": "UBOT", "is_bot": True}]
        self.history = history if history is not None else [
            {"user": "U1", "ts": f"{AT - 120:.1f}", "text": "<@UBOT> can you check the invoice?"},
            {"user": "UBOT", "bot_id": "BME", "ts": f"{AT - 60:.1f}", "text": "Which one?"}]
        # what users() holds, and every id it asked Slack for
        self.held: dict[str, dict] = {}
        self.asked: list[str] = []
        # each history read's arguments
        self.fetched: list[tuple] = []
        # What a read back from Slack finds: the conversations she is in, as
        # users.conversations gives them; each one's history and each
        # thread's replies, by channel and by (channel, thread ts), which it
        # reads as Slack does, from the time it is given; each call it made
        # and its arguments; and an error to raise at a call, by its arguments
        self.listed: list[dict] = []
        self.histories: dict[str, list[dict]] = {}
        self.replies_of: dict[tuple[str, str], list[dict]] = {}
        self.read_calls = 0
        self.reads: list[tuple] = []
        self.read_errors: dict[tuple, BaseException] = {}
        # the time each conversation's history was read back from
        self.oldest: dict[str, float] = {}

    async def fetch_context(self, channel, thread_ts, since, counted=None):
        self.fetched.append((channel, thread_ts, since, counted))
        if not self.paged:
            return list(self.history)
        read, n = [], 0
        for m in sorted(self.history, key=lambda m: float(m["ts"]), reverse=True):
            if float(m["ts"]) < since or n >= vault.EARLIER:
                break
            read.append(m)
            n += bool(counted and counted(m))
        return read[::-1]

    def kept(self, ids):
        return {u: self.held[u] for u in ids if u in self.held}

    def _read(self, *call, oldest=None):
        self.read_calls += 1
        self.reads.append(call)
        if oldest is not None:
            self.oldest[call[1]] = oldest
        if (e := self.read_errors.get(call[:2]) or self.read_errors.get(call[:3])) is not None:
            raise e

    async def conversations(self):
        self._read("conversations")
        return list(self.listed)

    async def read_history(self, channel, oldest):
        self._read("history", channel, oldest=oldest)
        yield sorted((m for m in self.histories.get(channel, []) if float(m["ts"]) >= oldest),
                     key=lambda m: float(m["ts"]), reverse=True), False

    async def read_thread(self, channel, ts, oldest):
        self._read("thread", channel, ts)
        return [m for m in self.replies_of.get((channel, ts), []) if float(m["ts"]) >= oldest or m["ts"] == ts]

    async def users(self, ids):
        known = {"U1": {"profile": {"display_name": "fzhu"}}, "U2": {"profile": {"display_name": "mei"}},
                 "U3": {"profile": {"display_name": "jane"}},
                 "UBOT": {"is_bot": True, "profile": {"display_name": "wanda"}}}
        for u in set(ids) - self.held.keys():
            self.asked.append(u)
            if u in known:
                self.held[u] = known[u]
        return self.kept(ids)

    async def members(self, channel):
        if self.member_ids is None:
            raise RuntimeError("missing_scope")
        return self.member_ids

    async def workspace(self):
        return self.people

    async def own_ids(self):
        return frozenset({"UBOT", "BME"})


class RecordingRunner:
    """Stands in for claude: records each run and reports what it is given,
    or ends as a RunResult it is given. A session with a feed begins one
    turn, as the runner tells its feed."""

    def __init__(self, *reports, ok=True):
        self.agent_sem = asyncio.Semaphore(2)
        self.calls = []
        self.reports = list(reports)
        self.ok = ok

    async def run(self, prompt, **kw):
        if kw.get("feed") is not None:
            kw["feed"].began()
        self.calls.append((prompt, kw))
        out = self.reports.pop(0) if self.reports else {"recalled": [], "answer": "", "recorded": []}
        if isinstance(out, RunResult):
            return out
        if not self.ok:
            return RunResult(ok=False, error="claude reported an error")
        return RunResult(ok=True, structured=out if isinstance(out, dict) else None,
                         result_text=out if isinstance(out, str) else json.dumps(out), session_id="ignored")


def dm(ts, text, channel_type="im", thread=None, channel="D1", user="U1"):
    return Event(source="slack", dedupe_key=f"{channel}:{ts}", payload={
        "kind": "dm" if channel_type in ("im", "mpim") else "mention", "channel": channel,
        "channel_type": channel_type, "task_key": thread or "conversation", "reply_thread": thread,
        "in_thread": bool(thread), "user": user, "text": text, "files": [], "ts": ts})


def told(store, names=None, at=None) -> None:
    """The run store as a start leaves it once Slack has named each id, here
    fan and mei by default, the names sessions are then told."""
    h = Household.load(store, [])
    for uid, name in (names or {"U1": "fan", "U2": "mei"}).items():
        h.observe(uid, {"profile": {"display_name": name}}, at or datetime(2026, 9, 1, tzinfo=timezone.utc))
        h.save(store, uid)


# how late a turn is when it is framed at its session's start, which
# memory_processor sets aside and a test of a late turn sets back
LATE_TURN_S = main.LATE_TURN_S


def memory_processor(tmp_path, slack, runner, monkeypatch, snapshot=None):
    p, store = make(tmp_path, slack, data_dir=tmp_path, slack_owner_user_ids="U1,U2", tz="America/Los_Angeles")
    # the tests' messages are dated AT, before any test runs, and are framed
    # at their own time, as a message just sent is, and her reaction on each
    # is made at once and not waited for, as on a message just sent; a test
    # of a turn that comes late, or of a late reaction, sets the threshold
    # back and the time it runs at
    monkeypatch.setattr(main, "LATE_TURN_S", 10 ** 9)
    monkeypatch.setattr(main, "LATE_ADD_S", 10 ** 9)
    told(store)
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    p.runner = runner
    snaps = []
    monkeypatch.setattr("wanda.vault.snapshot", snapshot or (lambda cfg, message: snaps.append(message)))
    return p, store, snaps


def answer(text):
    return {"recalled": ["person:aaaaaa"], "answer": text, "recorded": ["event:x"]}


def keep(store, ev, *sessions):
    """A message as the watcher keeps it until it is answered, then taken by
    each of `sessions` in turn, none of which finished. Returns its key."""
    store.first_time(ev.dedupe_key, ev.payload)
    key = (ev.payload["channel"], ev.payload["ts"])
    for sid in sessions:
        store.took(sid, [key], {key})
    return key


def kept(store):
    """Each message kept: its time, its state, its tries and its session."""
    return [(r["ts"], r["state"], r["tries"], r["session"]) for r in store.kept()]


def started_again(p, runner, slack=None):
    """The next start's processor on the same run store, which runs again
    what was kept as run_daemon has it run again, each turn to its end."""
    q = Processor(p.cfg, p.store, asyncio.Queue(), slack or p.slack, runner)

    async def go():
        for task, keys in q.kept():
            q.take_up(task, keys)
        while q._bg:
            await asyncio.sleep(0.01)
    asyncio.run(go())
    return q


# a report filled with scaffolding, which is no report
FILLER = {"recalled": ["test"], "answer": "test", "recorded": ["test"]}


def test_each_turn_is_a_fresh_session_in_the_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("WANDA_SLACK_BOT_TOKEN", "xoxb-not-for-sessions")
    runner = RecordingRunner()
    p, _, snaps = memory_processor(tmp_path, ConversationSlack(), runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "the March one")))
    asyncio.run(p.handle_slack(dm(f"{AT + 60:.1f}", "and is it paid?")))

    (first, a), (_, b) = runner.calls
    assert a["session_id"] != b["session_id"] and "resume" not in a
    assert a["cwd"] == str(tmp_path / "vault") and a["tools"] == a["allowed_tools"] == "Read,Glob,Grep,Bash,Skill"
    assert a["output_schema"]["required"] == ["recalled", "answer", "recorded"]
    assert a["inherit_env"] is False and not [k for k in a["env"] if k.startswith("WANDA_")]
    assert a["env"]["MEM_SESSION"] == a["session_id"]
    # what the session leaves running is found by what it inherits, and ended
    assert a["mark"] == f"MEM_SESSION={a['session_id']}"
    # the message's own time, in the household's zone
    assert a["append_system_prompt"].startswith(
        ANCHOR + "\n\nToday is Thursday, 2026-10-01, and it is 16:40 here (PDT) as this session begins. ")
    assert a["env"]["MEM_DATE"] == "2026-10-01" and first.startswith("I am wanda.\n\nToday is 2026-10-01.\n\n")
    # the vault's name for him, not his Slack one; what came before, each line with its time
    assert "In a direct message that fan and I read." in first
    assert ("    16:38 fan: @wanda can you check the invoice?\n    16:39 me: Which one?\n\n"
            "fan now says:\n\n    the March one") in first
    assert snaps == [f"after {a['session_id']}", f"after {b['session_id']}"]


@pytest.mark.parametrize("reports", [(answer("Have it."),), ("I filed it.", answer("Have it."))],
                         ids=["first", "a retry"])
def test_a_message_session_is_told_what_does_not_wake_her(tmp_path, monkeypatch, reports):
    """After its date paragraph, and its quiet retry's too, so that what she
    says she will do is only what will happen."""
    runner = RecordingRunner(*reports)
    p, _, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "the dentist moved to the 14th")))
    assert len(runner.calls) == len(reports) and p.slack.replies == ["Have it."]
    paragraph = vault.date_paragraph(datetime.fromtimestamp(AT, p.cfg.zone))
    for _, kw in runner.calls:
        assert kw["append_system_prompt"] == f"{ANCHOR}\n\n{paragraph}\n\n{vault.NO_WAKE}"


def test_a_framed_session_is_told_what_does_not_wake_her_only_when_asked(tmp_path, monkeypatch):
    """By the flag, not by the frame: a session framed in a conversation
    that something other than a message started is not told it."""
    runner = RecordingRunner(answer(""))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")
    now = datetime.fromtimestamp(AT, p.cfg.zone)

    async def frame(again):
        return "an arrival", now, Additions([], lambda m: None, main.Holding(store))
    assert asyncio.run(p.memory_turn(task, None, None, channel="D1", reply_thread=None, owed=False,
                                     frame=frame)) is None
    [(_, kw)] = runner.calls
    assert kw["append_system_prompt"] == f"{ANCHOR}\n\n{vault.date_paragraph(now)}"


def test_each_snapshot_is_followed_by_a_look_for_files_mem_cannot_read(tmp_path, monkeypatch):
    """A node file a session or a hand damaged since is put back, or left
    out and alerted, before the next session meets it."""
    seen = []
    runner = RecordingRunner()
    p, _, _ = memory_processor(tmp_path, ConversationSlack(), runner, monkeypatch,
                               snapshot=lambda cfg, message: seen.append(message))
    monkeypatch.setattr("wanda.vault.put_back", lambda cfg: seen.append("look") or [])
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "the March one")))
    asyncio.run(p.handle_slack(dm(f"{AT + 60:.1f}", "and is it paid?")))
    (_, a), (_, b) = runner.calls
    assert seen == [f"after {a['session_id']}", "look", f"after {b['session_id']}", "look"]


def test_silence_posts_nothing_and_owes_nothing(tmp_path, monkeypatch):
    slack = ConversationSlack()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("")), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "the plumber comes Tuesday")))
    assert slack.replies == []
    assert store.pending_deliveries() == []
    run = store._query("SELECT * FROM runs")[0]
    assert run["status"] == "ok" and run["notified"] == 1 and run["result_text"] == ""


def test_an_answer_is_posted_once_where_it_was_asked(tmp_path, monkeypatch):
    slack = ConversationSlack()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("Will do — Tuesday.")), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "remind me tuesday", thread="77.1")))
    assert slack.replies == ["Will do — Tuesday."] and slack.channels == ["D1"] and slack.threads == ["77.1"]
    assert store.pending_deliveries() == []


def test_the_answer_is_recorded_and_posted_before_the_snapshot(tmp_path, monkeypatch):
    """The answer does not wait for the snapshot, which can wait its turn
    behind another, and a restart during either still finds it recorded."""
    seen = []
    slack = ConversationSlack()
    p, store, _ = memory_processor(
        tmp_path, slack, RecordingRunner(answer("Yes.")), monkeypatch,
        snapshot=lambda cfg, message: seen.append(
            (list(slack.replies), [dict(r) for r in store._query("SELECT result_text, notified FROM runs")])))
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    assert seen == [(["Yes."], [{"result_text": "Yes.", "notified": 1}])] and slack.replies == ["Yes."]


def test_a_redelivery_pass_during_the_snapshot_posts_nothing_twice(tmp_path, monkeypatch):
    """The mail loop's pass, once a minute, falling in a slow snapshot."""
    slack = ConversationSlack()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("Yes, paid on the 3rd.")),
                                   monkeypatch, snapshot=lambda cfg, message: time.sleep(0.5))

    async def go():
        turn = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
        await asyncio.sleep(0.2)
        await p.deliver_pending()
        await turn
    asyncio.run(go())
    assert slack.replies == ["Yes, paid on the 3rd."] and store.pending_deliveries() == []


def test_a_redelivery_pass_skips_what_was_posted_while_it_waited(tmp_path, monkeypatch):
    """The pass reads its list once; a reply handler can post one of its rows
    while the pass is still posting an earlier one."""
    class Slow(ConversationSlack):
        async def reply(self, thread_ts, text, channel=None):
            await asyncio.sleep(0.2)
            await super().reply(thread_ts, text, channel)

    slack = Slow()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(), monkeypatch)
    tid = store.create_task(None, "D1", "conversation", kind="dm")
    first, second = (store.record_run(kind="agent", task_id=tid, session_id=None, started_at=utcnow(),
                                      exit_code=0, cost_usd=0.0, status="ok", result_text=t, notified=0)
                     for t in ("an older answer", "Yes."))

    async def go():
        posting = p._delivering[second] = asyncio.Event()
        redelivery = asyncio.create_task(p.deliver_pending())
        await asyncio.sleep(0.1)  # the pass is posting the first
        store.mark_run_notified(second)  # as _post_run does once Slack takes it
        del p._delivering[second]
        posting.set()
        await redelivery
    asyncio.run(go())
    assert slack.replies == ["an older answer"]


def test_a_failed_snapshot_is_alerted(tmp_path, monkeypatch):
    slack = ConversationSlack()
    p, _, _ = memory_processor(tmp_path, slack, RecordingRunner(), monkeypatch,
                               snapshot=lambda cfg, message: f"snapshot {message!r}: failed: the vault "
                                                             "stayed locked for 60 s")
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "noted")))
    assert len(slack.alerts) == 1 and slack.alerts[0].startswith("vault snapshots: snapshot 'after ")
    assert slack.alerts[0].endswith("': failed: the vault stayed locked for 60 s")


def test_an_alert_is_shown_to_a_session_as_an_alert_never_as_her_words(tmp_path, monkeypatch):
    """Alerts may go to fan's DM, where his sessions are framed: posted with
    the harness's mark, they are shown as alerts posted in her name."""
    from wanda.vault import ALERT_EVENT

    history = [{"user": "U1", "ts": f"{AT - 120:.1f}", "text": "the plumber comes thursday"},
               {"user": "UBOT", "bot_id": "BME", "ts": f"{AT - 60:.1f}",
                "text": "⚠️ wanda is not running: memory is not working: mem entity: exit 1",
                "metadata": {"event_type": ALERT_EVENT, "event_payload": {}}}]
    runner = RecordingRunner()
    p, _, _ = memory_processor(tmp_path, ConversationSlack(history=history), runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "and the electrician friday")))
    first = runner.calls[0][0]
    assert ("    16:38 fan: the plumber comes thursday\n    16:39 an alert posted in my name: ⚠️ wanda is not "
            "running: memory is not working: mem entity: exit 1\n") in first
    assert not re.search(r"\d me: ⚠️", first)


ALERTED_AT = f"{AT - 60:.6f}"


class AlertWeb:
    """chat.postMessage as Slack answers it: the channel it posted in, and
    the post's ts."""

    def __init__(self, channel):
        self.channel = channel

    def chat_postMessage(self, **kw):
        return {"ok": True, "channel": self.channel, "ts": ALERTED_AT}


def alert_to(monkeypatch, to, answered, store, text="a vault snapshot failed"):
    import wanda.actions.slack as actions

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(alert_channel=to), store)
    sa.web = AlertWeb(answered)
    asyncio.run(sa.alert(text))


def test_an_alert_records_its_thread_as_hers_only_outside_a_dm(tmp_path, monkeypatch, caplog):
    """Under the channel and ts Slack answered with, as a conversation begun
    with @wanda. Not where every reply already reaches her as a DM's: an
    alert to a user's id, whatever channel Slack answers with, or one Slack
    answers with a DM's channel; not with no store open; and a record that
    fails is logged, never raised, since the alert is posted."""
    import logging

    for i, (to, answered, recorded) in enumerate((("C9", "C9", [("mention", "C9", ALERTED_AT)]),
                                                  ("U0FAN", "C7", []), ("W0FAN", "C7", []), ("C9", "D7", []))):
        store = Store(tmp_path / f"{i}.db")
        alert_to(monkeypatch, to, answered, store)
        assert [tuple(r) for r in store._query("SELECT kind, slack_channel, thread_ts FROM tasks")] == recorded, to
    with caplog.at_level(logging.WARNING, logger="wanda"):
        alert_to(monkeypatch, "C9", "C9", None)
        assert caplog.text == ""
        store.close()
        alert_to(monkeypatch, "C9", "C9", store)
    assert f"could not record the thread of the alert {ALERTED_AT} in C9" in caplog.text


def test_a_members_reply_under_an_alert_in_a_channel_is_a_turn_of_hers(tmp_path, monkeypatch):
    """With no @wanda: the reply runs a session whose thread opens with the
    alert under its label. A threaded reply under an alert in fan's DM opens
    a DM's task, as any reply there does."""
    from types import SimpleNamespace

    from wanda.vault import ALERT_EVENT
    from wanda.watchers.slack_watcher import SlackWatcher

    alert = {"user": "UBOT", "bot_id": "BME", "ts": ALERTED_AT, "text": "⚠️ a vault snapshot failed",
             "metadata": {"event_type": ALERT_EVENT, "event_payload": {}}}
    runner = RecordingRunner(answer("A snapshot failed; nothing is lost."))
    slack = ConversationSlack(members=["U1", "UBOT"], history=[alert])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)

    def reply_under(channel, kind):
        loop = asyncio.new_event_loop()
        queue = asyncio.Queue()
        w = SlackWatcher(p.cfg, store, loop, queue)
        w.bot_user_id = "UBOT"
        w._handle(SimpleNamespace(send_socket_mode_response=lambda r: None), SimpleNamespace(
            type="events_api", envelope_id="e", payload={"event": {
                "type": "message", "user": "U1", "channel": channel, "channel_type": kind, "ts": f"{AT:.6f}",
                "thread_ts": ALERTED_AT, "text": "what does this mean?"}}))
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        return queue.get_nowait()

    alert_to(monkeypatch, "C9", "C9", store)
    ev = reply_under("C9", "group")
    assert ev.payload["kind"] == "task"
    asyncio.run(p.handle_slack(ev))
    assert ("In a Slack thread that fan and I read. Everyone in it sees what I say there.\n\nThe thread so far:\n\n"
            "    16:39 an alert posted in my name: ⚠️ a vault snapshot failed\n\nfan now says:\n\n"
            "    what does this mean?") in runner.calls[0][0]
    assert slack.replies == ["A snapshot failed; nothing is lost."] and slack.threads == [ALERTED_AT]
    alert_to(monkeypatch, "U1", "D1", store)
    ev = reply_under("D1", "im")
    assert ev.payload["kind"] == "dm"
    asyncio.run(p.handle_slack(ev))
    assert store.get_task_by_thread("D1", ALERTED_AT)["kind"] == "dm"


def test_a_frame_looks_up_every_mention_of_a_members_and_few_of_anyone_elses(tmp_path, monkeypatch):
    """Each lookup is a paced Slack call made while the frame holds a session
    slot. An outsider's line mentioning 500 people costs MENTIONED of them,
    the rest named as someone, and one already held costs nothing; an
    allowed id, which is of the household, is looked up outside that cap; a
    member's mentions are all looked up, the turn's own included, and so are
    the readers, the posters shown and her user id, never her bot id."""
    many = [f"UX{i:03d}" for i in range(500)]
    twelve = [f"UM{i:02d}" for i in range(12)]
    history = [{"user": "U3", "ts": f"{AT - 180:.1f}", "text": "<@U5> said so"},
               {"user": "U3", "ts": f"{AT - 120:.1f}", "text": "<@UK1> " + " ".join(f"<@{u}>" for u in many)},
               {"user": "U1", "ts": f"{AT - 60:.1f}", "text": " ".join(f"<@{u}>" for u in twelve)}]
    slack = ConversationSlack(members=["U1", "U2", "U6", "UBOT"], history=history)
    slack.held["UK1"] = {"profile": {"display_name": "kim"}}
    runner = RecordingRunner()
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    p.household = Household.load(store, ["U1", "U2", "U5"])
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "who are they, <@UT1>?", channel_type="mpim", channel="G1")))
    assert sorted(slack.asked) == sorted(["U3", "U6", "UBOT", "U5", "UT1", *twelve, *many[:main.MENTIONED]])
    first = runner.calls[0][0]
    assert "In a group direct message that U6 (outside the household), fan, mei and I read." in first
    assert "16:38 “jane” (outside the household): @“kim” (outside the household) @UX000 (outside the household)" in first
    assert f"@{many[main.MENTIONED]}" not in first and "@someone (outside the household) @someone" in first
    assert "16:37 “jane” (outside the household): @U5 said so\n" in first
    assert "16:39 fan: @UM00 (outside the household) @UM01 (outside the household)" in first
    assert "fan now says:\n\n    who are they, @UT1 (outside the household)?\n" in first


def test_a_thread_of_lines_mentioning_thousands_is_framed_at_once(tmp_path, monkeypatch):
    """The frame holds a session slot, and the event loop with it, while it
    picks which mentions to look up: 49 lines of 3,000 ids each take one
    pass."""
    lines = [{"user": "U3", "ts": f"{AT - 3000 + i:.1f}",
              "text": " ".join(f"<@UX{i:02d}{j:04d}>" for j in range(3000))} for i in range(49)]
    history = [{"user": "U1", "ts": f"{AT - 4000:.1f}", "text": "the plan"}, *lines]
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=history)
    runner = RecordingRunner()
    p, _, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    turn = dm(f"{AT:.1f}", "what was all that?", channel_type="mpim", channel="G1", thread=f"{AT - 4000:.1f}")
    started = time.monotonic()
    asyncio.run(p._memory_arrival(turn.payload, [turn.payload], datetime.fromtimestamp(AT, p.cfg.zone)))
    assert time.monotonic() - started < 1
    assert len(slack.asked) == 2 + main.MENTIONED


def test_a_frame_reads_back_twelve_hours_until_the_households_lines_are_read(tmp_path, monkeypatch):
    """The history read stops by the test the frame's window counts by: a
    member's line or hers, never anyone else's, an app's, her note or a
    join."""
    from wanda.vault import NOTE_EVENT

    slack = ConversationSlack(members=["U1", "U2", "UBOT"])
    p, _, _ = memory_processor(tmp_path, slack, RecordingRunner(), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "dinner at 7", channel_type="mpim", channel="G1")))
    (channel, thread, since, counted), = slack.fetched
    assert (channel, thread, since) == ("G1", None, AT - 12 * 3600)
    at = f"{AT - 60:.1f}"
    assert counted({"user": "U1", "ts": at}) and counted({"user": "UBOT", "bot_id": "BME", "ts": at})
    assert not any(counted(m) for m in (
        {"user": "U3", "ts": at}, {"user": "U7", "bot_id": "B7", "ts": at}, {"bot_id": "B8", "ts": at},
        {"user": "UBOT", "bot_id": "BME", "ts": at, "metadata": {"event_type": NOTE_EVENT}},
        {"user": "U1", "ts": at, "subtype": "channel_join"}))


@pytest.mark.parametrize("thread", [None, f"{AT - 300:.1f}"], ids=["unthreaded", "in a thread"])
def test_a_1_1_dm_names_everyone_as_slack_shows_them(tmp_path, monkeypatch, thread):
    """Only she and the member post in a 1:1 DM, so its frames mark no one,
    in a thread there too, under one of her alerts say."""
    history = [{"user": "U1", "ts": f"{AT - 120:.1f}", "text": "is <@U3> coming?"}]
    runner = RecordingRunner()
    p, _, _ = memory_processor(tmp_path, ConversationSlack(history=history), runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "ask <@U3>", thread=thread)))
    first = runner.calls[0][0]
    assert "16:38 fan: is @jane coming?" in first and "now says:\n\n    ask @jane\n" in first
    assert "outside the household" not in first


def test_her_failure_note_is_in_her_words_and_a_marked_one_is_shown_to_no_session(tmp_path, monkeypatch):
    """Her note where a message's turn failed is in her words, with no mark
    (the fakes take none), when first posted and when delivered later; Claude
    Code's reason goes to the alerts alone. A note posted with the mark, in
    Claude Code's words, which conversations still hold, is not shown to a
    session."""
    from wanda.vault import NOTE_EVENT

    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(ok=False), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert slack.replies == [main.FAILED]
    # one Slack refused at first, delivered later as it was
    refusing = Refusing(history=[])
    p, store, _ = memory_processor(tmp_path / "later", refusing, RecordingRunner(ok=False), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    p.slack = ConversationSlack()
    asyncio.run(p.deliver_pending())
    assert refusing.refused == p.slack.replies == [main.FAILED]
    # the next session is shown it as hers
    history = [{"user": "U1", "ts": f"{AT - 120:.1f}", "text": "what is the plumber's number?"},
               {"user": "U1", "ts": f"{AT:.1f}", "text": "and the electrician's?"}]
    runner = RecordingRunner(ok=False)
    p, _, _ = memory_processor(tmp_path / "next", Answering(history=history), runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "and the electrician's?")))
    asyncio.run(p.handle_slack(dm(f"{AT + 60:.1f}", "either will do")))
    assert f"    16:40 me: {main.FAILED}\n\nfan now says:\n\n    either will do" in runner.calls[-1][0]
    # and a marked note in the conversation is not shown to the next session
    runner = RecordingRunner()
    history = [{"user": "U1", "ts": f"{AT - 120:.1f}", "text": "what is the plumber's number?"},
               {"user": "UBOT", "bot_id": "BME", "ts": f"{AT - 60:.1f}",
                "text": "⚠️ my run failed: You've hit your limit",
                "metadata": {"event_type": NOTE_EVENT, "event_payload": {}}}]
    p, _, _ = memory_processor(tmp_path / "marked", ConversationSlack(history=history), runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "never mind, found it")))
    first = runner.calls[0][0]
    assert "16:38 fan: what is the plumber's number?" in first and "my run failed" not in first


def test_the_report_may_arrive_as_the_result_text(tmp_path, monkeypatch):
    slack = ConversationSlack()
    p, _, _ = memory_processor(tmp_path, slack, RecordingRunner(json.dumps(answer("Yes."))), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    assert slack.replies == ["Yes."]


@pytest.mark.parametrize("said,posted", [
    ({"recalled": [], "answer": "test", "recorded": []}, "test"),
    (answer("TBD"), "TBD"),
    ({"recalled": ["person:aaaaaa"], "answer": "The plumber comes at 5.", "recorded": ["placeholder"]},
     "The plumber comes at 5."),
    (FILLER, None),
], ids=["one word", "beside a real report", "a placeholder recorded", "nothing but filler"])
def test_a_one_word_answer_is_posted_and_filler_is_no_report(tmp_path, monkeypatch, said, posted):
    """A placeholder answer is hers unless `recalled` or `recorded` holds one
    too, when the session filled the schema with scaffolding and ended
    without its report, as its retry did too here; any other answer is hers
    whatever they hold."""
    slack = ConversationSlack()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(said, said), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "say test if you're up")))
    runs = [(r["status"], r["error"]) for r in store._query("SELECT status, error FROM runs WHERE kind = 'agent'")]
    if posted:
        assert slack.replies == [posted] and runs == [("ok", None)]
    else:
        assert runs == [("error", "the session ended without its report")] * 2
        assert slack.replies == [main.FAILED]


def test_an_answer_pings_no_group_and_hides_no_link(tmp_path, monkeypatch):
    """Rendered harmless before it is recorded, so it is posted so at once,
    kept so, and delivered so later."""
    said = "<!channel> dinner's at 7, <https://x.example/a|the menu>"
    shown = "@channel dinner's at 7, the menu (https://x.example/a)"
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer(said)), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "when's dinner?")))
    assert slack.replies == [shown]
    assert [r["result_text"] for r in store._query("SELECT result_text FROM runs")] == [shown]
    refusing = Refusing(history=[])
    p, store, _ = memory_processor(tmp_path / "later", refusing, RecordingRunner(answer(said)), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "when's dinner?")))
    p.slack = ConversationSlack()
    asyncio.run(p.deliver_pending())
    assert refusing.refused == [shown] and p.slack.replies == [shown]


@pytest.mark.parametrize("runner", [RecordingRunner("I filed it.", "I filed it."), RecordingRunner(ok=False)],
                         ids=["no report", "failed run"])
def test_a_session_that_reports_nothing_is_a_failure(tmp_path, monkeypatch, runner):
    slack = ConversationSlack()
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert len(runner.calls) == 2 and slack.replies == [main.FAILED]
    assert [(r["kind"], r["status"]) for r in store._query("SELECT kind, status FROM runs")] == [
        ("agent", "error"), ("agent", "error"), ("note", "ok")]


def test_a_session_no_one_asked_for_posts_nothing_when_it_fails_or_is_refused(tmp_path, monkeypatch):
    """And is not tried once more: the clock and the names keep their own
    tries."""
    slack = ConversationSlack()
    runner = RecordingRunner(ok=False)
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")
    now = datetime.fromtimestamp(AT, p.cfg.zone)
    got = asyncio.run(p.memory_turn(task, "an arrival no one asked for", now, channel="D1", reply_thread=None,
                                    owed=False))
    assert got == "claude reported an error" and slack.replies == [] and store.pending_deliveries() == []
    assert len(runner.calls) == 1 and store.get_meta("failed_runs") is None
    store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                     cost_usd=p.cfg.daily_cost_cap_usd, status="ok")
    assert asyncio.run(p.memory_turn(task, "x", now, channel="D1", reply_thread=None, owed=False)) == "breaker"
    assert slack.replies == []


def said_by_claude(error: str, subtype: str = "success", **kw) -> RunResult:
    """A session Claude Code ended in error, in its own words."""
    return RunResult(ok=False, envelope={"type": "result", "subtype": subtype, "is_error": True, "result": error},
                     error=error, **kw)


def failed_runs(store) -> list[tuple]:
    return [(r["kind"], r["status"], r["error"]) for r in store._query("SELECT kind, status, error FROM runs")]


@pytest.mark.parametrize("retry,posted", [(answer("Yes."), ["Yes."]), (answer(""), [])], ids=["answers", "silent"])
def test_a_failed_session_is_tried_once_more_by_one_told_of_it(tmp_path, monkeypatch, retry, posted):
    """Holding the slot, as its retry, named by the first's id, which a
    direct message with nothing before it says on the opening line every
    other frame has. The retry's outcome alone is posted, a silence
    included, and the first's run owes nothing."""
    runner = RecordingRunner("I filed it.", retry)
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    (first, a), (second, _) = runner.calls
    assert "fan says to me, in a direct message:\n\n    hello" in first
    assert (f"In a direct message that fan and I read. {vault.RETRIED.format(sid8=a['session_id'][:8])}\n\n"
            "fan says:\n\n    hello") in second
    assert slack.replies == posted and store.pending_deliveries() == []
    assert failed_runs(store) == [("agent", "error", "the session ended without its report"), ("agent", "ok", None)]
    assert store.get_meta("failed_runs") is None


def test_a_retry_whose_runner_raises_gets_her_note(tmp_path, monkeypatch):
    class Raising(RecordingRunner):
        async def run(self, prompt, **kw):
            if self.calls:
                self.calls.append((prompt, kw))
                raise RuntimeError("the claude binary is gone")
            return await super().run(prompt, **kw)

    runner = Raising("I filed it.")
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert len(runner.calls) == 2 and slack.replies == [main.FAILED]
    assert failed_runs(store) == [("agent", "error", "the session ended without its report"),
                                  ("agent", "error", "an internal error: the claude binary is gone"),
                                  ("note", "ok", None)]
    # named by the session that failed, as a retry that fails in its session is
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert (failed["id"], failed["why"], failed["said"], failed["then"]) == (
        1, "other", "the session ended without its report", "tried once more, a note asked for it again")


@pytest.mark.parametrize("retry,why,said,then", [
    (RunResult(ok=False, timed_out=True, error="timed out after 420s"), "timeout", "timed out after 420s",
     "a note asked for it again"),
    (said_by_claude("You've hit your limit · resets 5pm", api_error="rate_limit"), "usage limit",
     "Claude Code said: You've hit your limit · resets 5pm", "held until Claude Code runs again"),
], ids=["out of time", "refused"])
def test_a_retry_that_fails_otherwise_than_its_first_try_is_alerted_under_its_own_class(tmp_path, monkeypatch,
                                                                                       retry, why, said, then):
    """A retry that ran out of time, or that Claude Code refused, is named by
    itself, in its own words, under its own class, where a token to renew or
    a limit is read; the first try it retried is named by its run. The
    refused one holds its message."""
    runner = RecordingRunner("I filed it.", retry)
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    keep(store, dm(f"{AT:.1f}", "hello"))
    settle(p, p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert len(runner.calls) == 2 and slack.replies == ([main.FAILED] if why == "timeout" else [])
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert (failed["id"], failed["why"], failed["said"], failed["then"]) == (2, why, said, f"the retry of run 1, {then}")


@pytest.mark.parametrize("channel_type,thread,note", [("im", None, main.FAILED), ("mpim", None, main.FAILED_GROUP),
                                                      ("mpim", "77.1", main.FAILED_GROUP)],
                         ids=["a DM", "a group DM", "a thread in a group DM"])
def test_a_session_that_fails_twice_gets_her_note(tmp_path, monkeypatch, caplog, channel_type, thread, note):
    """In her words, with no mark (the fakes take none), chosen by the
    conversation's kind, which a reply in a thread of a group DM shares; the
    log says what became of each session."""
    import logging

    runner = RecordingRunner(said_by_claude("error_during_execution", "error_during_execution"),
                             said_by_claude("error_during_execution", "error_during_execution"))
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "dinner at 7?", channel_type=channel_type, thread=thread)))
    assert slack.replies == [note] and slack.threads == [thread]
    assert [r.getMessage().split("0 recorded, ")[1] for r in caplog.records
            if r.getMessage().startswith("memory session ")] == [
        "failed: error_during_execution; trying once more", "failed: error_during_execution; a note asks for it again"]


def test_the_failed_alert_names_each_class_once_a_day_with_claude_codes_reason(tmp_path, monkeypatch):
    """By run and time, never where, after `Claude Code said:` where the
    words are Claude Code's; a class alerted that day holds back no other,
    and its next waits for the next day, none dropped meanwhile."""
    def failing():
        return said_by_claude("error_during_execution", "error_during_execution")
    runner = RecordingRunner(failing(), failing(), RunResult(ok=False, timed_out=True, error="timed out after 420s"),
                             failing(), failing())
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    asyncio.run(p._flush_failed())
    assert len(slack.alerts) == 1 and re.fullmatch(
        r"1 message session\(s\) failed \(other\): run 1 at \d\d:\d\d, Claude Code said: error_during_execution, "
        r"tried once more, a note asked for it again", slack.alerts[0])
    asyncio.run(p.handle_slack(dm(f"{AT + 60:.1f}", "and this")))
    asyncio.run(p._flush_failed())
    assert len(slack.alerts) == 2 and re.fullmatch(
        r"1 message session\(s\) failed \(timeout\): run 4 at \d\d:\d\d, timed out after 420s, not tried again, "
        r"a note asked for it again", slack.alerts[1])
    asyncio.run(p.handle_slack(dm(f"{AT + 120:.1f}", "and that")))
    asyncio.run(p._flush_failed())
    assert len(slack.alerts) == 2
    store.set_meta("failed_alert_date:other", "2026-01-01")  # the next day
    asyncio.run(p._flush_failed())
    assert len(slack.alerts) == 3 and slack.alerts[2].startswith("1 message session(s) failed (other): run 6 at ")
    assert json.loads(store.get_meta("failed_runs")) == [] and not any("D1" in a for a in slack.alerts)


@pytest.mark.parametrize("ended,status,why", [
    (RunResult(ok=False, timed_out=True, error="timed out after 420s"), "timeout", "timeout"),
    (said_by_claude("error_max_budget_usd", "error_max_budget_usd"), "error", "other"),
    (RunResult(ok=False, error="could not read the session's output: a result without is_error"), "error", "other"),
], ids=["a timeout", "its budget spent", "an output it cannot read"])
def test_a_failure_a_second_session_would_meet_gets_her_note_at_once(tmp_path, monkeypatch, ended, status, why):
    """Not tried once more; her note counts toward no daily cap."""
    runner = RecordingRunner(ended, answer("Yes."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert len(runner.calls) == 1 and slack.replies == [main.FAILED]
    assert [r[:2] for r in failed_runs(store)] == [("agent", status), ("note", "ok")]
    assert store.runs_today(p.cfg.zone)[0] == 1
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert failed["why"] == why and failed["then"] == "not tried again, a note asked for it again"


@pytest.mark.parametrize("ended,why", [
    (said_by_claude("You've hit your limit · resets 5pm", api_error="rate_limit"), "usage limit"),
    (said_by_claude("Usage limit reached ∙ resets at 5pm"), "usage limit"),
    (said_by_claude("Login expired · Please run /login"), "authentication"),
    (said_by_claude("OAuth token revoked · Please run /login"), "authentication"),
    (said_by_claude("You've hit your limit · resets 5pm", api_error="rate_limit", api_errors=["rate_limit"],
                    results=[{"type": "result", "subtype": "success", "is_error": True,
                              "result": "You've hit your limit · resets 5pm"}]), "usage limit"),
], ids=["rate_limit", "Usage limit reached", "Login expired", "OAuth token revoked", "refused at its one result"])
def test_a_session_claude_code_refused_holds_its_message_with_nothing_said(tmp_path, monkeypatch, caplog, ended,
                                                                           why):
    """Not tried once more, and no note yet: its message is held, its
    reaction on, the session that ran nothing counted toward no daily cap,
    and the hold begins; its line in the log ends "; held". Refused at its
    first result, nothing of it ran, so the held row names no session."""
    import logging

    runner = RecordingRunner(ended, answer("Yes."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    keep(store, dm(f"{AT:.1f}", "hello"))
    with caplog.at_level(logging.INFO, logger="wanda"):
        settle(p, p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert [r.getMessage().endswith(f"failed: {ended.error}; held") for r in caplog.records
            if r.getMessage().startswith("memory session ")] == [True]
    assert len(runner.calls) == 1 and slack.replies == [] and slack.unreacted == []
    assert [r[:2] for r in failed_runs(store)] == [("agent", "refused")]
    assert kept(store) == [(f"{AT:.1f}", "held", 0, None)]
    assert store.runs_today(p.cfg.zone)[0] == 0 and store.get_meta("held_since")
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert failed["why"] == why and failed["then"] == "not tried again, held until Claude Code runs again"


def test_a_first_try_past_half_its_time_gets_her_note_at_once(tmp_path, monkeypatch):
    """A second would hold the slot as long again."""
    class Slow(RecordingRunner):
        async def run(self, prompt, **kw):
            await asyncio.sleep(0.6)
            return await super().run(prompt, **kw)

    runner = Slow("I filed it.", answer("Yes."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 1)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert len(runner.calls) == 1 and slack.replies == [main.FAILED]


@pytest.mark.parametrize("error", ["Context limit reached · /compact or /clear to continue",
                                   "API Error: 500 request req_240157 failed"])
def test_any_other_failure_is_tried_once_more(tmp_path, monkeypatch, error):
    runner = RecordingRunner(said_by_claude(error), answer("Yes."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert len(runner.calls) == 2 and slack.replies == ["Yes."]


def test_an_internal_error_in_a_turn_gets_her_note(tmp_path, monkeypatch):
    """Anything that raises before the turn's outcome is recorded."""
    def broken(out):
        raise KeyError("answer")
    monkeypatch.setattr("wanda.vault.answer", broken)
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("Yes.")), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?", channel_type="mpim")))
    assert slack.replies == [main.FAILED_GROUP]
    assert failed_runs(store) == [("agent", "error", "an internal error: 'answer'"), ("note", "ok", None)]


def test_a_message_whose_handling_fails_before_its_turn_gets_her_note(tmp_path, monkeypatch):
    """As when the run store takes no write: her note, posted with no run.
    An email task's thread keeps its own text."""
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(), monkeypatch)

    def full(*a, **kw):
        raise sqlite3.OperationalError("database or disk is full")
    line = dm(f"{AT:.1f}", "dinner at 7?", channel_type="mpim")
    keep(store, line)
    monkeypatch.setattr(store, "create_task", full)
    asyncio.run(p.handle_slack(line))
    assert slack.replies == [main.FAILED_GROUP] and store._query("SELECT * FROM runs") == []
    assert kept(store) == [], "answered by her note, not run again at the next start"
    slack = ConversationSlack()
    p, store, _ = memory_processor(tmp_path / "email", slack, RecordingRunner(), monkeypatch)
    store.create_task(None, "C1", "5.5", kind="email")

    async def raises(task, payload, state):
        raise RuntimeError("boom")
    monkeypatch.setattr(p, "_run_task_reply", raises)
    asyncio.run(p.handle_slack(Event(source="slack", dedupe_key="C1:6.6", payload={
        "kind": "task", "channel": "C1", "task_key": "5.5", "reply_thread": "5.5", "user": "U1", "text": "x",
        "ts": "6.6"})))
    assert slack.replies == ["⚠️ I hit an internal error handling that reply."]


def turn_result(out=None, subtype="error_during_execution", said=None):
    """One turn's result: a report, or an error of `subtype`, in Claude
    Code's words `said` when it gave some."""
    if out is not None:
        return {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(out),
                "structured_output": out}
    return {"type": "result", "subtype": subtype, "is_error": True, **({"result": said} if said else {})}


def ended(results, api_errors=None):
    """A streamed session that gave `results`, one a turn, as the runner
    reads it."""
    last = results[-1]
    ok = not last.get("is_error")
    return RunResult(ok=ok, envelope=last, structured=last.get("structured_output"), result_text=last.get("result"),
                     error=None if ok else last.get("result") or last["subtype"], results=results,
                     api_errors=api_errors or [])


class Taking(RecordingRunner):
    """Its first session is handed the next message added to its
    conversation, once one waits, before it ends as it is given (`took`
    holds what it was handed); the rest report as RecordingRunner's do."""

    def __init__(self, *reports):
        super().__init__(*reports)
        self.took: list[str] = []

    async def run(self, prompt, **kw):
        if not self.calls and (feed := kw.get("feed")) is not None:
            await until(lambda: feed.waiting, "a message added while it works")
            self.took.append(await feed.next())
        return await super().run(prompt, **kw)


def added_while_it_works(p, runner, *texts):
    """fan's first line, and his second added while its session works."""
    async def go():
        first = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", texts[0])))
        await until(lambda: runner.calls or p._in_turn, "the first turn")
        await p.handle_slack(dm(f"{AT + 30:.1f}", texts[1]))
        await first
        await reactions_end(p)
    asyncio.run(go())


def test_a_failed_turn_before_one_that_answers_is_not_run_again(tmp_path, monkeypatch):
    """A later turn of the session answered after it, and saw what it was
    begun by: it is kept for the alert as answered by a later turn. A turn a
    background command's notice began then failed, which is logged and
    alerted alone."""
    results = [turn_result(answer("Noted.")), turn_result(), turn_result(answer("And the plumber's at 5.")),
               turn_result()]
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [
        vault.Turn(True, []), vault.Turn(True, ["two"]), vault.Turn(True, ["three"]), vault.Turn(False, [])])
    runner = RecordingRunner(RunResult(ok=False, envelope=results[-1], error="error_during_execution",
                                       results=results))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "one")))
    assert len(runner.calls) == 1 and slack.replies == ["And the plumber's at 5."]
    assert failed_runs(store) == [("agent", "ok", "error_during_execution")] and p._waiting[1] == []
    assert [f["then"] for f in json.loads(store.get_meta("failed_runs"))] == [
        "answered by a later turn", "not tried again, no note"]


@pytest.mark.parametrize("api_errors,why", [([], "other"), (["rate_limit", None], "usage limit")],
                         ids=["a failure", "a refusal"])
def test_a_first_turn_that_failed_before_a_second_answered_is_told_beside_the_answer(tmp_path, monkeypatch,
                                                                                     caplog, api_errors, why):
    """The answer is posted, the run is an answer's, and the failure is kept
    for the alert once, under its own class, as answered by a later turn."""
    import logging

    said = "You've hit your limit · resets 5pm" if api_errors else None
    rr = ended([turn_result(said=said), turn_result(answer("Yes, at 5."))], api_errors)
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [vault.Turn(True, []), vault.Turn(True, ["two"])])
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(rr), monkeypatch)
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is the plumber coming?")))
    assert slack.replies == ["Yes, at 5."] and failed_runs(store) == [("agent", "ok", None)]
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert (failed["why"], failed["then"]) == (why, "answered by a later turn")
    [line] = session_lines(caplog)
    assert line.endswith(f", 10 characters to post, an earlier turn failed: {said or 'error_during_execution'}")
    assert store.get_meta("held_since") is None


@pytest.mark.parametrize("shape", ["no transcript", "its message deleted"])
def test_an_earlier_failed_turn_is_noted_only_beside_a_posted_answer_to_a_members_turn(tmp_path, monkeypatch,
                                                                                      caplog, shape):
    """With no transcript no turn is known to be a member's, and with its
    message deleted the answer is posted nowhere: either way no failed turn
    is kept for the alert as answered by a later turn, nor named on the
    line; with no transcript, a later turn that said nothing is not named
    either."""
    import logging

    results = [turn_result(), turn_result(answer("Yes, at 5."))]
    if shape == "no transcript":
        results.append(turn_result(answer("")))
    runner = RecordingRunner(ended(results))
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: None if shape == "no transcript" else [
        vault.Turn(True, []), vault.Turn(True, ["two"])])
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    keep(store, line := dm(f"{AT:.1f}", "is the plumber coming?"))
    if shape == "its message deleted":
        run = runner.run

        async def deleted_while_it_runs(prompt, **kw):
            await p.handle_slack(deletion(f"{AT:.1f}"))
            return await run(prompt, **kw)
        monkeypatch.setattr(runner, "run", deleted_while_it_runs)
    with caplog.at_level(logging.INFO, logger="wanda"):
        settle(p, p.handle_slack(line))
    assert p.slack.replies == ([] if shape == "its message deleted" else ["Yes, at 5."])
    assert store.get_meta("failed_runs") is None
    [said] = session_lines(caplog)
    assert said.endswith(", 10 characters, not posted: its messages were deleted" if shape == "its message deleted"
                         else ", 10 characters to post")


@pytest.mark.parametrize("failure,api_errors,then", [
    (turn_result(), [], "again"),
    (turn_result(subtype="error_max_budget_usd"), [], "rest"),
    (turn_result(said="You've hit your limit · resets 5pm"), [None, "rate_limit", None], "held"),
    (turn_result(said="Login expired · Please run /login"), [], "held"),
    (turn_result(said="API Error: 500 · Please run /login"), [None, "server_error", None], "again"),
], ids=["a failure", "its budget spent", "a usage limit", "a refusal in its words", "another error"])
def test_a_member_turn_that_failed_after_the_answer_is_answered_though_a_notice_reported_after_it(
        tmp_path, monkeypatch, caplog, failure, api_errors, then):
    """fan's line added after her answer began a turn that failed, and a
    background command's notice then began one that reported: her answer is
    posted, and his line is run again as the next turn, or gets FAILED_REST
    when a second session would spend its budget too, or is held when Claude
    Code refused that turn, as its own result says."""
    import logging

    runner = Taking(None, answer("Done, I'll call at 5."))
    runner.reports[0] = ended([turn_result(answer("Yes, at 5.")), failure, turn_result(answer(""))], api_errors)
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [
        vault.Turn(True, []), vault.Turn(True, list(runner.took)), vault.Turn(False, [])])
    monkeypatch.setattr("wanda.vault.handed", lambda v, sid: list(runner.took))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    keep(store, dm(f"{AT:.1f}", "is the plumber coming?"))
    keep(store, dm(f"{AT + 30:.1f}", "and can you call him?"))
    with caplog.at_level(logging.INFO, logger="wanda"):
        added_while_it_works(p, runner, "is the plumber coming?", "and can you call him?")
    assert len(runner.took) == 1
    error = failure.get("result") or failure["subtype"]
    if then == "again":
        assert slack.replies == ["Yes, at 5.", "Done, I'll call at 5."] and len(runner.calls) == 2
        assert vault.RETRIED.format(sid8=runner.calls[0][1]["session_id"][:8]) in runner.calls[1][0]
        assert kept(store) == []
    elif then == "rest":
        assert slack.replies == ["Yes, at 5.", main.FAILED_REST] and len(runner.calls) == 1
        assert kept(store) == []
    else:
        assert slack.replies == ["Yes, at 5."] and len(runner.calls) == 1
        assert [r[:2] for r in kept(store)] == [(f"{AT + 30:.1f}", "held")]
    follow = {"again": "run again as the next turn", "rest": "a note asks for it again", "held": "held"}[then]
    lines = session_lines(caplog)
    assert lines[0].endswith(f", 10 characters to post, then failed: {error}; {follow}")
    assert failed_runs(store)[0] == ("agent", "ok", error)


@pytest.mark.parametrize("first,api_errors", [
    (turn_result(), []),
    (turn_result(said="You've hit your limit · resets 5pm"), ["rate_limit", None, None, None]),
], ids=["a failure", "a refusal"])
def test_a_notices_answer_after_a_failed_member_turn_is_posted_and_a_later_failed_one_run_again(tmp_path,
                                                                                               monkeypatch,
                                                                                               caplog, first,
                                                                                               api_errors):
    """fan's first turn failed, a background command's notice then began a
    turn that answered, his added line's turn failed, and another notice's
    turn reported nothing: her answer is posted, his added line run again as
    the next turn, read from its own turn's result whatever the first turn's
    failure, and the first turn kept for the alert as answered by a later
    turn."""
    import logging

    runner = Taking(None, answer("Done, I'll call at 5."))
    runner.reports[0] = ended([first, turn_result(answer("The plumber's at 5.")), turn_result(),
                               turn_result(answer(""))], api_errors)
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [
        vault.Turn(True, []), vault.Turn(False, []), vault.Turn(True, list(runner.took)), vault.Turn(False, [])])
    monkeypatch.setattr("wanda.vault.handed", lambda v, sid: list(runner.took))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    with caplog.at_level(logging.INFO, logger="wanda"):
        added_while_it_works(p, runner, "is the plumber coming?", "and can you call him?")
    assert slack.replies == ["The plumber's at 5.", "Done, I'll call at 5."] and len(runner.calls) == 2
    assert [f["then"] for f in json.loads(store.get_meta("failed_runs"))] == [
        "answered by a later turn", "run again as the next turn"]
    assert session_lines(caplog)[0].endswith(
        ", 19 characters to post, then failed: error_during_execution; run again as the next turn, "
        f"an earlier turn failed: {first.get('result') or first['subtype']}")


def test_a_notices_turn_that_failed_after_the_answer_is_logged_and_alerted_alone(tmp_path, monkeypatch, caplog):
    """Another notice's turn reported after it: nothing more is said, and the
    failure is the log's and the alert's."""
    import logging

    rr = ended([turn_result(answer("Yes, at 5.")), turn_result(), turn_result(answer(""))])
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [
        vault.Turn(True, []), vault.Turn(False, []), vault.Turn(False, [])])
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(rr), monkeypatch)
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is the plumber coming?")))
    assert slack.replies == ["Yes, at 5."] and failed_runs(store) == [("agent", "ok", "error_during_execution")]
    assert session_lines(caplog)[0].endswith(", 10 characters to post, then failed: error_during_execution")
    asyncio.run(p._flush_failed())
    [alert] = slack.alerts
    assert alert.endswith(", Claude Code said: error_during_execution, not tried again, no note")


def test_a_notices_turn_that_failed_between_two_member_turns_is_not_told(tmp_path, monkeypatch, caplog):
    """The later member's turn answered: nothing is kept for the alert."""
    import logging

    rr = ended([turn_result(answer("Noted.")), turn_result(), turn_result(answer("Yes, at 5."))])
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [
        vault.Turn(True, []), vault.Turn(False, []), vault.Turn(True, ["two"])])
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(rr), monkeypatch)
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is the plumber coming?")))
    assert slack.replies == ["Yes, at 5."] and failed_runs(store) == [("agent", "ok", None)]
    assert store.get_meta("failed_runs") is None
    assert session_lines(caplog)[0].endswith(", 10 characters to post")


def test_a_member_turn_after_the_answer_that_said_nothing_is_marked_on_the_line(tmp_path, monkeypatch, caplog):
    """Her first answer is posted; the line says a later turn said nothing,
    for whoever reads the week's sessions to judge whether it needed a
    reply."""
    import logging

    rr = ended([turn_result(answer("Yes, at 5.")), turn_result(answer(""))])
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [vault.Turn(True, []), vault.Turn(True, ["two"])])
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(rr), monkeypatch)
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is the plumber coming?")))
    assert slack.replies == ["Yes, at 5."] and store.get_meta("failed_runs") is None
    assert session_lines(caplog)[0].endswith(", 10 characters to post, a later turn said nothing")


def test_a_member_turn_refused_after_a_notices_failure_is_held_by_its_own_result(tmp_path, monkeypatch):
    """After her answer a notice's turn failed, fan's added line's turn was
    refused, and another notice's turn reported nothing: his line is held,
    as its own turn's result says, not run again as the notice's failure
    would have it; the hold names this session, whose turns ran first."""
    runner = Taking(None)
    runner.reports[0] = ended([turn_result(answer("Yes, at 5.")), turn_result(),
                               turn_result(said="You've hit your limit · resets 5pm"), turn_result(answer(""))],
                              [None, None, "rate_limit", None])
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [
        vault.Turn(True, []), vault.Turn(False, []), vault.Turn(True, list(runner.took)), vault.Turn(False, [])])
    monkeypatch.setattr("wanda.vault.handed", lambda v, sid: list(runner.took))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    keep(store, dm(f"{AT:.1f}", "is the plumber coming?"))
    keep(store, dm(f"{AT + 30:.1f}", "and can you call him?"))
    added_while_it_works(p, runner, "is the plumber coming?", "and can you call him?")
    assert slack.replies == ["Yes, at 5."] and len(runner.calls) == 1
    sid = runner.calls[0][1]["session_id"]
    assert kept(store) == [(f"{AT + 30:.1f}", "held", 0, sid)]
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert (failed["why"], failed["then"]) == ("usage limit", "not tried again, held until Claude Code runs again")


class Refusing(ConversationSlack):
    """A Slack that takes no post."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.refused = []

    async def reply(self, thread_ts, text, channel=None):
        self.refused.append(text)
        raise RuntimeError("ratelimited")


def test_a_post_slack_refuses_is_not_a_failure(tmp_path, monkeypatch):
    """The run stays owed, is delivered later, and `memory_turn` returns None."""
    slack = Refusing()
    p, store, _ = memory_processor(
        tmp_path, slack, RecordingRunner(answer("The plumber is at 5."), answer("Yes.")), monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")
    now = datetime.fromtimestamp(AT, p.cfg.zone)
    got = asyncio.run(p.memory_turn(task, "an arrival no one asked for", now, channel="D1", reply_thread=None,
                                    owed=False))
    assert got is None and [r["result_text"] for r in store.pending_deliveries()] == ["The plumber is at 5."]
    # a message's answer the same way, with no post about an internal error;
    # its turn tries the answer already owed there before its frame, and
    # behind that one, refused again, its own is not tried
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    assert slack.refused == ["The plumber is at 5.", "The plumber is at 5."]
    assert [r["result_text"] for r in store.pending_deliveries()] == ["The plumber is at 5.", "Yes."]
    # the cap's refusal, with nothing kept to hold, posts nothing and
    # returns its verdict
    store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                     cost_usd=p.cfg.daily_cost_cap_usd, status="ok")
    assert asyncio.run(p.memory_turn(task, "x", now, channel="D1", reply_thread=None, owed=True)) == "breaker"
    assert len(slack.refused) == 2
    p.slack = ConversationSlack()
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == ["The plumber is at 5.", "Yes."] and store.pending_deliveries() == []


class Posting(ConversationSlack):
    """A Slack that takes no post while it is down, and shows what it took
    in the conversation as hers."""

    def __init__(self, **kw):
        super().__init__(history=[], **kw)
        self.down = False
        self.refused = []

    async def reply(self, thread_ts, text, channel=None):
        if self.down:
            self.refused.append(text)
            raise RuntimeError("ratelimited")
        await super().reply(thread_ts, text, channel)
        self.history.append({"user": "UBOT", "bot_id": "BME", "ts": f"{AT + 90 + len(self.history):.1f}",
                             "text": text})


def test_a_follow_up_framed_after_slack_is_back_sees_the_owed_answer_as_hers(tmp_path, monkeypatch):
    """Its turn posts what is owed there before its frame, which then shows
    it among what she said, and its own answer follows it."""
    slack = Posting()
    runner = RecordingRunner(answer("The plumber is at 5."), answer("Yes, paid on the 3rd."))
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    slack.down = True
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "when is the plumber?")))
    slack.down = False
    asyncio.run(p.handle_slack(dm(f"{AT + 60:.1f}", "and is it paid?")))
    assert slack.replies == ["The plumber is at 5.", "Yes, paid on the 3rd."] and store.pending_deliveries() == []
    assert "me: The plumber is at 5." in runner.calls[1][0]


def test_the_owed_runs_a_turn_posts_before_its_frame_stop_at_the_first_slack_refuses(tmp_path, monkeypatch):
    slack = Refusing()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("Yes.")), monkeypatch)
    owed(store, "The plumber is at 5.")
    owed(store, "And it's paid.")
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    assert slack.refused == ["The plumber is at 5."]
    assert [r["result_text"] for r in store.pending_deliveries()] == ["The plumber is at 5.", "And it's paid.",
                                                                      "Yes."]


def test_an_answer_behind_one_still_owed_is_left_for_the_pass_to_post_after_it(tmp_path, monkeypatch):
    """Slack back while the session ran: its answer is not posted before the
    one its turn found refused; the mail loop, woken, posts both in order."""
    slack = Posting()

    class Back(RecordingRunner):
        async def run(self, prompt, **kw):
            slack.down = False
            return await super().run(prompt, **kw)

    p, store, _ = memory_processor(tmp_path, slack, Back(answer("Yes.")), monkeypatch)
    owed(store, "The plumber is at 5.")
    slack.down = True
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    assert slack.refused == ["The plumber is at 5."] and slack.replies == []
    assert p.queue.qsize() == 1, "the mail loop is woken"
    asyncio.run(p.deliver_pending())
    assert slack.replies == ["The plumber is at 5.", "Yes."]


@pytest.mark.parametrize("reaches", ["its try", "_post_run"])
def test_a_pass_mid_post_and_a_turn_there_post_each_run_once(tmp_path, monkeypatch, reaches):
    """The mail loop's pass, posting slowly, and a message's turn in the same
    conversation, which reaches its try before its frame, or its own post,
    while the pass is mid-post: each run is posted once, in its order."""
    class Slow(ConversationSlack):
        async def reply(self, thread_ts, text, channel=None):
            self.posting.set()
            await asyncio.sleep(0.2)
            await super().reply(thread_ts, text, channel)

    slack = Slow()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("Noted.")), monkeypatch)
    before = ["an older answer", "Yes."] if reaches == "its try" else []
    for text in before:
        owed(store, text)

    async def go():
        slack.posting = asyncio.Event()
        if reaches == "its try":
            redelivery = asyncio.create_task(p.deliver_pending())
            await slack.posting.wait()
            await p.handle_slack(dm(f"{AT:.1f}", "noted?"))
        else:
            turn = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "noted?")))
            await slack.posting.wait()
            redelivery = asyncio.create_task(p.deliver_pending())
            await turn
        await redelivery
    asyncio.run(go())
    assert slack.replies == before + ["Noted."] and store.pending_deliveries() == []


def test_a_group_dm_names_its_readers(tmp_path, monkeypatch):
    runner = RecordingRunner()
    p, _, _ = memory_processor(tmp_path, ConversationSlack(members=["U1", "U2", "UBOT"]), runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "dinner at 7", channel_type="mpim")))
    assert "In a group direct message that fan, mei and I read. Everyone in it sees what I say there." \
        in runner.calls[0][0]


def test_a_member_is_answered_where_someone_outside_reads(tmp_path, monkeypatch, caplog):
    """Someone outside the household in a group keeps no member's message
    from its session: each runs, framed with them marked, and is answered."""
    import logging

    runner = RecordingRunner(answer("Seven works."), answer("Noted."))
    slack = ConversationSlack(members=["U1", "U3", "UBOT"], history=[])
    p, _, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    with caplog.at_level(logging.INFO, logger="wanda"):
        for i in range(2):
            asyncio.run(p.handle_slack(dm(f"{AT + i:.1f}", "dinner at 7", channel_type="mpim")))
    assert len(runner.calls) == 2 and slack.replies == ["Seven works.", "Noted."]
    for prompt, _ in runner.calls:
        assert ("In a group direct message that fan, “jane” (outside the household) and I read. Everyone in it "
                "sees what I say there.\n\n") in prompt
    assert "not taking part" not in caplog.text and p._outside == set()


def test_a_public_channel_says_when_anyone_in_the_workspace_is_outside(tmp_path, monkeypatch):
    """Anyone in this Slack can open a public channel, so its frame says that
    some who can read it are outside the household once anyone in the
    workspace is, and the session runs either way."""
    runner = RecordingRunner()
    slack = ConversationSlack(members=["U1", "UBOT"])
    p, _, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "<@UBOT> note this", channel_type="channel", channel="C1")))
    slack.people = slack.people + [{"id": "U3"}]
    asyncio.run(p.handle_slack(dm(f"{AT + 1:.1f}", "<@UBOT> and this", channel_type="channel", channel="C2")))
    (first, _), (second, _) = runner.calls
    opening = "In a public Slack channel that anyone in this Slack can read; fan and I are in it."
    assert f"{opening}\n\n" in first
    assert f"{opening} Some who can read it are outside the household.\n\n" in second


class NoWorkspace(ConversationSlack):
    async def workspace(self):
        raise RuntimeError("ratelimited")


def test_who_can_read_is_said_as_far_as_slack_says(tmp_path, monkeypatch, caplog):
    """A deactivated account reads nothing, so a public channel in a Slack of
    the household alone has no mark and no sentence; a reader outside the
    household brings the sentence whether or not the workspace holds them,
    as it does not hold a Slack Connect one; a Slack whose people cannot be
    read may hold anyone, which the frame says; a member list that cannot be
    read is said after that; a reader Slack will not describe is named by its
    id, outside the household."""
    import logging

    public = "In a public Slack channel that anyone in this Slack can read; fan and I are in it."
    outside = f"{public} Some who can read it are outside the household."
    gone = {"id": "U9", "deleted": True}
    household = ConversationSlack(members=["U1", "U9", "UBOT"], workspace=[{"id": "U1"}, {"id": "U2"}, gone])
    household.held["U9"] = gone
    cases = [
        (household, "channel", f"{public}\n\n"),
        (ConversationSlack(members=["U1", "U3", "UBOT"]), "channel",
         "In a public Slack channel that anyone in this Slack can read; fan, “jane” (outside the household) and I are "
         "in it. Some who can read it are outside the household.\n\n"),
        (NoWorkspace(members=["U1", "UBOT"]), "channel", f"{outside}\n\n"),
        (ConversationSlack(members=None, workspace=[{"id": "U1"}, {"id": "U3"}]), "channel",
         f"{outside} I could not find out who else is in it.\n\n"),
        (ConversationSlack(members=["U1", "U4", "UBOT"]), "mpim",
         "In a group direct message that U4 (outside the household), fan and I read. Everyone in it sees what I "
         "say there.\n\n"),
    ]
    for i, (slack, kind, opening) in enumerate(cases):
        runner = RecordingRunner()
        p, _, _ = memory_processor(tmp_path / str(i), slack, runner, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="wanda"):
            asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "<@UBOT> note this", channel_type=kind, channel="C1")))
        assert len(runner.calls) == 1 and opening in runner.calls[0][0], i
    assert "could not read who is in this Slack for C1: ratelimited" in caplog.text


def test_a_session_runs_when_slack_will_not_say_who_reads(tmp_path, monkeypatch, caplog):
    """Told so, naming the turn's speakers, and answered; no failure note,
    and the reason logged."""
    import logging

    runner = RecordingRunner(answer("Seven it is."))
    slack = ConversationSlack(members=None)
    p, _, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="wanda"):
        asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "dinner at 7", channel_type="mpim")))
    assert "could not read who is in D1: missing_scope" in caplog.text
    unlisted = "Everyone in it sees what I say there. I could not find out who else is in it.\n\n"
    assert f"In a group direct message that fan and I read. {unlisted}" in runner.calls[0][0]
    assert slack.replies == ["Seven it is."]
    turn = [dm(f"{AT + i:.1f}", text, channel_type="mpim", user=u).payload
            for i, u, text in ((1, "U1", "dinner at 7?"), (2, "U2", "or 8"))]
    framed = asyncio.run(p._memory_arrival(turn[1], turn, datetime.fromtimestamp(AT + 2, p.cfg.zone)))
    assert framed.startswith(f"In a group direct message that fan, mei and I read. {unlisted}")
    # a 1:1 DM's reader is the member who wrote, and Slack is not asked
    framed = asyncio.run(p._memory_arrival(dm(f"{AT:.1f}", "hi").payload, [dm(f"{AT:.1f}", "hi").payload],
                                           datetime.fromtimestamp(AT, p.cfg.zone)))
    assert framed.startswith("In a direct message that fan and I read.\n\n")


def test_a_frame_that_cannot_be_built_posts_a_note_and_runs_nothing(tmp_path, monkeypatch):
    """Whatever else raises while the frame is built, her note is posted, and
    what failed is kept for the alert."""
    class Broken(ConversationSlack):
        async def users(self, ids):
            raise RuntimeError("boom")

    runner = RecordingRunner()
    slack = Broken(members=["U1", "U2", "UBOT"])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "dinner at 7", channel_type="mpim")))
    assert runner.calls == [] and slack.replies == [main.FAILED_GROUP]
    assert store.pending_deliveries() == []
    assert [(r["kind"], r["error"]) for r in store._query("SELECT kind, error FROM runs")] == [
        ("agent", "could not gather what was said here: boom"), ("note", None)]
    asyncio.run(p._flush_failed())
    assert slack.alerts[-1].endswith(", could not gather what was said here: boom, not tried again, a note asked "
                                     "for it again")


class Held(RecordingRunner):
    """Holds each session open until released, its turn begun, counting how
    many run at once; `started` has each one's prompt as it starts."""

    def __init__(self, *reports):
        super().__init__(*reports)
        self.release = asyncio.Event()
        self.running = self.most = 0
        self.started = []

    async def run(self, prompt, **kw):
        if kw.get("feed") is not None:
            kw["feed"].began()
        self.started.append(prompt)
        self.running += 1
        self.most = max(self.most, self.running)
        await self.release.wait()
        self.running -= 1
        return await super().run(prompt, **kw)


def test_a_burst_is_one_turn(tmp_path, monkeypatch):
    """Messages that arrive while a conversation's session runs are taken by
    one session: the newest as the message, the others before it."""
    runner = Held()
    p, _, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)

    async def burst():
        first = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "can you remind me")))
        await asyncio.sleep(0.05)
        rest = [asyncio.create_task(p.handle_slack(dm(f"{AT + i:.1f}", text)))
                for i, text in ((10, "to call the plumber"), (20, "on tuesday"))]
        await asyncio.sleep(0.05)
        runner.release.set()
        await asyncio.gather(first, *rest)
    asyncio.run(burst())
    assert len(runner.calls) == 2
    second = runner.calls[1][0]
    assert "    16:40 fan: to call the plumber\n\nfan now says:\n\n    on tuesday" in second


def test_a_turn_with_both_of_them_in_it_names_both(tmp_path, monkeypatch):
    """fan's request and mei's later line in one turn: the closing line names
    fan too, so the request is never read back as mei's."""
    runner = Held()
    p, _, _ = memory_processor(tmp_path, ConversationSlack(members=["U1", "U2", "UBOT"], history=[]),
                               runner, monkeypatch)

    async def turn():
        first = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "dinner at 7", channel_type="mpim")))
        await asyncio.sleep(0.05)
        rest = [asyncio.create_task(p.handle_slack(dm(f"{AT + i:.1f}", text, channel_type="mpim", user=who)))
                for i, who, text in ((10, "U1", "remind me at 5 tomorrow"), (20, "U2", "I'm out then anyway"))]
        await asyncio.sleep(0.05)
        runner.release.set()
        await asyncio.gather(first, *rest)
    asyncio.run(turn())
    assert len(runner.calls) == 2
    assert ("    16:40 fan: remind me at 5 tomorrow\n\nmei now says, after fan:\n\n    I'm out then anyway"
            in runner.calls[1][0])


def test_a_message_deleted_while_it_waits_is_withdrawn(tmp_path, monkeypatch):
    runner = Held()
    p, _, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)

    async def go():
        first = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "one")))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(p.handle_slack(dm(f"{AT + 5:.1f}", "sent to the wrong person")))
        await asyncio.sleep(0.05)
        await p.handle_slack(Event(source="slack", dedupe_key="x", payload={
            "kind": "deleted", "channel": "D1", "ts": f"{AT + 5:.1f}"}))
        runner.release.set()
        await asyncio.gather(first, second)
    asyncio.run(go())
    assert len(runner.calls) == 1


class Answering(ConversationSlack):
    """A Slack whose history takes each post, as Slack's does, a second after
    the newest message in it."""

    async def reply(self, thread_ts, text, channel=None):
        await super().reply(thread_ts, text, channel)
        newest = max(float(m["ts"]) for m in self.history)
        self.history.append({"user": "UBOT", "bot_id": "BME", "ts": f"{newest + 1:.1f}", "text": text})


def test_a_line_added_while_a_session_ran_is_framed_after_its_answer(tmp_path, monkeypatch):
    """The next turn takes the added line once the first turn's answer is
    posted, and is shown that answer: the question it answered would
    otherwise look unanswered to the session taking the follow-up."""
    history = [{"user": "U1", "ts": f"{AT:.1f}", "text": "what time is the dentist on Tuesday?"},
               {"user": "U1", "ts": f"{AT + 10:.1f}", "text": "and can you remind me the day before?"}]
    slack = Answering(history=history)
    runner = Held(answer("10:30, at Dr Ruiz's."), answer("Will do."))
    p, _, _ = memory_processor(tmp_path, slack, runner, monkeypatch)

    async def burst():
        first = asyncio.create_task(p.handle_slack(dm(history[0]["ts"], history[0]["text"])))
        await asyncio.sleep(0.05)
        added = asyncio.create_task(p.handle_slack(dm(history[1]["ts"], history[1]["text"])))
        await asyncio.sleep(0.05)
        runner.release.set()
        await asyncio.gather(first, added)
    asyncio.run(burst())
    assert slack.replies == ["10:30, at Dr Ruiz's.", "Will do."]
    assert ("The conversation so far:\n\n    16:40 fan: what time is the dentist on Tuesday?\n"
            "    16:40 me: 10:30, at Dr Ruiz's.\n\nfan now says:\n\n    and can you remind me the day before?"
            ) in runner.calls[1][0]


def one_slot_behind_a_dm(slack, tmp_path, monkeypatch):
    """A processor with one session at a time, and a session of mei's DM
    holding it until released."""
    runner = Held()
    runner.agent_sem = asyncio.Semaphore(1)
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    return p, store, runner, dm(f"{AT - 5:.1f}", "out Tuesday", channel="D2", user="U2")


def test_who_reads_a_turn_is_who_is_there_when_its_session_starts(tmp_path, monkeypatch):
    """A turn can wait a whole session of another conversation for its slot.
    Who is in the conversation is read once it has the slot: someone invited
    meanwhile is named, marked, and the answer is posted; someone who left
    meanwhile is not named."""
    for i, (later, readers) in enumerate(((["U1", "U2", "U3", "UBOT"], "fan, mei, “jane” (outside the household)"),
                                          (["U1", "UBOT"], "fan"))):
        slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
        p, store, runner, meis = one_slot_behind_a_dm(slack, tmp_path / str(i), monkeypatch)
        runner.reports = [answer("Noted."), answer("The blue one.")]

        async def wait_for_the_slot():
            first = asyncio.create_task(p.handle_slack(meis))
            await asyncio.sleep(0.05)
            group = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "what did we decide about the house?",
                                                          channel_type="mpim", channel="G1")))
            await asyncio.sleep(0.05)
            slack.member_ids = later
            runner.release.set()
            await asyncio.gather(first, group)
        asyncio.run(wait_for_the_slot())
        assert "out Tuesday" in runner.calls[0][0] and len(runner.calls) == 2
        assert f"In a group direct message that {readers} and I read." in runner.calls[1][0]
        assert slack.replies == ["Noted.", "The blue one."] and slack.channels == ["D2", "G1"]


def test_a_turn_takes_its_messages_when_its_session_starts(tmp_path, monkeypatch):
    """A message sent while the turn waits for its slot is in that turn, and
    one deleted meanwhile is withdrawn from it."""
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, _, runner, meis = one_slot_behind_a_dm(slack, tmp_path, monkeypatch)

    async def go():
        first = asyncio.create_task(p.handle_slack(meis))
        await asyncio.sleep(0.05)
        group = [asyncio.create_task(p.handle_slack(dm(f"{AT + i:.1f}", text, channel_type="mpim", channel="G1")))
                 for i, text in ((0, "dinner at 7?"), (10, "sent to the wrong group"), (20, "or 8"))]
        await asyncio.sleep(0.05)
        await p.handle_slack(Event(source="slack", dedupe_key="x", payload={
            "kind": "deleted", "channel": "G1", "ts": f"{AT + 10:.1f}"}))
        runner.release.set()
        await asyncio.gather(first, *group)
    asyncio.run(go())
    assert len(runner.calls) == 2
    assert "    16:40 fan: dinner at 7?\n\nfan now says:\n\n    or 8" in runner.calls[1][0]
    assert "wrong group" not in runner.calls[1][0]


def test_a_refusal_holds_every_message_waiting(tmp_path, monkeypatch, fake_time, caplog):
    """Two lines wait on the lock while a session works when the cap is
    reached: the turn the cap refuses before its frame keeps both, as a
    session would have taken them both, each with her reaction on and one
    note saying so; the cap's day out, a pass leaves them, and the first
    after midnight takes both up as one turn."""
    import logging

    caplog.set_level(logging.INFO, logger="wanda")
    fake_time.at = datetime.fromtimestamp(AT + 30, timezone.utc)
    runner = Held(answer("Noted."), answer("Both, then."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    lines = [dm(f"{AT + i:.1f}", text) for i, text in ((0, "one"), (10, "two"), (20, "three"))]
    for ev in lines:
        keep(store, ev)

    async def go():
        first = asyncio.create_task(p.handle_slack(lines[0]))
        await asyncio.sleep(0.05)
        rest = [asyncio.create_task(p.handle_slack(ev)) for ev in lines[1:]]
        await asyncio.sleep(0.05)
        store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                         cost_usd=p.cfg.daily_cost_cap_usd, status="ok")
        runner.release.set()
        await asyncio.gather(first, *rest)
        await reactions_end(p)
    asyncio.run(go())
    assert len(runner.calls) == 1 and slack.replies == ["Noted.", main.CAPPED_NOTE]
    assert [r[:2] for r in kept(store)] == [(f"{AT + 10:.1f}", "capped"), (f"{AT + 20:.1f}", "capped")]
    assert slack.unreacted == [("D1", f"{AT:.1f}")]
    a_pass(p)
    assert len(runner.calls) == 1 and caplog.text.count("the daily run cap keeps 2 message(s) in D1") == 1
    fake_time.at = datetime(2026, 10, 2, 7, 1, tzinfo=timezone.utc)  # 00:01 in Los Angeles
    a_pass(p)
    assert len(runner.calls) == 2 and "    16:40 fan: two\n\nfan now says:\n\n    three" in runner.calls[1][0]
    assert runner.calls[1][0].count("fan: two") == 1, "each kept line framed once"
    assert slack.replies[-1] == "Both, then." and kept(store) == []


def test_what_is_owed_is_posted_later_wherever_it_is_owed(tmp_path, monkeypatch):
    """An answer Slack refused at first is posted at a later pass, or at a
    start, which can be hours on, where it is owed, with no read of who is
    there by then: in a group someone outside reads, and where Slack would
    not say who is in it; and an email task's too."""
    slack = Refusing(members=["U1", "U3", "UBOT"], history=[])
    p, store, _ = memory_processor(
        tmp_path, slack, RecordingRunner(answer("The plumber is at 5."), answer("Yes.")), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "when is the plumber?", channel_type="mpim", channel="G1")))
    asyncio.run(p.handle_slack(dm(f"{AT + 1:.1f}", "is it paid?")))
    task = store.create_task(None, "C1", "5.5", kind="email")
    store.record_run(kind="agent", task_id=task, session_id=None, started_at=utcnow(), exit_code=0, cost_usd=0.0,
                     status="ok", result_text="Filed it.", notified=0)
    owed = ["The plumber is at 5.", "Yes.", "Filed it."]
    assert [r["result_text"] for r in store.pending_deliveries()] == owed
    # a member list read now would raise, and nothing reads a conversation's type
    p.slack = ConversationSlack(members=None)
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == owed and p.slack.channels == ["G1", "D1", "C1"]
    assert store.pending_deliveries() == []


def test_conversations_run_side_by_side(tmp_path, monkeypatch):
    runner = Held()
    p, _, _ = memory_processor(tmp_path, ConversationSlack(), runner, monkeypatch)

    async def both():
        tasks = [asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "one", channel=c, user=u)))
                 for c, u in (("D1", "U1"), ("D2", "U2"))]
        await asyncio.sleep(0.05)
        runner.release.set()
        await asyncio.gather(*tasks)
    asyncio.run(both())
    assert runner.most == 2


def test_a_turn_a_stop_cut_short_runs_again_once(tmp_path, monkeypatch):
    """Three lines, the stop cutting their turn short while its session
    works on the first: nothing is said there, and the next start runs all
    three in one turn, framed with the session cut short."""
    runner = Held()
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    lines = [dm(f"{AT + i:.1f}", f"line {i}") for i in range(3)]

    async def go():
        for ev in lines:
            keep(store, ev)
            t = asyncio.create_task(p.handle_slack(ev))
            p._bg.add(t)
            await asyncio.sleep(0.02)
        await p.shutdown(grace_s=1)
    asyncio.run(go())
    sid = kept(store)[0][3]
    assert slack.replies == [] and store.pending_deliveries() == [] and sid
    assert kept(store) == [(f"{AT:.1f}", "due", 1, sid), (f"{AT + 1:.1f}", "due", 0, None),
                           (f"{AT + 2:.1f}", "due", 0, None)]
    again = RecordingRunner(answer("Noted, all three."))
    started_again(p, again)
    [(prompt, _)] = again.calls
    assert f"In a direct message that fan and I read. {vault.RETRIED.format(sid8=sid[:8])}\n\n" in prompt
    assert "    16:40 fan: line 0\n    16:40 fan: line 1\n\nfan now says:\n\n    line 2" in prompt
    assert slack.replies == ["Noted, all three."] and kept(store) == []


def test_triage_off_leaves_mail_rows_alone(tmp_path):
    slack = FakeSlack()
    p, store = make(tmp_path, slack, email_triage=False)
    ingest_triaged(store, "k1", "attention")
    ingest_triaged(store, "k2", "shadow_trash", uid=2)
    store.set_message_status("k2", "acting")
    store.ingest_message(dedupe_key="k3", message_id="<k3>", folder="INBOX", uidvalidity=1, uid=3,
                         from_addr="a@x.example", subject="s", date_hdr="d", snippet="b")
    asyncio.run(p.startup_recovery())
    asyncio.run(p.drain_mail())
    assert slack.tasks == [] and slack.digests == []
    assert [store.get_message_by_key(k)["status"] for k in ("k1", "k2", "k3")] == ["triaged", "acting", "new"]


# --- a message added while its conversation's session works (tests/claude_standin.py
# replays the shapes the pinned CLI wrote for sessions with their input open) ---

STANDIN = Path(__file__).with_name("claude_standin.py")


def standin_processor(tmp_path, monkeypatch, slack=None, sessions=None, **behaviour):
    """A processor whose sessions run the stand-in for claude in a vault of
    the test's own, with the transcripts under the test's own home: each as
    `behaviour` has it, or each in turn as `sessions` does."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("STANDIN", json.dumps(sessions if sessions is not None else behaviour))
    fake = tmp_path / "claude"
    fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STANDIN}" "$@"\n')
    fake.chmod(0o755)
    slack = slack or ConversationSlack(history=[])
    p, store, snaps = memory_processor(tmp_path, slack, RunnerService(str(fake), agent_sem=asyncio.Semaphore(1)),
                                       monkeypatch)
    (tmp_path / "vault").mkdir(exist_ok=True)
    return p, store, snaps, slack


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


def conversation(p, *messages):
    """Each (moment, event) handled as the watcher would, the first at once."""
    async def go():
        tasks = []
        for at, ev in messages:
            await moment(at, p.cfg.vault_dir)
            tasks.append(asyncio.create_task(p.handle_slack(ev)))
        await asyncio.gather(*tasks)
    asyncio.run(go())


def handed_texts(tmp_path):
    out = []
    for f in vault.transcripts_dir(tmp_path / "vault").glob("*.jsonl"):
        out.append(vault.handed(tmp_path / "vault", f.stem))
    return out


def test_a_message_added_while_its_session_works_gets_the_one_answer(tmp_path, monkeypatch):
    p, store, snaps, slack = standin_processor(tmp_path, monkeypatch, steps=[1.0, 0.2])
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_use", 1, 0.1), dm(f"{AT + 30:.1f}", "to call the plumber")))
    assert slack.replies == ["one answer to 2: can you remind me at 5 | to call the plumber"]
    assert len(snaps) == 1 and len(store._query("SELECT * FROM runs")) == 1
    # framed with who added it, where, when, and that only the last answer is sent
    assert handed_texts(tmp_path) == [[vault.added_text("dm", "fan", "to call the plumber", "16:40")]]


def test_one_added_after_the_answer_was_written_still_gets_one_post(tmp_path, monkeypatch):
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5)
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber")))
    assert slack.replies == ["one answer to 2: can you remind me at 5 | to call the plumber"]
    assert store._query("SELECT cost_usd FROM runs")[0]["cost_usd"] == 0.02


def test_one_its_session_was_not_handed_is_answered_by_the_next_turn(tmp_path, monkeypatch):
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, on_eof="drop")
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber")))
    assert slack.replies == ["one answer to 1: can you remind me at 5", "one answer to 1: to call the plumber"]


def test_past_the_limit_a_message_waits_for_the_next_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "FOLD_LIMIT", 1)
    p, _, _, slack = standin_processor(tmp_path, monkeypatch, steps=[1.2, 0.2])
    conversation(p, (0, dm(f"{AT:.1f}", "one")), (("tool_use", 1, 0.1), dm(f"{AT + 10:.1f}", "two")),
                 (0.1, dm(f"{AT + 20:.1f}", "three")))
    assert slack.replies == ["one answer to 2: one | two", "one answer to 1: three"]


def test_a_failed_session_that_was_handed_an_addition_posts_one_note(tmp_path, monkeypatch):
    """Tried once more, both messages in the retry's frame, and failing
    again: one note for both."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[1.0, 0.2], fail="first")
    conversation(p, (0, dm(f"{AT:.1f}", "one")), (("tool_use", 1, 0.1), dm(f"{AT + 10:.1f}", "two")))
    assert slack.replies == [main.FAILED]
    assert [(r["kind"], r["status"]) for r in store._query("SELECT kind, status FROM runs")] == [
        ("agent", "error"), ("agent", "error"), ("note", "ok")]
    assert store.pending_deliveries() == []


def test_mei_added_to_fans_question_is_framed_as_hers(tmp_path, monkeypatch):
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, _, _, slack = standin_processor(tmp_path, monkeypatch, slack=slack, steps=[1.0, 0.2])
    conversation(p, (0, dm(f"{AT:.1f}", "dinner at 7?", channel_type="mpim")),
                 (("tool_use", 1, 0.1), dm(f"{AT + 60:.1f}", "I'm out till 8", channel_type="mpim", user="U2")))
    assert slack.replies == ["one answer to 2: dinner at 7? | I'm out till 8"]
    assert handed_texts(tmp_path) == [[vault.added_text("group", "mei", "I'm out till 8", "16:41")]]


@pytest.mark.parametrize("channel_type,said",
                         [("im", "@jane too"), ("mpim", "@“jane” (outside the household) too")])
def test_a_message_added_while_its_session_works_names_whom_it_mentions_as_its_frame_does(
        tmp_path, monkeypatch, channel_type, said):
    """In a 1:1 DM by the names Slack shows, elsewhere quoted and marked."""
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, _, _, slack = standin_processor(tmp_path, monkeypatch, slack=slack, steps=[1.0, 0.2])
    conversation(p, (0, dm(f"{AT:.1f}", "who's coming?", channel_type=channel_type)),
                 (("tool_use", 1, 0.1), dm(f"{AT + 30:.1f}", "<@U3> too", channel_type=channel_type)))
    place = "dm" if channel_type == "im" else "group"
    assert handed_texts(tmp_path) == [[vault.added_text(place, "fan", said, "16:40")]]


def test_a_session_no_one_messaged_takes_in_nothing(tmp_path, monkeypatch):
    """A clock session's frame says no message started it, and it owes
    nobody: a message in that DM waits for the session that follows."""
    runner = Held(answer("Morning."))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    tid = store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")

    async def go():
        async def clock():
            async with p._task_locks.setdefault(tid, asyncio.Lock()):
                await p.memory_turn(task, "(a look)", datetime.fromtimestamp(AT, p.cfg.zone),
                                    channel="D1", reply_thread=None, owed=False)
        looking = asyncio.create_task(clock())
        await asyncio.sleep(0.05)
        message = asyncio.create_task(p.handle_slack(dm(f"{AT + 5:.1f}", "morning!")))
        await asyncio.sleep(0.05)
        runner.release.set()
        await asyncio.gather(looking, message)
    asyncio.run(go())
    (_, look), (_, reply) = runner.calls
    assert look["feed"] is None and isinstance(reply["feed"], Additions)


def test_a_stop_while_a_session_holds_an_addition_leaves_both_due(tmp_path, monkeypatch):
    """The first taken by the session, which the stop cut short, and the
    second written to its input and not yet taken in: nothing is said or
    owed there, and both are due for the next start, the second with neither
    the session nor the try its write counted, which a next start would
    otherwise frame as a session that saw it."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[30])

    async def go():
        for at, ev in ((0, dm(f"{AT:.1f}", "one")), (("tool_use", 1, 0.3), dm(f"{AT + 10:.1f}", "two"))):
            await moment(at, p.cfg.vault_dir)
            keep(store, ev)
            t = asyncio.create_task(p.handle_slack(ev))
            p._bg.add(t)
        await asyncio.sleep(0.5)
        taken.extend(len(more.taken) for more in p._additions.values())
        await p.shutdown(grace_s=5)
    taken = []
    asyncio.run(go())
    assert taken == [1]  # written to the session's input when the stop came
    [run] = store._query("SELECT session_id, status, notified FROM runs")
    assert (run["status"], run["notified"]) == ("cancelled", 1) and store.pending_deliveries() == []
    assert kept(store) == [(f"{AT:.1f}", "due", 1, run["session_id"]), (f"{AT + 10:.1f}", "due", 0, None)]
    asyncio.run(p.deliver_pending())
    assert slack.replies == []


def test_a_restart_during_a_further_turn_delivers_the_answer_already_given(tmp_path, monkeypatch):
    """The first turn has answered, and a follow-up written after its last
    step runs a further turn, when the daemon stops: the answer is the run's,
    delivered at the next start, and answers the first message alone; the
    follow-up is run again then, framed with the session cut short."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.0, steps_later=[30])

    async def go():
        for at, ev in ((0, dm(f"{AT:.1f}", "can you remind me at 5")),
                       (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber"))):
            await moment(at, p.cfg.vault_dir)
            keep(store, ev)
            t = asyncio.create_task(p.handle_slack(ev))
            p._bg.add(t)
        # the further turn's tool call has begun
        await moment(("tool_use", 2, 0.2), p.cfg.vault_dir)
        await p.shutdown(grace_s=5)
    asyncio.run(go())
    [run] = [dict(r) for r in store._query("SELECT id, session_id, status, notified, result_text FROM runs")]
    sid = run["session_id"]
    assert run == {"id": 1, "session_id": sid, "status": "ok", "notified": 0,
                   "result_text": "one answer to 1: can you remind me at 5"}
    assert [r[:2] + (r[3],) for r in kept(store)] == [(f"{AT:.1f}", "answered", sid), (f"{AT + 30:.1f}", "due", sid)]
    again = RecordingRunner(answer("At 5, then."))
    started_again(p, again, slack)
    assert slack.replies == ["one answer to 1: can you remind me at 5", "At 5, then."] and kept(store) == []
    [(prompt, _)] = again.calls
    assert vault.RETRIED.format(sid8=sid[:8]) in prompt and "fan says:\n\n    to call the plumber" in prompt


# --- every message kept until it is answered ---

def recorded_rows(store, monkeypatch):
    """What each record of a run leaves of the kept messages, as kept(store)
    has it, in order."""
    after, record, note = [], store.record_run, store.record_run_and_note

    def run(**kw):
        got = record(**kw)
        after.append(kept(store))
        return got

    def run_and_note(text, **kw):
        got = note(text, **kw)
        after.append(kept(store))
        return got
    monkeypatch.setattr(store, "record_run", run)
    monkeypatch.setattr(store, "record_run_and_note", run_and_note)
    return after


def test_a_record_answers_its_turns_message_and_one_its_session_was_handed(tmp_path, monkeypatch):
    """Both answered by its run, written with it, and gone once it is posted."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[1.0, 0.2])
    after = recorded_rows(store, monkeypatch)
    one, two = dm(f"{AT:.1f}", "one"), dm(f"{AT + 10:.1f}", "two")
    keep(store, one), keep(store, two)
    conversation(p, (0, one), (("tool_use", 1, 0.1), two))
    sid = store._query("SELECT session_id FROM runs")[0]["session_id"]
    assert after == [[(f"{AT:.1f}", "answered", 1, sid), (f"{AT + 10:.1f}", "answered", 1, sid)]]
    assert slack.replies == ["one answer to 2: one | two"] and kept(store) == []


def test_one_written_to_a_session_that_never_took_it_in_is_due_again_without_its_try(tmp_path, monkeypatch):
    """Not answered by the session's run: due again, the session and the try
    its write counted gone, and answered by the next turn."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, on_eof="drop")
    after = recorded_rows(store, monkeypatch)
    one, two = dm(f"{AT:.1f}", "can you remind me at 5"), dm(f"{AT + 30:.1f}", "to call the plumber")
    keep(store, one), keep(store, two)
    conversation(p, (0, one), (("tool_result", 1, 0.2), two))
    sid = store._query("SELECT session_id FROM runs ORDER BY id LIMIT 1")[0]["session_id"]
    assert after[0] == [(f"{AT:.1f}", "answered", 1, sid), (f"{AT + 30:.1f}", "due", 0, None)]
    assert slack.replies == ["one answer to 1: can you remind me at 5", "one answer to 1: to call the plumber"]
    assert kept(store) == []


@pytest.mark.parametrize("at", ["the lock", "the slot", "the frame", "the session"])
def test_a_cancel_at_each_await_of_a_turn_leaves_its_kept_message_as_it_was(tmp_path, monkeypatch, at):
    """Waiting for its conversation's turn or a session's place, framed, or
    with its session running, as a stop finds it: nothing is written of it."""
    class Stalled(ConversationSlack):
        async def fetch_context(self, channel, thread_ts, since, counted=None):
            await asyncio.Event().wait()

    runner = Held()
    if at == "the slot":
        runner.agent_sem = asyncio.Semaphore(0)
    slack = Stalled(history=[]) if at == "the frame" else ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)

    async def go():
        if at == "the lock":
            await p._task_locks.setdefault(store.create_task(None, "D1", "conversation", kind="dm"),
                                           asyncio.Lock()).acquire()
        turn = asyncio.create_task(p.handle_slack(line))
        await asyncio.sleep(0.1)
        before = kept(store)
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        return before
    before = asyncio.run(go())
    assert kept(store) == before
    [(ts, state, tries, sid)] = before
    # a session that began counts a try, and names itself
    assert (state, tries, bool(sid)) == (("due", 1, True) if at == "the session" else ("due", 0, False))
    assert store.pending_deliveries() == [] and slack.replies == []


@pytest.mark.parametrize("ending", ["a sender not let in", "a deletion", "a frame that fails", "an internal error",
                                    "an internal error before the frame"])
def test_a_message_nothing_will_answer_is_not_left_due(tmp_path, monkeypatch, ending):
    """Its sender not let in, or deleted while it waits, it is no longer
    kept; a turn whose frame fails, or that raises, answers it with her
    note, before its frame every message waiting with it."""
    runner = Held(answer("Noted."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    if ending == "a sender not let in":
        store._exec("DELETE FROM meta WHERE key='names:U2'")
        p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    if ending == "a frame that fails":
        async def broken(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(p, "_memory_arrival", broken)
    if ending == "an internal error":
        def broken(out):
            raise KeyError("answer")
        monkeypatch.setattr("wanda.vault.answer", broken)
    line = dm(f"{AT:.1f}", "is it paid?", user="U2" if ending == "a sender not let in" else "U1")
    keep(store, line)
    if ending == "an internal error before the frame":
        async def broken(task_id, channel):
            raise RuntimeError("the store went away")
        monkeypatch.setattr(p, "_post_owed", broken)
        first = dm(f"{AT - 5:.1f}", "one")
        keep(store, first)

        async def go():
            # both waiting when the turn begins
            lock = p._task_locks.setdefault(store.create_task(None, "D1", "conversation", kind="dm"),
                                            asyncio.Lock())
            await lock.acquire()
            turns = [asyncio.create_task(p.handle_slack(ev)) for ev in (first, line)]
            await asyncio.sleep(0.05)
            lock.release()
            await asyncio.gather(*turns)
    elif ending == "a deletion":
        # deleted while its turn waits behind one a session holds
        first = dm(f"{AT - 5:.1f}", "one")
        keep(store, first)

        async def go():
            turns = [asyncio.create_task(p.handle_slack(ev)) for ev in (first, line)]
            await asyncio.sleep(0.05)
            await p.handle_slack(Event(source="slack", dedupe_key="x", payload={
                "kind": "deleted", "channel": "D1", "ts": f"{AT:.1f}"}))
            runner.release.set()
            await asyncio.gather(*turns)
    else:
        async def go():
            runner.release.set()
            await p.handle_slack(line)
    asyncio.run(go())
    assert kept(store) == []
    assert slack.replies == {"a sender not let in": [], "a deletion": ["Noted."],
                             "a frame that fails": [main.FAILED], "an internal error": [main.FAILED],
                             "an internal error before the frame": [main.FAILED]}[ending]


def test_what_is_owed_raising_while_its_turn_waits_for_it_ends_the_turn_before_its_frame(tmp_path, monkeypatch):
    """The post of what is owed fails inside its bound: no session runs, and
    her note waits behind the answer still owed there."""
    runner = RecordingRunner(answer("Yes, it's paid."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.record_run(kind="agent", task_id=task, session_id="s", started_at=utcnow(), exit_code=0,
                     cost_usd=0.0, status="ok", result_text="An answer owed.", notified=0)

    async def broken(task_id=None):
        raise RuntimeError("the store went away")
    monkeypatch.setattr(p, "deliver_pending", broken)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)
    asyncio.run(p.handle_slack(line))
    assert runner.calls == []
    assert [r["result_text"] for r in store.pending_deliveries()] == ["An answer owed.", main.FAILED]


class HeldNote(ConversationSlack):
    """Holds each post of her note `note` until `gate` is set, as a slow
    Slack would; `waiting` once one is held."""

    def __init__(self, note=main.FAILED, **kw):
        super().__init__(**kw)
        self.note, self.gate, self.waiting = note, asyncio.Event(), False

    async def reply(self, thread_ts, text, channel=None):
        if text == self.note:
            self.waiting = True
            await self.gate.wait()
        await super().reply(thread_ts, text, channel)


def test_a_line_sent_while_her_note_on_a_failure_before_the_frame_posts_gets_its_own_turn(tmp_path, monkeypatch):
    """Her note answers what waited when the turn failed; a line that came
    while it was posted is no part of that, and its turn runs."""
    runner = RecordingRunner(answer("Yes, it's paid."))
    slack = HeldNote(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    real, calls = p._post_owed, []

    async def fails_once(task_id, channel):
        calls.append(task_id)
        if len(calls) == 1:
            raise RuntimeError("the store went away")
        await real(task_id, channel)
    monkeypatch.setattr(p, "_post_owed", fails_once)
    one, two = dm(f"{AT:.1f}", "one"), dm(f"{AT + 10:.1f}", "is it paid?")
    keep(store, one), keep(store, two)

    async def go():
        first = asyncio.create_task(p.handle_slack(one))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(p.handle_slack(two))
        await asyncio.sleep(0.05)
        slack.gate.set()
        await asyncio.gather(first, second)
    asyncio.run(go())
    assert slack.replies == [main.FAILED, "Yes, it's paid."] and len(runner.calls) == 1
    assert "fan says to me, in a direct message:\n\n    is it paid?" in runner.calls[0][0]
    assert kept(store) == []


class RefusesSaying(FakeSlack):
    """A Slack that takes no post saying `word`, refusing it with `error`."""

    def __init__(self, word, error):
        super().__init__()
        self.word, self.error = word, error

    async def reply(self, thread_ts, text, channel=None):
        if self.word in text:
            raise self.error
        await super().reply(thread_ts, text, channel)


@pytest.mark.parametrize("note", ["the cap's", "a hold's"])
def test_her_note_on_the_cap_or_a_hold_given_up_leaves_her_reaction_on_what_it_tells_of(tmp_path, fake_time, note):
    """Her note that she will come back to it, refused for two hours of her
    running: given up, with no note of its own, and what it told of stays
    kept, her reaction on. The alert says when, and nothing of a note,
    which is said of an answer to members' messages."""
    slack = RefusesSaying("come back", RuntimeError("ratelimited"))
    p, store = make(tmp_path, slack, email_triage=False)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    line = keep(store, dm(f"{fake_time.at.timestamp() - 60:.1f}", "is it paid?"))
    kept_as = Settled(capped=(line,)) if note == "the cap's" else Settled(held=((line, "s1"),))
    store.set_meta("held_since", (fake_time.at - timedelta(minutes=5)).isoformat())
    store.record_run(kind="note", task_id=task, session_id=None, started_at=utcnow(), exit_code=None, cost_usd=0.0,
                     status="ok", notified=0, settled=kept_as,
                     result_text=main.CAPPED_NOTE if note == "the cap's" else main.HELD)
    settle(p, p.deliver_pending())
    fake_time.at += timedelta(minutes=122)
    settle(p, p.deliver_pending())
    assert slack.replies == [] and not store.pending_deliveries(task) and slack.unreacted == []
    assert [r[1] for r in kept(store)] == ["capped" if note == "the cap's" else "held"]
    assert [g["how"] for g in json.loads(store.get_meta("given_up_runs"))] == ["after two hours"]


def test_her_note_after_an_answer_given_up_names_the_first_message_that_stands(tmp_path, fake_time):
    """An answer to three messages, the first deleted after the record,
    given up after two hours: her note stands for the second and the third,
    and names the second's time (GIVEN_UP)."""
    slack = RefusesSaying("plumber", RuntimeError("ratelimited"))
    p, store = make(tmp_path, slack, email_triage=False)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    at = fake_time.at.timestamp()
    first = keep(store, dm(f"{at - 600:.1f}", "when is the plumber?"))
    second = keep(store, dm(f"{at - 300:.1f}", "and is it paid?"))
    third = keep(store, dm(f"{at - 60:.1f}", "and the gate code?"))
    store.record_run(kind="agent", task_id=task, session_id="s1", started_at=utcnow(), exit_code=0, cost_usd=0.1,
                     status="ok", result_text="The plumber is at 5.", notified=0,
                     settled=Settled(answered=(first, second, third)))
    settle(p, p.drain_mail())

    async def gone():
        p._withdraw(*first)
    settle(p, gone())
    fake_time.at += timedelta(minutes=122)
    settle(p, p.drain_mail())
    settle(p, p.drain_mail())
    assert slack.replies == [main.GIVEN_UP.format(at=main.written_at(
        datetime.fromtimestamp(at - 300, timezone.utc), fake_time.at.astimezone(p.cfg.zone)))]


def test_an_answer_whose_messages_are_all_deleted_while_slack_refuses_it_is_given_up_quietly(tmp_path, fake_time):
    """An answer to two messages, both deleted while the post that fails at
    two hours is in flight: given up, its note recorded for the first and
    then not posted, as nothing it tells of stands."""
    at = fake_time.at.timestamp()

    class Deletes(RefusesSaying):
        async def reply(self, thread_ts, text, channel=None):
            if self.word in text and fake_time.at.timestamp() - at > 7200:
                for k in self.keys:
                    p._withdraw(*k)
            await super().reply(thread_ts, text, channel)
    slack = Deletes("plumber", RuntimeError("ratelimited"))
    p, store = make(tmp_path, slack, email_triage=False)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    first = keep(store, dm(f"{at - 600:.1f}", "when is the plumber?"))
    second = keep(store, dm(f"{at - 60:.1f}", "and is it paid?"))
    slack.keys = [first, second]
    store.record_run(kind="agent", task_id=task, session_id="s1", started_at=utcnow(), exit_code=0, cost_usd=0.1,
                     status="ok", result_text="The plumber is at 5.", notified=0,
                     settled=Settled(answered=(first, second)))
    settle(p, p.drain_mail())
    fake_time.at += timedelta(minutes=122)
    settle(p, p.drain_mail())
    settle(p, p.drain_mail())
    assert slack.replies == [] and not store.pending_deliveries(task) and kept(store) == []


ARCHIVED = SlackApiError("The request to the Slack API failed.", {"ok": False, "error": "is_archived"})


@pytest.mark.parametrize("given_up", ["after two hours, with her note after it", "after two hours, in a group DM",
                                      "at once", "at once, with her note after it", "her note, at once"])
def test_an_answer_given_up_after_two_hours_is_followed_by_her_note_saying_so(tmp_path, fake_time, given_up):
    """An answer to two of fan's messages, his third answered after it. Given
    up after two hours, her note after it dropped with it: GIVEN_UP, with
    the time of the first of the two, after the later answer there, holding
    both, which go, with her reaction, once it is posted. Given up at once,
    Slack would refuse a note there too: nothing is posted, and the messages
    go with their reaction left on, for good. A note given up is not
    replaced."""
    group = "group" in given_up
    at_once = "at once" in given_up
    slack = RefusesSaying("Sorry" if given_up.startswith("her note") else "plumber",
                          ARCHIVED if at_once else RuntimeError("ratelimited"))
    p, store = make(tmp_path, slack, email_triage=False)
    task = store.create_task(None, "G1" if group else "D1", "conversation", kind="dm")
    at = fake_time.at.timestamp()
    first, second, third = (keep(store, dm(f"{at - 120 + i:.1f}", text, channel_type="mpim" if group else "im",
                                           channel="G1" if group else "D1"))
                            for i, text in enumerate(("when is the plumber?", "and is it paid?", "thanks")))
    run = dict(kind="agent", task_id=task, session_id="s1", started_at=utcnow(), exit_code=0, cost_usd=0.1,
               status="ok", result_text="The plumber is at 5.", notified=0)
    if given_up.startswith("her note"):
        run |= {"status": "error", "result_text": None, "notified": 1}
    if "note" in given_up:
        answered, noted = store.record_run_and_note(main.FAILED_REST if "after it" in given_up else main.FAILED,
                                                    noted=Settled(answered=(first, second)), **run)
        if given_up.startswith("her note"):
            answered = noted
    else:
        answered = store.record_run(**run, settled=Settled(answered=(first, second)))
    store.record_run(kind="agent", task_id=task, session_id="s2", started_at=utcnow(), exit_code=0, cost_usd=0.1,
                     status="ok", result_text="Yes.", notified=0, settled=Settled(answered=(third,)))
    settle(p, p.drain_mail())
    if not at_once:
        assert slack.replies == [] and [r[0] for r in kept(store)] == [first[1], second[1], third[1]]
        assert slack.unreacted == []
        fake_time.at += timedelta(minutes=122)
        settle(p, p.drain_mail())
        assert [r[:2] for r in kept(store)] == [(first[1], "answered"), (second[1], "answered")]
        assert slack.unreacted == [third]
        settle(p, p.drain_mail())
        note = (main.GIVEN_UP_GROUP if group else main.GIVEN_UP).format(at="at 07:58")
        assert slack.replies == ["_(I wrote this at 08:00; it couldn't be sent until now.)_\nYes.", note]
        assert sorted(slack.unreacted) == [first, second, third]
    else:
        assert slack.replies == ["Yes."]
        settle(p, p.drain_mail())
        assert slack.unreacted == [third]
    assert kept(store) == [] and store.pending_deliveries() == []
    then = "after two hours, a note asked for it again" if not at_once else "at once (is_archived), no note"
    assert f"run {answered}, from 08:00, {then}. " in slack.alerts[0]


def test_her_note_given_up_after_two_hours_takes_her_reaction_off_its_message(tmp_path, fake_time):
    """Not replaced, and its message goes with nothing said; Slack refused
    that conversation for two hours, not for good."""
    slack = RefusesSaying("Sorry", RuntimeError("ratelimited"))
    p, store = make(tmp_path, slack, email_triage=False)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    line = keep(store, dm(f"{fake_time.at.timestamp() - 60:.1f}", "is it paid?"))
    store.record_run_and_note(main.FAILED, noted=Settled(answered=(line,)), kind="agent", task_id=task,
                              session_id="s1", started_at=utcnow(), exit_code=1, cost_usd=0.1, status="error")
    settle(p, p.drain_mail())
    assert kept(store)[0][:2] == (line[1], "answered") and slack.unreacted == []
    fake_time.at += timedelta(minutes=122)
    settle(p, p.drain_mail())
    assert slack.replies == [] and kept(store) == [] and slack.unreacted == [line]


def test_a_stops_marker_from_before_is_posted_as_her_note_or_the_email_tasks(tmp_path):
    """The text-less run a stop recorded for a message, before messages were
    kept: her note in a memory conversation, worded for everyone in a group
    DM, whose id is not a 1:1 DM's; an email task's own words."""
    p, store = make(tmp_path)
    for channel, kind in (("D1", "dm"), ("G1", "dm"), ("C1", "email")):
        task = store.create_task(None, channel, "5.5", kind=kind)
        store.record_run(kind="agent", task_id=task, session_id=None, started_at=utcnow(), exit_code=None,
                         cost_usd=0.0, status="cancelled", error="daemon shut down before completion", notified=0)
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == [main.CUT_SHORT, main.CUT_SHORT_GROUP,
                               "⏸ I restarted while working on this — reply again to retry."]


def cut_short_at_a_start(p, runner):
    """A start that runs again what was kept, stopped while each session
    works."""
    q = Processor(p.cfg, p.store, asyncio.Queue(), p.slack, runner)

    async def go():
        for task, keys in q.kept():
            q.take_up(task, keys)
        while not runner.running:
            await asyncio.sleep(0.01)
        await q.shutdown(grace_s=1)
    asyncio.run(go())


@pytest.mark.parametrize("channel_type, note", [("im", main.CUT_SHORT), ("mpim", main.CUT_SHORT_GROUP)],
                         ids=["a DM", "a group DM"])
def test_messages_two_stops_cut_short_get_her_note_once_in_place_of_a_third_run(tmp_path, monkeypatch, caplog,
                                                                               channel_type, note):
    """Taken by a session a stop cut short, they run again at the next
    start, framed with it; cut short again, the start after gives her note
    in their place, once for the conversation, and runs them no more."""
    import logging

    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(), monkeypatch)
    for i, text in enumerate(("one", "two")):
        keep(store, dm(f"{AT + i:.1f}", text, channel_type=channel_type))
    first, second = Held(), Held()
    cut_short_at_a_start(p, first)
    [(_, _, sid)] = {r[1:] for r in kept(store)}
    assert "An earlier session of mine" not in first.started[0]
    cut_short_at_a_start(p, second)
    assert vault.RETRIED.format(sid8=sid[:8]) in second.started[0]
    assert {r[1:3] for r in kept(store)} == {("due", 2)}
    third = RecordingRunner(answer("never"))
    with caplog.at_level(logging.INFO, logger="wanda"):
        q = started_again(p, third)
        asyncio.run(q.deliver_pending())
    assert third.calls == [] and slack.replies == [note] and kept(store) == []
    assert "run again after a stop: 0 message(s) in 0 conversation(s); 2 cut short twice, given a note" in caplog.text


def test_a_message_given_back_and_then_cut_short_runs_again_framed_with_both_sessions(tmp_path, monkeypatch):
    """A further turn begun by it failed after the answer, and the turn that
    ran it again was cut short by a stop: the next start runs it again,
    framed with each of those sessions, the oldest first."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, sessions=[
        {"steps": [0.2], "reply_s": 1.5, "fail": "later"}, {"hang": True}])
    asked, follow = dm(f"{AT:.1f}", "can you remind me at 5"), dm(f"{AT + 30:.1f}", "to call the plumber")

    async def go():
        for at, ev in ((0, asked), (("tool_result", 1, 0.2), follow)):
            await moment(at, p.cfg.vault_dir)
            keep(store, ev)
            t = asyncio.create_task(p.handle_slack(ev))
            p._bg.add(t)
        for _ in range(400):
            ran = store._query("SELECT session_id FROM runs ORDER BY id LIMIT 1")
            if ran and [r for r in kept(store) if r[3] not in (None, ran[0]["session_id"])]:
                break
            await asyncio.sleep(0.025)
        await p.shutdown(grace_s=5)
    asyncio.run(go())
    failed = store._query("SELECT session_id FROM runs ORDER BY id LIMIT 1")[0]["session_id"]
    [(ts, state, tries, cut)] = kept(store)
    assert (ts, state, tries) == (f"{AT + 30:.1f}", "due", 1) and cut != failed
    again = RecordingRunner(answer("At 5, then."))
    started_again(p, again, slack)
    [(prompt, _)] = again.calls
    assert f"{vault.RETRIED.format(sid8=failed[:8])} {vault.RETRIED.format(sid8=cut[:8])}\n\n" in prompt
    assert slack.replies == ["one answer to 1: can you remind me at 5", "At 5, then."] and kept(store) == []


def test_a_message_kept_a_week_runs_late_saying_when_she_was_not_running(tmp_path, monkeypatch, fake_time):
    fake_time.at = datetime.fromtimestamp(AT, timezone.utc) + timedelta(days=7, minutes=2)
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), RecordingRunner(), monkeypatch)
    monkeypatch.setattr(main, "LATE_TURN_S", LATE_TURN_S)
    keep(store, dm(f"{AT:.1f}", "is the plumber coming?"), "3f9a1c2e-cut-short")
    store.set_meta("down", json.dumps([[datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
                                        for t in (AT + 60, AT + 7 * 86400)]]))
    runner = RecordingRunner(answer("Yes, at 5."))
    started_again(p, runner)
    [(prompt, kw)] = runner.calls
    assert ("In a direct message that fan and I read. What fan says below was sent at Thu 2026-10-01 16:40 and "
            "reaches me only now. I was not running from Thu 2026-10-01 16:41 until 16:40. "
            f"{vault.RETRIED.format(sid8='3f9a1c2e')}\n\nfan says:\n\n    is the plumber coming?") in prompt
    assert "it is 16:42 here" in kw["append_system_prompt"] and kw["env"]["MEM_DATE"] == "2026-10-08"
    assert p.slack.replies == ["Yes, at 5."]


def a_start(tmp_path, monkeypatch, runner, slack, *, connect=(), dies=False,
            recovery=Processor.startup_recovery, loop=None, snapshot=None, watcher_stops=None):
    """A daemon start on the run store in `tmp_path`, as run_daemon makes
    it, against fakes: Slack is `slack`, each session runs on `runner`, and
    `connect` is what the watcher is handed as it connects, the watcher
    then being `slack.watcher`. It stops once what the start ran again has
    ended and the mail loop has had a pass, or dies once what was kept is
    taken up; or `loop` is the mail loop, and the test stops it. `snapshot`
    stands in for the vault's snapshots, and `watcher_stops` is called as
    the watcher is stopped."""
    slack.know_own_ids = lambda ids: None

    async def user_now(uid):
        return {"U1": {"profile": {"display_name": "fan"}}, "U2": {"profile": {"display_name": "mei"}}}[uid]
    slack.user_now = user_now

    def start(watcher):
        connected(watcher)
        slack.watcher = watcher
        for event in connect:
            watcher._handle(SimpleNamespace(send_socket_mode_response=lambda r: None),
                            SimpleNamespace(type="events_api", envelope_id="e", payload={"event": event}))

    async def one_pass(self):
        while self._bg:
            await asyncio.sleep(0.01)
        await self.drain_mail()
        os.kill(os.getpid(), signal.SIGTERM)

    async def dying(self):
        await asyncio.sleep(0.1)
        raise RuntimeError("the store went away")

    monkeypatch.setattr("wanda.main.SlackActions", lambda cfg, store: slack)
    monkeypatch.setattr("wanda.main.RunnerService", lambda claude_bin, **kw: runner)
    monkeypatch.setattr("wanda.main.SlackWatcher.start", start)
    monkeypatch.setattr("wanda.main.SlackWatcher.stop", lambda self: watcher_stops and watcher_stops())
    monkeypatch.setattr("wanda.main.Processor.loop", loop or one_pass)
    monkeypatch.setattr("wanda.main.Processor.startup_recovery", dying if dies else recovery)
    # framed at their own time, and her reaction made at once, as in
    # memory_processor
    monkeypatch.setattr(main, "LATE_TURN_S", 10 ** 9)
    monkeypatch.setattr(main, "LATE_ADD_S", 10 ** 9)
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: None)
    monkeypatch.setattr("wanda.vault.snapshot", snapshot or (lambda cfg, message: None))
    monkeypatch.setattr("wanda.vault.last_snapshot", lambda cfg: "none")
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)
    asyncio.run(main.run_daemon(start_config(tmp_path)))


def start_config(tmp_path) -> Config:
    return Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y", alert_channel="C9",
                  slack_owner_user_ids="U1,U2", tz="America/Los_Angeles", email_triage=False, claude_bin="/bin/true")


def test_a_conversations_first_message_kept_and_never_run_survives_starts_and_runs_once(tmp_path, monkeypatch):
    """Kept before a handler made its conversation's task: each start makes
    the task and takes it up; three that die with it waiting for a session
    spend no try, and the fourth runs it once."""
    store = Store(start_config(tmp_path).db_path)
    line = dm(f"{AT:.1f}", "hello?")
    keep(store, line)
    assert store.get_task_by_thread("D1", "conversation") is None
    for _ in range(3):
        runner = RecordingRunner()
        runner.agent_sem = asyncio.Semaphore(0)
        with pytest.raises(RuntimeError, match="the store went away"):
            a_start(tmp_path, monkeypatch, runner, ConversationSlack(history=[]), dies=True)
    assert store.get_task_by_thread("D1", "conversation") is not None
    assert kept(store) == [(f"{AT:.1f}", "due", 0, None)] and store._query("SELECT id FROM runs") == []
    runner, slack = RecordingRunner(answer("Hello!")), ConversationSlack(history=[])
    a_start(tmp_path, monkeypatch, runner, slack)
    assert len(runner.calls) == 1 and slack.replies == ["Hello!"] and kept(store) == []


def test_two_kept_messages_of_a_dm_are_one_frame_and_one_kept_as_she_connects_is_framed_once(
        tmp_path, monkeypatch, caplog):
    """Taken up at a start through the real slack_loop, the slot free: one
    session for both, the one a session took second in order still named
    with it; and a line that comes as the watcher connects, from the queue,
    is in one frame only."""
    import logging

    store = Store(start_config(tmp_path).db_path)
    keep(store, dm(f"{AT:.1f}", "first"))
    keep(store, dm(f"{AT + 10:.1f}", "second"), "7b20d4e1-cut-short")
    runner, slack = RecordingRunner(answer("Both noted."), answer("And that.")), ConversationSlack(history=[])
    later = {"type": "message", "user": "U1", "channel": "D1", "channel_type": "im", "ts": f"{AT + 20:.1f}",
             "text": "third"}
    with caplog.at_level(logging.INFO, logger="wanda"):
        a_start(tmp_path, monkeypatch, runner, slack, connect=[later])
    assert "run again after a stop: 2 message(s) in 1 conversation(s); 0 cut short twice, given a note" in caplog.text
    prompt = runner.calls[0][0]
    assert f"In a direct message that fan and I read. {vault.RETRIED.format(sid8='7b20d4e1')}\n\n" in prompt
    assert "    16:40 fan: first\n" in prompt and "    second" in prompt
    assert not any("fan: first" in prompt or "    second" in prompt for prompt, _ in runner.calls[1:])
    assert sum("third" in prompt for prompt, _ in runner.calls) == 1
    assert kept(store) == []


def test_two_take_ups_of_one_conversation_are_one_turn(tmp_path, monkeypatch):
    runner = RecordingRunner(answer("Noted."))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    key = keep(store, dm(f"{AT:.1f}", "is it paid?"))
    [(task, keys)] = p.kept()

    async def go():
        loop = asyncio.create_task(p.slack_loop())
        p.take_up(task, keys)
        p.take_up(task, keys)
        while p._bg:
            await asyncio.sleep(0.01)
        loop.cancel()
    asyncio.run(go())
    assert keys == [key] and len(runner.calls) == 1 and p.slack.replies == ["Noted."]


def test_a_kept_message_whose_sender_is_no_longer_let_in_is_not_run_again(tmp_path, monkeypatch):
    """The allowlist can change between two starts: a take-up passes each
    row through the check a new message passes, and one it refuses is no
    longer kept."""
    runner = RecordingRunner(answer("Noted."))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    keep(store, dm(f"{AT:.1f}", "is it paid?", user="U2"))
    store._exec("DELETE FROM meta WHERE key='names:U2'")
    started_again(p, runner)
    assert runner.calls == [] and kept(store) == []


def test_a_kept_message_deleted_while_its_take_up_waits_is_not_run(tmp_path, monkeypatch):
    runner = RecordingRunner(answer("Noted."))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    keep(store, dm(f"{AT:.1f}", "sent to the wrong person"))
    [(task, keys)] = p.kept()

    async def go():
        loop = asyncio.create_task(p.slack_loop())
        lock = p._task_locks.setdefault(task["id"], asyncio.Lock())
        await lock.acquire()  # a clock session in the DM, say
        p.take_up(task, keys)
        p.slack_queue.put_nowait(Event(source="slack", dedupe_key="x", payload={
            "kind": "deleted", "channel": "D1", "ts": f"{AT:.1f}"}))
        await asyncio.sleep(0.1)
        lock.release()
        while p._bg:
            await asyncio.sleep(0.01)
        loop.cancel()
    asyncio.run(go())
    assert runner.calls == [] and kept(store) == []


def test_doctor_names_a_message_due_longer_than_a_turn_takes(tmp_path, capsys):
    """Counted from the latest of when it was sent, when it was kept and the
    last start, which runs again what a stop left, so not at once after a
    start."""
    from wanda.main import run_doctor

    c = Config(_env_file=None, data_dir=tmp_path, claude_bin="/bin/true", email_triage=False)
    store = Store(c.db_path)
    sent = datetime.now(timezone.utc) - timedelta(hours=3)
    keep(store, dm(f"{sent.timestamp():.1f}", "is it paid?"))
    keep(store, dm(f"{sent.timestamp() + 1:.1f}", "thanks"))
    # kept as they were sent, live
    store._exec("UPDATE slack_events SET received_at=?", (sent.isoformat(timespec="seconds"),))
    store._exec("UPDATE unanswered SET state='answered' WHERE ts=?", (f"{sent.timestamp() + 1:.1f}",))
    store.set_meta("started_at", utcnow())
    asyncio.run(run_doctor(c, smoke=False))
    assert "✓ messages taken and not yet answered — 1\n" in capsys.readouterr().out
    store.set_meta("started_at", sent.isoformat(timespec="seconds"))
    asyncio.run(run_doctor(c, smoke=False))
    out = capsys.readouterr().out
    assert "✗ messages taken and not yet answered — 1, 1 due longer than a turn takes\n" in out
    assert f"      D1, the message of {sent:%Y-%m-%d %H:%M} (ts {sent.timestamp():.1f}), taken by 0 session(s)" in out


@pytest.mark.parametrize("later, late", [(601, True), (540, False)])
def test_a_turn_that_reaches_its_session_late_is_framed_at_its_start_and_says_so(tmp_path, monkeypatch, fake_time,
                                                                                 later, late):
    """Past ten minutes, longer than a wait behind one other session."""
    runner = RecordingRunner()
    p, _, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    monkeypatch.setattr(main, "LATE_TURN_S", LATE_TURN_S)
    fake_time.at = datetime.fromtimestamp(AT + later, timezone.utc)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    [(prompt, kw)] = runner.calls
    if late:
        assert ("In a direct message that fan and I read. What fan says below was sent at 16:40 and reaches me "
                "only now.\n\nfan says:\n\n    is it paid?") in prompt
        assert "it is 16:50 here" in kw["append_system_prompt"]
    else:
        assert "fan says to me, in a direct message:\n\n    is it paid?" in prompt
        assert "it is 16:40 here" in kw["append_system_prompt"]


@pytest.mark.parametrize("stops, named", [
    ([(5, 50)], [("16:45", "17:30")]),
    ([(5, 50), (80, 110)], [("16:45", "17:30"), ("18:00", "18:30")]),
    ([(60, 60 + 1 / 3)], []),
], ids=["across a stop", "across two stops", "across a 20-second restart"])
def test_a_late_turn_names_each_interval_she_was_not_running_since_its_oldest_message(
        tmp_path, monkeypatch, fake_time, stops, named):
    """Each of a minute or more, five minutes after the start too; a restart
    of seconds is no reason for the lateness, and one before the message is
    none either."""
    runner = RecordingRunner()
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    monkeypatch.setattr(main, "LATE_TURN_S", LATE_TURN_S)

    def at(minutes):
        return datetime.fromtimestamp(AT + minutes * 60, timezone.utc).isoformat(timespec="seconds")
    store.set_meta("down", json.dumps([[at(-60), at(-30)]] + [[at(a), at(b)] for a, b in stops]))
    fake_time.at = datetime.fromtimestamp(AT + (max(b for _, b in stops) + 5) * 60, timezone.utc)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    [(prompt, _)] = runner.calls
    down = "".join(f" {vault.DOWN.format(since=a, until=b)}" for a, b in named)
    assert f"What fan says below was sent at 16:40 and reaches me only now.{down}\n\n" in prompt


def test_a_batch_spanning_a_night_is_framed_whole_her_note_among_it(tmp_path, monkeypatch, fake_time):
    """Thirty-eight of fan's lines kept from 23:00 to 11:20, longer than the
    twelve hours a frame reads back, her note among them, run again at noon
    as one turn: every line from the first on is shown, past the household's
    twenty, and the twenty before it, the history read back from the first
    until it has those."""
    fake_time.at = datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc)  # noon in Los Angeles
    night = [AT + 6 * 3600 + 20 * 60 + i * 1200 for i in range(38)]  # from 23:00
    before = [night[0] - 3600 - i * 60 for i in range(30)]
    history = ([{"user": "U1", "ts": f"{t:.1f}", "text": f"evening {i}"} for i, t in enumerate(before)]
               + [{"user": "U1", "ts": f"{t:.1f}", "text": f"night {i}"} for i, t in enumerate(night)]
               + [{"user": "UBOT", "bot_id": "BME", "ts": f"{night[5] + 1:.1f}", "text": main.FAILED}])
    runner = RecordingRunner(answer("Morning."))
    slack = ConversationSlack(history=history, paged=True)
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    monkeypatch.setattr(main, "LATE_TURN_S", LATE_TURN_S)
    for i, t in enumerate(night):
        keep(store, dm(f"{t:.1f}", f"night {i}"))
    started_again(p, runner)
    [(prompt, _)] = runner.calls
    lines = prompt.split("The conversation so far:\n\n")[1].split("\n\nfan now says:")[0].splitlines()
    assert [line.split(": ", 1)[1] for line in lines] == (
        [f"evening {i}" for i in reversed(range(20))] + [f"night {i}" for i in range(6)] + [main.FAILED]
        + [f"night {i}" for i in range(6, 37)])
    assert "now says:\n\n    night 37" in prompt


def test_two_earlier_sessions_in_one_batch_are_each_named_oldest_first(tmp_path, monkeypatch):
    runner = RecordingRunner(answer("Both."))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    keep(store, dm(f"{AT:.1f}", "one"), "3f9a1c2e-a")
    keep(store, dm(f"{AT + 1:.1f}", "two"), "7b20d4e1-b")
    started_again(p, runner)
    assert (f"{vault.RETRIED.format(sid8='3f9a1c2e')} {vault.RETRIED.format(sid8='7b20d4e1')}\n\n"
            in runner.calls[0][0])


# --- a planned stop ---

def test_a_stop_lets_the_running_session_answer_record_and_snapshot_before_the_store_closes(tmp_path, monkeypatch,
                                                                                            caplog):
    """SIGTERM while the session a start took up works: the stop says so,
    ends the clock and the reads of names at once, and waits for it, the mail
    loop passing meanwhile and Slack still connected, so that what fan sends
    then is kept, dispatched and offered to the session; its answer is
    posted and recorded and the vault snapshotted, and only then does the
    daemon shut down, stopping the watcher and writing when she last ran
    just before the store closes. What fan sent, never taken in, is left due
    for the next start, her reaction on it."""
    import logging

    events = []

    class Into(logging.Handler):
        def emit(self, record):
            events.append(record.getMessage())

    class Stopped(RecordingRunner):
        """Stops the daemon as its session starts, fan writing again in its
        DM while it works, and answers a little later."""

        async def run(self, prompt, **kw):
            events.append("the session starts")
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.1)
            slack.watcher._handle(SimpleNamespace(send_socket_mode_response=lambda r: None), SimpleNamespace(
                type="events_api", envelope_id="e2", payload={"event": {
                    "type": "message", "user": "U1", "channel": "D1", "channel_type": "im", "ts": f"{AT + 60:.1f}",
                    "text": "and the water?"}}))
            for _ in range(100):
                if any(m["ts"] == f"{AT + 60:.1f}" for m in kw["feed"].waiting):
                    events.append("offered to the session")
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            events.append("the session ends")
            return await super().run(prompt, **kw)

    def every(what):
        async def loop(self):
            while True:
                await asyncio.sleep(0.05)
                events.append(what)
        return loop

    set_meta, close = Store.set_meta, Store.close

    def marked(self, key, value):
        if key == "up_at":
            events.append("up_at")
        set_meta(self, key, value)

    def closed(self):
        events.append("the store closes")
        close(self)
    monkeypatch.setattr(Store, "set_meta", marked)
    monkeypatch.setattr(Store, "close", closed)
    monkeypatch.setattr("wanda.main.Processor.clock_loop", every("a tick"))
    monkeypatch.setattr("wanda.main.Processor.names_loop", every("a tick"))
    store = Store(start_config(tmp_path).db_path)
    keep(store, dm(f"{AT:.1f}", "is it paid?"))
    slack, into = ConversationSlack(history=[]), Into()
    logging.getLogger("wanda").addHandler(into)
    try:
        with caplog.at_level(logging.INFO, logger="wanda"):
            a_start(tmp_path, monkeypatch, Stopped(answer("Yes, on Monday.")), slack, loop=every("a pass"),
                    snapshot=lambda cfg, message: events.append(f"snapshot {message}"),
                    watcher_stops=lambda: events.append("the watcher stops"))
    finally:
        logging.getLogger("wanda").removeHandler(into)
    stopping = "stopping: letting 1 session(s) finish, for up to 960 s"
    session = next(e for e in events if e.startswith("memory session "))
    at = events.index
    assert (at("the session starts") < at(stopping) < at("offered to the session") < at("the session ends")
            < at(session) < at(f"snapshot after {session.split()[2]}") < at("shutting down")
            < at("the watcher stops")), events
    assert "a pass" in events[at(stopping):at("shutting down")] and "a tick" not in events[at(stopping):]
    assert events[at("shutting down"):][-2:] == ["up_at", "the store closes"]
    assert slack.replies == ["Yes, on Monday."] and kept(store) == [(f"{AT + 60:.1f}", "due", 0, None)]
    assert ("D1", f"{AT + 60:.1f}") in slack.reacted and ("D1", f"{AT + 60:.1f}") not in slack.unreacted
    assert [r["status"] for r in store._query("SELECT status FROM runs")] == ["ok"]


def test_a_stop_folds_a_message_into_the_running_session_and_starts_no_other(tmp_path, monkeypatch, caplog):
    """While fan's session works, mei's message waits for the one session's
    place; the stop cancels her turn and waits for his. What he adds
    meanwhile is taken into his session's one answer, and what she sends
    starts nothing: both of hers are left due, untried, for the next start,
    each still carrying her reaction, which his lose with the answer."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[1.0, 0.2])
    asked, added = dm(f"{AT:.1f}", "can you remind me at 5"), dm(f"{AT + 30:.1f}", "to call the plumber")
    waits, later = (dm(f"{AT + i:.1f}", text, channel="D2", user="U2")
                    for i, text in ((10, "is the gas paid?"), (40, "and the water?")))

    def handled(ev):
        # as the watcher keeps it and slack_loop dispatches it
        keep(store, ev)
        t = asyncio.create_task(p.handle_slack(ev))
        p._bg.add(t)
        return t

    async def go():
        handled(asked)
        await moment(("tool_use", 1, 0.1), p.cfg.vault_dir)
        waiting = handled(waits)
        await asyncio.sleep(0.1)
        stop = asyncio.create_task(p.let_finish(30))
        await asyncio.sleep(0.05)
        handled(added), handled(later)
        await asyncio.wait_for(stop, 10)
        await p.shutdown(grace_s=1)
        await reactions_end(p)
        return waiting
    with caplog.at_level(logging.INFO, logger="wanda"):
        waiting = asyncio.run(go())
    assert "stopping: letting 1 session(s) finish, for up to 30 s" in caplog.text
    assert waiting.cancelled()
    assert slack.replies == ["one answer to 2: can you remind me at 5 | to call the plumber"]
    assert [r["status"] for r in store._query("SELECT status FROM runs")] == ["ok"]
    assert kept(store) == [(f"{AT + 10:.1f}", "due", 0, None), (f"{AT + 40:.1f}", "due", 0, None)]
    fans, meis = [("D1", f"{AT:.1f}"), ("D1", f"{AT + 30:.1f}")], [("D2", f"{AT + 10:.1f}"), ("D2", f"{AT + 40:.1f}")]
    assert sorted(slack.reacted) == fans + meis and sorted(slack.unreacted) == fans


def test_a_stop_cancels_a_clock_wake_waiting_for_the_dm_a_session_works_in(tmp_path, monkeypatch):
    """fan's 19:00 reminder, claimed while his message's session works in his
    DM, waits for it: the stop cancels it there, as before, and the next
    start wakes it again; his session answers."""
    class InHisDM(ConversationSlack):
        async def dm_channel(self, user):
            return "D1"

    runner = Held(answer("Yes, on Monday."))
    p, store, _ = memory_processor(tmp_path, InHisDM(history=[]), runner, monkeypatch)
    at = datetime(2026, 10, 1, 19, 3, tzinfo=p.cfg.zone)
    w = clock.Wake("clock:due:b6647b:2026-10-01T19:00:fan", "U1", "    I undertook to remind fan at 7",
                   about="b6647b", by="2026-10-01T19:00", asked="fan")

    async def go():
        turn = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
        p._bg.add(turn)
        while not runner.running:
            await asyncio.sleep(0.01)
        wake = asyncio.create_task(p._clock_session(w, at))
        p._bg.add(wake)
        while not store.get_meta(w.key):
            await asyncio.sleep(0.01)  # claimed, and waiting for his DM
        stop = asyncio.create_task(p.let_finish(30))
        await asyncio.sleep(0.05)
        cancelled = wake.cancelled()
        runner.release.set()
        await asyncio.wait_for(stop, 5)
        await p.shutdown(grace_s=1)
        return cancelled
    assert asyncio.run(go())
    assert len(runner.calls) == 1 and p.slack.replies == ["Yes, on Monday."]
    p.settle_wakes(at + timedelta(minutes=5))
    assert json.loads(store.get_meta("clock:waking"))[w.key]["again"] and store.get_meta(w.key) == ""


@pytest.mark.parametrize("then", ["answers", "fails"])
def test_a_session_that_fails_while_she_is_stopping_is_not_tried_again_and_runs_at_the_next_start(
        tmp_path, monkeypatch, caplog, then):
    """Its quiet retry would be a second session the stop waits for: its
    message is left due, with nothing said, and the next start runs it,
    framed with the session that failed, as that session's one retry: one
    that fails too gets her note, and the alert names the first."""
    import logging

    runner = Held(FILLER)
    p, store, snaps = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)

    async def go():
        turn = asyncio.create_task(p.handle_slack(line))
        p._bg.add(turn)
        while not runner.running:
            await asyncio.sleep(0.01)
        stop = asyncio.create_task(p.let_finish(30))
        await asyncio.sleep(0.05)
        runner.release.set()
        await asyncio.wait_for(stop, 5)
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(go())
    [run] = store._query("SELECT session_id, status, result_text, notified FROM runs")
    sid = run["session_id"]
    assert len(runner.calls) == 1 and p.slack.replies == []
    assert (run["status"], run["result_text"], run["notified"]) == ("error", None, 1)
    assert kept(store) == [(f"{AT:.1f}", "due", 1, sid)] and snaps == [f"after {sid}"]
    assert "failed: the session ended without its report; run again at the next start" in caplog.text
    again = RecordingRunner(answer("Yes, on Monday.") if then == "answers" else FILLER)
    started_again(p, again)
    assert len(again.calls) == 1 and vault.RETRIED.format(sid8=sid[:8]) in again.calls[0][0]
    assert p.slack.replies == (["Yes, on Monday."] if then == "answers" else [main.FAILED]) and kept(store) == []
    if then == "fails":
        [failed] = json.loads(store.get_meta("failed_runs"))
        assert (failed["id"], failed["then"]) == (1, "tried once more, a note asked for it again")


def test_a_stop_while_a_retry_is_framed_cancels_the_turn(tmp_path, monkeypatch):
    """No session runs between a failed first try and its retry: a stop then
    cancels the turn, as it cancels one waiting for a session's place, and
    its message is left due with the first try's session."""
    class Stalled(ConversationSlack):
        """Reads the conversation for the first frame, and never for the retry's."""

        def __init__(self, **kw):
            super().__init__(**kw)
            self.stalled = asyncio.Event()

        async def fetch_context(self, channel, thread_ts, since, counted=None):
            if self.fetched:
                self.stalled.set()
                await asyncio.Event().wait()
            return await super().fetch_context(channel, thread_ts, since, counted)

    runner, slack = RecordingRunner(FILLER), Stalled(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)

    async def go():
        turn = asyncio.create_task(p.handle_slack(line))
        p._bg.add(turn)
        await asyncio.wait_for(slack.stalled.wait(), 5)
        began = time.monotonic()
        await asyncio.wait_for(p.let_finish(30), 5)
        took = time.monotonic() - began
        await asyncio.sleep(0.05)
        return took, turn.cancelled()
    took, cancelled = asyncio.run(go())
    assert took < 1 and cancelled and len(runner.calls) == 1 and slack.replies == []
    [run] = store._query("SELECT session_id, status FROM runs")
    assert run["status"] == "error" and kept(store) == [(f"{AT:.1f}", "due", 1, run["session_id"])]


def test_a_session_that_outlasts_the_stops_wait_is_cancelled_and_its_message_left_due(tmp_path, monkeypatch):
    runner = Held()
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)

    async def go():
        turn = asyncio.create_task(p.handle_slack(line))
        p._bg.add(turn)
        while not runner.running:
            await asyncio.sleep(0.01)
        began = time.monotonic()
        await asyncio.wait_for(p.let_finish(0.3), 5)
        waited, running = time.monotonic() - began, not turn.done()
        await p.shutdown(grace_s=1)
        return waited, running
    waited, running = asyncio.run(go())
    assert waited >= 0.3 and running
    [run] = store._query("SELECT session_id, status FROM runs")
    assert run["status"] == "cancelled" and kept(store) == [(f"{AT:.1f}", "due", 1, run["session_id"])]
    assert p.slack.replies == []


def test_a_stop_with_no_session_running_returns_at_once(tmp_path, monkeypatch, caplog):
    """A turn waiting for its conversation, which a clock wake holds, say, is
    cancelled with its message as it was."""
    import logging

    runner = Held()
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)

    async def go():
        await p._task_locks.setdefault(store.create_task(None, "D1", "conversation", kind="dm"),
                                       asyncio.Lock()).acquire()
        turn = asyncio.create_task(p.handle_slack(line))
        p._bg.add(turn)
        await asyncio.sleep(0.05)
        began = time.monotonic()
        await asyncio.wait_for(p.let_finish(30), 5)
        took = time.monotonic() - began
        await asyncio.sleep(0.05)
        return took, turn.cancelled()
    with caplog.at_level(logging.INFO, logger="wanda"):
        took, cancelled = asyncio.run(go())
    assert took < 1 and cancelled
    assert "stopping: letting 0 session(s) finish, for up to 30 s" in caplog.text
    assert runner.calls == [] and kept(store) == [(f"{AT:.1f}", "due", 0, None)]


def test_a_pass_while_she_is_stopping_posts_what_is_owed_and_starts_nothing_else(tmp_path, monkeypatch):
    """The stop waits for the sessions running, whose snapshots the
    housekeeping would hold up, and starts none of its own: a pass then
    delivers and alerts, and neither looks after the snapshots nor triages
    mail."""
    slack = FakeSlack()
    p, store = make(tmp_path, slack, email_triage=True, tz="America/Los_Angeles")
    housekept, triaged = [], []
    monkeypatch.setattr("wanda.vault.housekeep", lambda cfg: housekept.append(cfg.snapshots_dir) or None)
    monkeypatch.setattr(main, "HOUSEKEEPING_HOUR", 0)

    async def triage_batch(rows):
        triaged.extend(r["dedupe_key"] for r in rows)
        for r in rows:
            store.set_message_status(r["dedupe_key"], "done")
    monkeypatch.setattr(p, "triage_batch", triage_batch)
    p.cfg.snapshots_dir.mkdir()
    (p.cfg.snapshots_dir / "HEAD").write_text("ref: refs/heads/master\n")
    store.ingest_message(dedupe_key="k1", message_id="<k1>", folder="INBOX", uidvalidity=1, uid=1,
                         from_addr="a@x.example", subject="s", date_hdr="d", snippet="b")
    owed(store, "Yes, on Monday.")
    p.stopping = True
    asyncio.run(p.drain_mail())
    assert slack.replies == ["Yes, on Monday."] and housekept == [] and triaged == []
    p.stopping = False
    asyncio.run(p.drain_mail())
    assert housekept == [p.cfg.snapshots_dir] and triaged == ["k1"]


def test_compose_lets_a_stop_wait_for_a_session_before_docker_kills_the_daemon():
    """Docker kills the daemon once stop_grace_period has passed: past the
    session's timeout and the stop's minute more, and the shutdown's grace."""
    compose = (Path(__file__).resolve().parent.parent / "compose.wanda.yaml").read_text()
    grace = re.search(r"\n    stop_grace_period: (\d+)(s|m)\n", compose)
    timeout = re.search(r'\n      WANDA_AGENT_TIMEOUT_S: "(\d+)"\n', compose)
    assert main.STOP_AFTER_TIMEOUT_S + main.SHUTDOWN_GRACE_S == 80
    assert int(grace[1]) * (60 if grace[2] == "m" else 1) > int(timeout[1]) + 80


async def reactions_end(p):
    """Until every call on her reaction set going so far has ended: one left
    when a test's run ends would be cancelled with it."""
    while p._reacting:
        await asyncio.wait(set(p._reacting))


def settle(p, coro):
    """Runs `coro`, then lets each call on her reaction it set going end."""
    async def go():
        out = await coro
        await reactions_end(p)
        return out
    return asyncio.run(go())


def slack_error(error):
    return SlackApiError("The request to the Slack API failed.", {"ok": False, "error": error})


class Watched(ConversationSlack):
    """Each post and each call on her reaction, in order, in `seen`."""

    def __init__(self, **kw):
        super().__init__(history=[], **kw)
        self.seen = []

    async def reply(self, thread_ts, text, channel=None):
        await super().reply(thread_ts, text, channel)
        self.seen.append(text)

    async def react(self, channel, ts):
        await super().react(channel, ts)
        self.seen.append(("on", ts))

    async def unreact(self, channel, ts):
        await super().unreact(channel, ts)
        self.seen.append(("off", ts))


class Reacting(ConversationSlack):
    """A Slack whose reactions stand as its calls left them, in `on`, each
    add answered in turn by `adds` and each removal by `removals`: None
    takes it, an error refuses it, "made" takes it and then times out, and
    "hang" takes it once `gate` is set."""

    def __init__(self, adds=(), removals=()):
        super().__init__(history=[])
        self.adds, self.removals = list(adds), list(removals)
        self.on: set = set()
        self.gate = asyncio.Event()

    async def _answer(self, answers, key, put):
        how = answers.pop(0) if answers else None
        if how == "hang":
            await self.gate.wait()
            how = None
        if how in (None, "made"):
            (self.on.add if put else self.on.discard)(key)
        if how == "made":
            raise TimeoutError("The read operation timed out")
        if how is not None:
            raise how

    async def react(self, channel, ts):
        await super().react(channel, ts)
        await self._answer(self.adds, (channel, ts), True)

    async def unreact(self, channel, ts):
        await super().unreact(channel, ts)
        await self._answer(self.removals, (channel, ts), False)


def test_her_reaction_goes_on_each_message_a_member_let_in_sends_while_it_waits(tmp_path, monkeypatch):
    """Not waited for: fan's two lines carry it while his session works,
    the second's waiting for the next turn, and each loses it once the turn
    that took it has ended. mei, allowed and not let in, gets none: her line,
    no longer kept, is only taken off, as any is whose row goes, since an
    earlier start may have put it on."""
    runner = Held(answer("Will do."))
    slack = Watched()
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    store._exec("DELETE FROM meta WHERE key='names:U2'")
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    first, second = (f"{AT + i:.1f}" for i in (0, 10))
    lines = [dm(first, "can you remind me"), dm(second, "to call the plumber"),
             dm(f"{AT + 20:.1f}", "hi", channel="D2", user="U2")]
    for ev in lines:
        keep(store, ev)

    async def go():
        turns = [asyncio.create_task(p.handle_slack(ev)) for ev in lines]
        await asyncio.sleep(0.05)
        working = list(slack.reacted)
        runner.release.set()
        await asyncio.gather(*turns)
        await reactions_end(p)
        return working
    assert asyncio.run(go()) == [("D1", first), ("D1", second)] == slack.reacted
    assert slack.seen.index("Will do.") < slack.seen.index(("off", first))
    assert sorted(slack.unreacted) == [("D1", first), ("D1", second), ("D2", f"{AT + 20:.1f}")]
    assert kept(store) == []


@pytest.mark.parametrize("ending", ["her answer", "a silence", "her note", "a deletion"])
def test_her_reaction_comes_off_once_when_its_message_is_no_longer_kept(tmp_path, monkeypatch, ending):
    """Whatever lets the message go: the post of her answer or of her note,
    which answers it, a silence, and its deletion while its session works.
    Once, after anything posted to it, and not before."""
    runner = Held(*{"her answer": [answer("Yes.")],
                    "her note": [said_by_claude("API Error: 500")] * 2}.get(ending, [answer("")]))
    slack = Watched()
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    key = keep(store, line)

    async def go():
        turn = asyncio.create_task(p.handle_slack(line))
        await asyncio.sleep(0.05)
        await reactions_end(p)
        working, deleted = list(slack.unreacted), None
        if ending == "a deletion":
            await p.handle_slack(Event(source="slack", dedupe_key="x", payload={
                "kind": "deleted", "channel": "D1", "ts": key[1]}))
            await reactions_end(p)
            deleted = list(slack.unreacted)
        runner.release.set()
        await turn
        await reactions_end(p)
        return working, deleted
    working, deleted = asyncio.run(go())
    assert working == []
    assert deleted == ([key] if ending == "a deletion" else None)
    assert slack.reacted == [key] and slack.unreacted == [key] and kept(store) == []
    assert slack.seen[-1] == ("off", key[1]) and slack.replies == {
        "her answer": ["Yes."], "her note": [main.FAILED]}.get(ending, [])


def test_her_reaction_stays_on_through_the_quiet_retry(tmp_path, monkeypatch):
    """The first session ends without its report: the message is still hers
    to answer while its second session works."""
    slack = Watched()

    class Twice(RecordingRunner):
        async def run(self, prompt, **kw):
            await reactions_end(p)
            on.append(len(slack.reacted) - len(slack.unreacted))
            return await super().run(prompt, **kw)
    on = []
    p, store, _ = memory_processor(tmp_path, slack, Twice("I filed it.", answer("Yes.")), monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    key = keep(store, line)
    settle(p, p.handle_slack(line))
    assert on == [1, 1] and slack.seen == [("on", key[1]), "Yes.", ("off", key[1])]


def test_her_reaction_stays_on_while_an_answer_slack_refuses_is_tried_again(tmp_path, monkeypatch, fake_time):
    """Owed for close to two hours of her running, the answer is still to
    come: the reaction comes off once Slack takes it."""
    class Down(Watched):
        down = True

        async def reply(self, thread_ts, text, channel=None):
            if self.down:
                raise RuntimeError("ratelimited")
            await super().reply(thread_ts, text, channel)
    slack = Down()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("Yes.")), monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    key = keep(store, line)
    settle(p, p.handle_slack(line))
    for _ in range(2):
        fake_time.at += timedelta(minutes=55)
        settle(p, p.drain_mail())
    assert slack.reacted == [key] and slack.unreacted == [] and kept(store)[0][:2] == (key[1], "answered")
    slack.down = False
    fake_time.at += timedelta(minutes=5)
    settle(p, p.drain_mail())
    assert slack.unreacted == [key] and slack.seen[-1] == ("off", key[1]) and kept(store) == []


def test_an_add_slack_did_not_take_is_tried_again_at_the_next_pass(tmp_path, monkeypatch):
    """Once made it is not tried again, and it comes off with her answer."""
    slack = Reacting(adds=[slack_error("ratelimited")])
    runner = Held(answer("Yes."))
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    key = keep(store, line)

    async def go():
        turn = asyncio.create_task(p.handle_slack(line))
        await asyncio.sleep(0.05)
        seen = [set(slack.on)]
        for _ in range(2):
            await p.drain_mail()
            await reactions_end(p)
            seen.append((set(slack.on), len(slack.reacted)))
        runner.release.set()
        await turn
        await reactions_end(p)
        return seen
    assert asyncio.run(go()) == [set(), ({key}, 2), ({key}, 2)]
    assert slack.on == set() and slack.unreacted == [key]


def test_a_message_deleted_while_its_adds_retry_is_in_flight_ends_with_no_reaction(tmp_path, monkeypatch):
    """The removal waits for the add in flight, a pass meanwhile starts no
    second add, and none is tried once the removal is due. The silence after
    the deletion takes nothing off again."""
    slack = Reacting(adds=[slack_error("ratelimited"), "hang"])
    runner = Held(answer(""))
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    key = keep(store, line)

    async def go():
        turn = asyncio.create_task(p.handle_slack(line))
        await asyncio.sleep(0.05)
        await p.drain_mail()
        await asyncio.sleep(0.05)
        await p.drain_mail()
        await p.handle_slack(Event(source="slack", dedupe_key="x", payload={
            "kind": "deleted", "channel": "D1", "ts": key[1]}))
        await asyncio.sleep(0.05)
        waiting = list(slack.unreacted)
        slack.gate.set()
        await reactions_end(p)
        await p.drain_mail()
        runner.release.set()
        await turn
        await reactions_end(p)
        return waiting
    assert asyncio.run(go()) == []
    assert slack.on == set() and len(slack.reacted) == 2 and slack.unreacted == [key] and kept(store) == []


def test_an_add_that_timed_out_after_slack_made_it_comes_off_with_her_answer(tmp_path, monkeypatch):
    """The add failed as far as she knows, while its session works: no
    removal is skipped for that."""
    slack = Reacting(adds=["made"])
    runner = Held(answer("Yes."))
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    key = keep(store, line)

    async def go():
        turn = asyncio.create_task(p.handle_slack(line))
        await asyncio.sleep(0.05)
        made = set(slack.on)
        runner.release.set()
        await turn
        await reactions_end(p)
        return made
    assert asyncio.run(go()) == {key}
    assert slack.replies == ["Yes."] and slack.reacted == slack.unreacted == [key] and slack.on == set()


@pytest.mark.parametrize("error", ["not_reactable", "missing_scope"])
def test_an_add_slack_will_never_take_is_dropped(tmp_path, monkeypatch, caplog, error):
    """Not tried again. A token without reactions:write is said in the log
    once, whatever it refuses, and a removal it refuses is not tried again
    either: only a reinstall and a start give it the scope."""
    import logging

    scope = error == "missing_scope"
    slack = Reacting(adds=[slack_error(error)] * 2, removals=[slack_error(error)] * 2 if scope else [])
    runner = Held(answer(""), answer(""))
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    lines = [dm(f"{AT:.1f}", "one"), dm(f"{AT + 60:.1f}", "two", channel="D2", user="U2")]
    for ev in lines:
        keep(store, ev)

    async def go():
        turns = [asyncio.create_task(p.handle_slack(ev)) for ev in lines]
        await asyncio.sleep(0.05)
        await p.drain_mail()
        await reactions_end(p)
        runner.release.set()
        await asyncio.gather(*turns)
        await reactions_end(p)
        await p.drain_mail()
        await reactions_end(p)
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(go())
    assert len(slack.reacted) == 2 and len(slack.unreacted) == 2 and slack.on == set()
    assert caplog.text.count("the bot token lacks reactions:write") == (1 if scope else 0)


def test_a_removal_slack_did_not_take_is_tried_at_each_pass_for_a_day(tmp_path, monkeypatch, fake_time, caplog):
    import logging

    slack = Reacting(removals=[slack_error("ratelimited")] * 5)
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("")), monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    key = keep(store, line)
    settle(p, p.handle_slack(line))
    for _ in range(2):
        fake_time.at += timedelta(minutes=1)
        settle(p, p.drain_mail())
    assert len(slack.unreacted) == 3 and slack.on == {key}
    fake_time.at += timedelta(days=1)
    with caplog.at_level(logging.WARNING, logger="wanda"):
        for _ in range(2):
            settle(p, p.drain_mail())
    assert len(slack.unreacted) == 3 and "gave up trying to take off her reaction" in caplog.text


def test_a_reaction_call_that_hangs_holds_up_no_post_and_no_wake(tmp_path, monkeypatch):
    """Its task is no session's: the clock still starts a wake, and the
    removal waits for it behind the post."""
    slack = Reacting(adds=["hang"])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("Yes.")), monkeypatch)
    woken = []

    async def wake(w, now):
        woken.append(w.key)
    monkeypatch.setattr(p, "_clock_session", wake)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)

    async def go():
        await asyncio.wait_for(p.handle_slack(line), 5)
        p._wake([SimpleNamespace(key="clock:due:aaaaaa")], datetime.now(p.cfg.zone))
        await asyncio.sleep(0.05)
        return len(p._reacting)
    assert asyncio.run(go()) == 2
    assert slack.replies == ["Yes."] and woken == ["clock:due:aaaaaa"]


@pytest.mark.parametrize("header,scopes,line", [
    ("x-oauth-scopes", "chat:write,reactions:write,users:read",
     "✓ bot token — bot user UBOT in household; reactions:write\n"),
    ("X-OAuth-Scopes", "chat:write, reactions:write",
     "✓ bot token — bot user UBOT in household; reactions:write\n"),
    ("x-oauth-scopes", "chat:write,users:read",
     "✗ bot token — the token lacks reactions:write: update the app from slack/manifest.yaml and reinstall it "
     "(README, Setup, step 3)\n")], ids=["with it", "its header's name in another case", "without it"])
def test_doctor_says_whether_the_bot_token_can_put_her_reaction_on(tmp_path, capsys, monkeypatch, header, scopes,
                                                                    line):
    """By the scopes Slack sends with auth.test's answer."""
    from slack_sdk.web.slack_response import SlackResponse

    class Web:
        def __init__(self, **kw):
            pass

        def auth_test(self):
            return SlackResponse(client=None, http_verb="POST", api_url="auth.test", req_args={}, status_code=200,
                                 data={"ok": True, "user_id": "UBOT", "team": "household"}, headers={header: scopes})

        def apps_connections_open(self, app_token):
            return {"ok": True}
    monkeypatch.setattr("slack_sdk.WebClient", Web)
    c = Config(_env_file=None, data_dir=tmp_path, claude_bin="/bin/true", email_triage=False,
               slack_bot_token="xoxb-x", slack_app_token="xapp-y")
    asyncio.run(main.run_doctor(c, smoke=False))
    assert f"  {line}" in capsys.readouterr().out


# --- the run cap, and Claude Code's limit ---

def a_pass(p):
    """A pass of the mail loop, then every turn it took up, to its end."""
    async def go():
        await p.drain_mail()
        await until(lambda: not p._bg, "every turn it took up ended")
        await reactions_end(p)
    asyncio.run(go())


async def until(done, what):
    """Waits for `done()`, failing the test past a few seconds rather than
    hanging it."""
    for _ in range(500):
        if done():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"gave up waiting until {what}")


LIMIT = "You've hit your limit · resets 5pm"


class Limited(RecordingRunner):
    """Claude Code at its usage limit: every session it is given begins its
    turn and is refused, until `back`; then each reports as RecordingRunner's
    does. A session waits for `gate` first, when one is set."""

    def __init__(self, *reports):
        super().__init__(*reports)
        self.back = False
        self.gate: asyncio.Event | None = None
        self.running = 0

    async def run(self, prompt, **kw):
        self.running += 1
        if self.gate is not None:
            await self.gate.wait()
        self.running -= 1
        if self.back:
            return await super().run(prompt, **kw)
        if kw.get("feed") is not None:
            kw["feed"].began()
        self.calls.append((prompt, kw))
        return said_by_claude(LIMIT, api_error="rate_limit")


@pytest.mark.parametrize("refusal", ["breaker", "busy", "a refused retry", "breaker, in a group DM"])
def test_at_the_cap_a_conversation_is_told_once_and_its_messages_are_kept(tmp_path, monkeypatch, fake_time, refusal):
    """Her note the first time in the local day (CAPPED_NOTE, CAPPED_GROUP),
    nothing for a second message there, both kept with her reaction on,
    whether the runs recorded reach the cap, one in flight would, or the
    first try's run does before its quiet retry, which then keeps the first
    session for the take-up to name. The second is sent after UTC midnight,
    still the household's day, so the run before it counts and the note is
    not said again; the alert, kept to the UTC day, is. After local midnight
    a pass takes both up as one turn."""
    fake_time.at = datetime.fromtimestamp(AT + 30, timezone.utc)
    group = "group" in refusal
    retry = refusal == "a refused retry"
    runner = RecordingRunner(*([said_by_claude("error_during_execution", "error_during_execution")] if retry else []),
                             answer("Both noted."))
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    monkeypatch.setattr(p.cfg, "daily_run_cap", 1)
    if refusal.startswith("breaker"):
        store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                         cost_usd=0.1, status="ok")
    p._inflight_runs = int(refusal == "busy")
    # 16:40 and 17:10 in Los Angeles, either side of UTC midnight
    lines = [dm(f"{AT + i:.1f}", text, channel_type="mpim" if group else "im", channel="G1" if group else "D1")
             for i, text in ((0, "is it paid?"), (1800, "and the plumber?"))]
    for ev in lines:
        fake_time.at = datetime.fromtimestamp(float(ev.payload["ts"]) + 30, timezone.utc)
        keep(store, ev)
        settle(p, p.handle_slack(ev))
    assert slack.replies == [main.CAPPED_GROUP if group else main.CAPPED_NOTE] and slack.unreacted == []
    assert (slack.alerts == 2 * ["daily run cap reached (1 runs since midnight, America/Los_Angeles); messages are "
                                 "held until midnight"]) if refusal != "busy" else slack.alerts == []
    first = runner.calls[0][1]["session_id"] if retry else None
    assert kept(store) == [(f"{AT:.1f}", "capped", 0, first), (f"{AT + 1800:.1f}", "capped", 0, None)]
    fake_time.at = datetime(2026, 10, 2, 7, 1, tzinfo=timezone.utc)  # 00:01 in Los Angeles
    p._inflight_runs = 0
    a_pass(p)
    prompt = runner.calls[-1][0]
    assert len(runner.calls) == 1 + retry and "is it paid?" in prompt and "and the plumber?" in prompt
    assert (vault.RETRIED.format(sid8=first[:8]) in prompt) if retry else "An earlier session" not in prompt
    assert slack.replies[-1] == "Both noted." and kept(store) == []


def test_a_line_sent_while_her_note_on_the_cap_posts_is_kept_with_the_one_before(tmp_path, monkeypatch, fake_time):
    """fan writes again a moment after a line the cap kept, while her note on
    it is posted: the cap keeps that one too, saying nothing more, and after
    midnight both are one turn."""
    fake_time.at = datetime.fromtimestamp(AT + 30, timezone.utc)
    runner, slack = RecordingRunner(answer("Both, then.")), HeldNote(note=main.CAPPED_NOTE, history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    one, two = dm(f"{AT:.1f}", "one"), dm(f"{AT + 10:.1f}", "two")
    keep(store, one), keep(store, two)
    store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                     cost_usd=p.cfg.daily_cost_cap_usd, status="ok")

    async def go():
        first = asyncio.create_task(p.handle_slack(one))
        await until(lambda: slack.waiting, "her note is being posted")
        second = asyncio.create_task(p.handle_slack(two))
        await asyncio.sleep(0.05)
        slack.gate.set()
        await asyncio.gather(first, second)
        await reactions_end(p)
    asyncio.run(go())
    assert slack.replies == [main.CAPPED_NOTE] and [r[1] for r in kept(store)] == ["capped", "capped"]
    fake_time.at = datetime(2026, 10, 2, 7, 1, tzinfo=timezone.utc)  # 00:01 in Los Angeles
    a_pass(p)
    assert len(runner.calls) == 1 and "    16:40 fan: one\n\nfan now says:\n\n    two" in runner.calls[0][0]
    assert slack.replies[-1] == "Both, then." and kept(store) == []


def test_what_the_cap_kept_is_one_late_turn_after_midnight_taken_up_once(tmp_path, monkeypatch, fake_time):
    """At the first pass after local midnight both lines the cap kept in a DM
    are one turn, framed late; a pass while its session works takes nothing
    up again."""
    fake_time.at = datetime(2026, 10, 2, 7, 1, tzinfo=timezone.utc)  # 00:01 in Los Angeles
    runner = Held(answer("Both noted."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    monkeypatch.setattr(main, "LATE_TURN_S", LATE_TURN_S)
    store.create_task(None, "D1", "conversation", kind="dm")
    store.settle(Settled(capped=tuple(keep(store, dm(f"{AT + i:.1f}", text))
                                      for i, text in ((0, "is it paid?"), (10, "and the plumber?")))))

    async def go():
        await p.drain_mail()
        await until(lambda: runner.running, "a session ran")
        await p.drain_mail()
        again = len(p._bg)
        runner.release.set()
        await until(lambda: not p._bg, "every turn ended")
        return again
    assert asyncio.run(go()) == 1
    [(prompt, _)] = runner.calls
    assert ("What fan says below was sent at Thu 2026-10-01 16:40 and reaches me only now.\n\n"
            "The conversation so far:\n\n    Thu 2026-10-01 16:40 fan: is it paid?") in prompt
    assert slack.replies == ["Both noted."] and kept(store) == []


def test_a_line_sent_while_its_turn_waits_for_a_place_joins_what_the_cap_kept(tmp_path, monkeypatch, fake_time):
    """After local midnight, through slack_loop: fan sends a third line to
    his DM, where the cap kept two, while mei's session holds the one place.
    A pass then passes over his DM, and his turn takes all three."""
    fake_time.at = datetime(2026, 10, 2, 7, 1, tzinfo=timezone.utc)  # 00:01 in Los Angeles
    p, store, runner, meis = one_slot_behind_a_dm(ConversationSlack(history=[]), tmp_path, monkeypatch)
    runner.reports = [answer("Noted."), answer("All three.")]
    store.create_task(None, "D1", "conversation", kind="dm")
    store.settle(Settled(capped=tuple(keep(store, dm(f"{AT + i:.1f}", text))
                                      for i, text in ((0, "one"), (10, "two")))))
    third = dm(f"{fake_time.at.timestamp():.1f}", "three")

    async def go():
        loop = asyncio.create_task(p.slack_loop())
        p.slack_queue.put_nowait(meis)
        await asyncio.sleep(0.05)
        keep(store, third)
        p.slack_queue.put_nowait(third)
        await asyncio.sleep(0.05)
        await p.drain_mail()
        runner.release.set()
        await until(lambda: not p._bg and p.slack_queue.empty(), "every turn ended")
        loop.cancel()
    asyncio.run(go())
    assert len(runner.calls) == 2
    assert ("Thu 2026-10-01 16:40 fan: one\n    Thu 2026-10-01 16:40 fan: two\n\nfan now says:\n\n    three"
            in runner.calls[1][0])
    assert p.slack.replies == ["Noted.", "All three."] and kept(store) == []


def test_sessions_claude_code_refused_count_nothing_and_a_message_then_is_held(tmp_path, monkeypatch, fake_time):
    """250 refused sessions today, past the cap of 200: a message still runs
    a session, which Claude Code refuses too, and it is held, never capped."""
    fake_time.at = datetime.fromtimestamp(AT, timezone.utc)
    runner = Limited()
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    for _ in range(250):
        store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=1,
                         cost_usd=0.0, status="refused", error=LIMIT)
    keep(store, dm(f"{AT:.1f}", "is it paid?"))
    settle(p, p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    fake_time.at += timedelta(minutes=1)
    a_pass(p)
    assert len(runner.calls) == 2 and slack.replies == [main.HELD]
    assert [r[1] for r in kept(store)] == ["held"]


class RefusesOnce(ConversationSlack):
    """A Slack that refuses her posts, as one rate limiting does, until
    `refuse` is cleared."""

    def __init__(self):
        super().__init__(members=["U1", "U2", "UBOT"], history=[])
        self.refuse = True

    async def reply(self, thread_ts, text, channel=None):
        if self.refuse:
            raise RuntimeError("ratelimited")
        await super().reply(thread_ts, text, channel)


@pytest.mark.parametrize("note", ["the cap's", "a hold's"])
@pytest.mark.parametrize("deleted", ["its one message", "its one message, in a group DM",
                                     "one of two, the other sent before", "one of two, the other sent after"])
def test_her_note_on_the_cap_or_a_hold_is_posted_only_while_a_message_it_tells_of_stands(
        tmp_path, monkeypatch, fake_time, note, deleted):
    """Her note that she will come back to it (CAPPED_NOTE, HELD, and in a
    group DM CAPPED_GROUP, HELD_GROUP), refused by Slack, and the message it
    was recorded for deleted before the next pass: with nothing the cap or
    the hold keeps left there, nothing is posted and the day's mark goes
    with it, so that a message kept there later that day is told; with
    another kept there, sent before the deletion or after it while the note
    was owed, the note tells of that one and is posted, once."""
    slack = RefusesOnce()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("never")), monkeypatch)
    at = fake_time.at.timestamp()
    group = "group" in deleted
    channel = "G1" if group else "D1"

    def line(ts, text):
        return dm(ts, text, channel_type="mpim" if group else "im", channel=channel)
    first, other = line(f"{at - 60:.1f}", "is it paid?"), line(f"{at - 30:.1f}", "and the plumber?")
    if note == "the cap's":
        text, state = main.CAPPED_GROUP if group else main.CAPPED_NOTE, "capped"
    else:
        text, state = main.HELD_GROUP if group else main.HELD, "held"
    task = store.create_task(None, channel, "conversation", kind="dm")
    if note == "the cap's":
        monkeypatch.setattr(p.cfg, "daily_run_cap", 1)
        store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                         cost_usd=0.1, status="ok")
    else:
        store.set_meta("held_since", (fake_time.at - timedelta(minutes=5)).isoformat())

    def kept_back(ev):
        # kept by the cap or the hold, as a turn or the hold's confirmation keeps it
        keep(store, ev)
        if note == "the cap's":
            settle(p, p.handle_slack(ev))
        else:
            store._exec("UPDATE unanswered SET state='held' WHERE state='due'")
            settle(p, p._claude_refused(store.get_task_by_thread(channel, "conversation")))
    kept_back(first)
    if deleted == "one of two, the other sent before":
        kept_back(other)
    assert slack.replies == [] and len(store.pending_deliveries(task)) == 1

    async def gone():
        p._withdraw(channel, first.payload["ts"])
    settle(p, gone())
    if deleted == "one of two, the other sent after":
        kept_back(other)
    slack.refuse = False
    settle(p, p.deliver_pending())
    if deleted.startswith("one of two"):
        assert slack.replies == [text] and kept(store) == [(other.payload["ts"], state, 0, None)]
        return
    assert slack.replies == [] and kept(store) == [] and not store.pending_deliveries(task)
    kept_back(line(f"{at:.1f}", "and the plumber?"))
    assert slack.replies == [text] and [r[0] for r in kept(store)] == [f"{at:.1f}"]


@pytest.mark.parametrize("note", ["the cap's", "a hold's"])
def test_her_note_of_the_day_before_not_posted_leaves_the_mark_of_today(tmp_path, monkeypatch, fake_time, note):
    """Her note on the cap or a hold recorded the day before and owed since,
    the hold lasting since, with nothing it could tell of kept there now:
    not posted, and the mark a note said there today left stays, so that
    nothing more is said there today."""
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("never")), monkeypatch)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    today = fake_time.at.astimezone(p.cfg.zone).date().isoformat()
    mark = ("cap_noted" if note == "the cap's" else "held_noted") + f":{task}"
    if note == "a hold's":
        store.set_meta("held_since", (fake_time.at - timedelta(days=1, minutes=5)).isoformat())
    store.record_run(kind="note", task_id=task, session_id=None,
                     started_at=(fake_time.at - timedelta(days=1)).isoformat(timespec="seconds"), exit_code=None,
                     cost_usd=0.0, status="ok", notified=0,
                     result_text=main.CAPPED_NOTE if note == "the cap's" else main.HELD)
    store.set_meta(mark, today)
    settle(p, p.deliver_pending())
    assert slack.replies == [] and not store.pending_deliveries(task) and store.get_meta(mark) == today


@pytest.mark.parametrize("since", ["deleted", "taken up since"])
def test_her_note_on_the_cap_with_nothing_capped_left_says_why_in_the_log(tmp_path, fake_time, caplog, since):
    """Her note on the cap owed the day it was said, with nothing capped
    left in its conversation: not posted, and its day's mark goes. When a
    take-up that day, the cap having been only busy, answered what it
    kept, which is then still kept there, answered, the log says so, and
    the answer follows alone; with nothing kept there, its messages were
    deleted."""
    import logging
    slack = ConversationSlack(history=[])
    p, store = make(tmp_path, slack, email_triage=False)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    note = store.record_run(kind="note", task_id=task, session_id=None, started_at=utcnow(), exit_code=None,
                            cost_usd=0.0, status="ok", notified=0, result_text=main.CAPPED_NOTE)
    mark = f"cap_noted:{task}"
    store.set_meta(mark, fake_time.at.astimezone(p.cfg.zone).date().isoformat())
    if since == "taken up since":
        line = keep(store, dm(f"{fake_time.at.timestamp() - 60:.1f}", "is it paid?"))
        store.record_run(kind="agent", task_id=task, session_id="s1", started_at=utcnow(), exit_code=0,
                         cost_usd=0.1, status="ok", result_text="Yes, paid.", notified=0,
                         settled=Settled(answered=(line,)))
    with caplog.at_level(logging.INFO, logger="wanda"):
        settle(p, p.deliver_pending())
    assert slack.replies == ([] if since == "deleted" else ["Yes, paid."]) and kept(store) == []
    assert f"run {note} not posted: its messages were {since}" in caplog.messages
    assert store.get_meta(mark) is None


def _owed_note(store, task, line, note, started_at=None):
    # her note on the cap or a hold, owed as when Slack refused it, said for
    # the kept message `line`
    kept_as = Settled(capped=(line,)) if note == "the cap's" else Settled(held=((line, "s1"),))
    return store.record_run(kind="note", task_id=task, session_id=None, started_at=started_at or utcnow(),
                            exit_code=None, cost_usd=0.0, status="ok", notified=0, settled=kept_as,
                            result_text=main.CAPPED_NOTE if note == "the cap's" else main.HELD)


@pytest.mark.parametrize("note", ["the cap's", "a hold's"])
@pytest.mark.parametrize("new", ["waiting for its turn", "answered since"])
def test_her_note_whose_message_was_deleted_is_not_posted_for_a_new_one_there(tmp_path, fake_time, caplog, note,
                                                                               new):
    """Her note on the cap or a hold owed, the hold lasting, its message
    deleted, and a new message sent there after it, which neither the cap
    nor the hold keeps, waiting for its turn or answered by a session whose
    answer comes after the note: not posted, its day's mark goes, and the
    log says its messages were deleted."""
    import logging
    slack = ConversationSlack(history=[])
    p, store = make(tmp_path, slack, email_triage=False)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    at = fake_time.at.timestamp()
    store.set_meta("held_since", (fake_time.at - timedelta(minutes=10)).isoformat())
    line = keep(store, dm(f"{at - 360:.1f}", "is it paid?"))
    run = _owed_note(store, task, line, note, started_at=(fake_time.at - timedelta(minutes=5)).isoformat())
    mark = ("cap_noted" if note == "the cap's" else "held_noted") + f":{task}"
    store.set_meta(mark, fake_time.at.astimezone(p.cfg.zone).date().isoformat())

    async def withdraw():
        p._withdraw(*line)
    settle(p, withdraw())
    again = keep(store, dm(f"{at - 60:.1f}", "is it paid? (the plumber)"))
    if new == "answered since":
        store.record_run(kind="agent", task_id=task, session_id="s1", started_at=utcnow(), exit_code=0,
                         cost_usd=0.1, status="ok", result_text="Yes, paid.", notified=0,
                         settled=Settled(answered=(again,)))
    with caplog.at_level(logging.INFO, logger="wanda"):
        settle(p, p.deliver_pending())
    assert slack.replies == ([] if new == "waiting for its turn" else ["Yes, paid."])
    assert not store.pending_deliveries(task) and store.get_meta(mark) is None
    assert f"run {run} not posted: its messages were deleted" in caplog.messages


@pytest.mark.parametrize("note", ["the cap's", "a hold's"])
def test_her_note_said_late_in_the_evening_takes_the_mark_of_its_local_day(tmp_path, fake_time, note):
    """Her note on the cap or a hold said at 23:30 in Los Angeles, already
    the next day in UTC, and its message deleted: the mark of the local day
    it was said goes."""
    fake_time.at = datetime(2026, 10, 2, 6, 30, tzinfo=timezone.utc)
    slack = ConversationSlack(history=[])
    p, store = make(tmp_path, slack, email_triage=False)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.set_meta("held_since", (fake_time.at - timedelta(minutes=5)).isoformat())
    line = keep(store, dm(f"{fake_time.at.timestamp() - 60:.1f}", "is it paid?"))
    mark = ("cap_noted" if note == "the cap's" else "held_noted") + f":{task}"
    _owed_note(store, task, line, note)
    store.set_meta(mark, "2026-10-01")

    async def withdraw():
        p._withdraw(*line)
    settle(p, withdraw())
    settle(p, p.deliver_pending())
    assert slack.replies == [] and store.get_meta(mark) is None


@pytest.mark.parametrize("note", ["the cap's", "a hold's"])
@pytest.mark.parametrize("beside", ["another DM", "a thread in the same DM"])
def test_her_note_is_kept_up_only_by_what_stands_in_its_own_conversation(tmp_path, fake_time, note, beside):
    """The cap and the hold reach every conversation at once: two, each
    owed her note, fan's DM and mei's, or a DM and a thread in it. The
    first one's message deleted: only the second's note is posted."""
    slack = ConversationSlack(history=[])
    p, store = make(tmp_path, slack, email_triage=False)
    at = fake_time.at.timestamp()
    store.set_meta("held_since", (fake_time.at - timedelta(minutes=5)).isoformat())
    top = f"{at - 600:.1f}"
    d1 = store.create_task(None, "D1", "conversation", kind="dm")
    gone = keep(store, dm(f"{at - 60:.1f}", "is it paid?"))
    if beside == "another DM":
        other = store.create_task(None, "D2", "conversation", kind="dm")
        stands = keep(store, dm(f"{at - 30:.1f}", "and the plumber?", channel="D2", user="U2"))
    else:
        other = store.create_task(None, "D1", top, kind="dm", reply_thread=top)
        stands = keep(store, dm(f"{at - 30:.1f}", "and the plumber?", thread=top))
    _owed_note(store, d1, gone, note)
    _owed_note(store, other, stands, note)

    async def withdraw():
        p._withdraw(*gone)
    settle(p, withdraw())
    settle(p, p.deliver_pending())
    assert slack.replies == [main.CAPPED_NOTE if note == "the cap's" else main.HELD]
    assert slack.channels == ["D2"] if beside == "another DM" else slack.threads == [top]
    assert [r[0] for r in kept(store)] == [stands[1]]


@pytest.mark.parametrize("wait", ["the cap's, midnight come", "the cap's, said at 23:30, midnight come",
                                  "a hold's, the hold ended", "a hold's, a later hold"])
def test_her_note_on_the_cap_or_a_hold_past_what_it_said_she_would_wait_for_is_not_posted(
        tmp_path, fake_time, caplog, wait):
    """Her note on the cap said the day before, or at 23:30 in Los Angeles
    and checked at 00:30, both the same day in UTC, or on a hold that has
    ended since, with a later hold or none in place now, what it kept still
    kept there: not posted, since that is taken up now and her answer comes
    alone. The log says so, and a mark said there since stays."""
    import logging
    if "23:30" in wait:
        fake_time.at = datetime(2026, 10, 2, 7, 30, tzinfo=timezone.utc)  # 00:30 in Los Angeles
    slack = ConversationSlack(history=[])
    p, store = make(tmp_path, slack, email_triage=False)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    note = "the cap's" if wait.startswith("the cap's") else "a hold's"
    line = keep(store, dm(f"{fake_time.at.timestamp() - 3600:.1f}", "is it paid?"))
    said = fake_time.at - (timedelta(hours=1) if "23:30" in wait else timedelta(days=1) if note == "the cap's"
                           else timedelta(minutes=30))
    run = _owed_note(store, task, line, note, started_at=said.isoformat())
    if wait == "a hold's, a later hold":
        store.set_meta("held_since", (fake_time.at - timedelta(minutes=5)).isoformat())
    mark = ("cap_noted" if note == "the cap's" else "held_noted") + f":{task}"
    today = fake_time.at.astimezone(p.cfg.zone).date().isoformat()
    store.set_meta(mark, today)
    with caplog.at_level(logging.INFO, logger="wanda"):
        settle(p, p.deliver_pending())
    assert slack.replies == [] and not store.pending_deliveries(task) and store.get_meta(mark) == today
    assert [r[1] for r in kept(store)] == ["capped" if note == "the cap's" else "held"]
    assert f"run {run} not posted: its messages were taken up since" in caplog.messages


def test_her_note_on_the_cap_owed_at_midnight_is_not_posted_before_her_answer(tmp_path, monkeypatch, fake_time):
    """Her note on the cap, refused by Slack before midnight and owed past
    it: the first pass after midnight takes up what the cap kept, and her
    answer comes alone."""
    fake_time.at = datetime.fromtimestamp(AT + 30, timezone.utc)
    slack = RefusesOnce()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("Yes, paid.")), monkeypatch)
    monkeypatch.setattr(p.cfg, "daily_run_cap", 1)
    store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                     cost_usd=0.1, status="ok")
    ev = dm(f"{AT:.1f}", "is it paid?")
    keep(store, ev)
    settle(p, p.handle_slack(ev))
    assert slack.replies == [] and [r[1] for r in kept(store)] == ["capped"]
    fake_time.at = datetime(2026, 10, 2, 7, 1, tzinfo=timezone.utc)  # 00:01 in Los Angeles
    slack.refuse = False
    a_pass(p)
    assert slack.replies == ["Yes, paid."] and kept(store) == []


def test_her_note_on_a_hold_owed_when_the_hold_ends_is_not_posted_before_her_answer(tmp_path, monkeypatch,
                                                                                     fake_time):
    """Her note on a hold, refused by Slack and owed when a session
    elsewhere ends the hold: the hold's end takes up what it held, and her
    answer comes alone."""
    fake_time.at = datetime.fromtimestamp(AT, timezone.utc)
    runner = Limited(answer("Yes, paid."))
    slack = RefusesOnce()
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    ev = dm(f"{AT:.1f}", "is it paid?")
    keep(store, ev)
    settle(p, p.handle_slack(ev))
    fake_time.at += timedelta(minutes=1)
    a_pass(p)  # the hold's try, refused again, confirms it: her note, refused by Slack
    assert slack.replies == [] and len(store.pending_deliveries()) == 1

    async def ended():
        # a session elsewhere ran: the hold ends, its take-up is started,
        # and a pass comes while it waits for its turn
        p._claude_ran(datetime.now(timezone.utc))
        await p.drain_mail()
        await until(lambda: not p._bg, "every turn ended")
        await reactions_end(p)
    runner.back = True
    slack.refuse = False
    fake_time.at += timedelta(seconds=10)
    asyncio.run(ended())
    assert store.get_meta("held_since") is None
    assert slack.replies == ["Yes, paid."] and kept(store) == []


def test_a_message_deleted_and_sent_again_during_a_hold_is_told(tmp_path, monkeypatch, fake_time, caplog):
    """The hold confirmed and her note on it refused by Slack; mei's message
    deleted and sent again, as a correction is. Its turn finds the note
    with nothing held there and does not post it, logging its message
    deleted, and its mark goes; the turn is refused and holds the new
    message, which is told."""
    import logging
    slack = RefusesOnce()
    p, store, _ = memory_processor(tmp_path, slack, Limited(), monkeypatch)
    at = fake_time.at.timestamp()
    task = store.create_task(None, "D1", "conversation", kind="dm")
    first = dm(f"{at - 60:.1f}", "is it paid?")
    keep(store, first)
    store._exec("UPDATE unanswered SET state='held'")
    store.set_meta("held_since", (fake_time.at - timedelta(minutes=5)).isoformat())
    settle(p, p._claude_refused(store.get_task_by_thread("D1", "conversation")))
    assert slack.replies == [] and len(store.pending_deliveries(task)) == 1
    [note] = [r["id"] for r in store.pending_deliveries(task)]

    async def withdraw():
        p._withdraw("D1", first.payload["ts"])
    settle(p, withdraw())
    slack.refuse = False
    again = dm(f"{at - 10:.1f}", "is it paid yet?")
    keep(store, again)
    with caplog.at_level(logging.INFO, logger="wanda"):
        settle(p, p.handle_slack(again))
    assert f"run {note} not posted: its messages were deleted" in caplog.messages
    assert slack.replies == [main.HELD] and [(r[0], r[1]) for r in kept(store)] == [(again.payload["ts"], "held")]


def test_an_answer_in_the_words_of_her_note_on_a_hold_is_posted_though_nothing_is_held(tmp_path, fake_time):
    """A session's answer is checked as an answer is, by the messages it
    answers, whatever its words: only her own note on the cap or a hold is
    checked against what the cap or the hold keeps there."""
    slack = ConversationSlack(history=[])
    p, store = make(tmp_path, slack, email_triage=False)
    task = store.create_task(None, "D1", "conversation", kind="dm")
    line = keep(store, dm(f"{fake_time.at.timestamp() - 60:.1f}", "can you get to anything right now?"))
    store.record_run(kind="agent", task_id=task, session_id="s1", started_at=utcnow(), exit_code=0, cost_usd=0.1,
                     status="ok", result_text=main.HELD, notified=0, settled=Settled(answered=(line,)))
    settle(p, p.deliver_pending())
    assert slack.replies == [main.HELD] and kept(store) == []


@pytest.mark.parametrize("then", ["refused again", "running again"])
def test_a_usage_limit_says_nothing_until_a_try_a_minute_on_confirms_it(tmp_path, monkeypatch, fake_time, then):
    """fan's DM and a group DM meet Claude Code's usage limit: each message
    held, nothing said. A minute on, a pass's try meets it again: her note in
    each (HELD, HELD_GROUP), and from then at once where a message is newly
    held, once a day there. One that has cleared by then says nothing: the
    try answers, and the hold's end takes up the rest."""
    fake_time.at = datetime.fromtimestamp(AT, timezone.utc)
    runner = Limited(answer("Yes, paid."), answer("7 is fine."))
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    for ev in (dm(f"{AT:.1f}", "is it paid?"), dm(f"{AT + 1:.1f}", "dinner at 7?", channel_type="mpim", channel="G1")):
        keep(store, ev)
        settle(p, p.handle_slack(ev))
    a_pass(p)
    assert slack.replies == [] and [r[1] for r in kept(store)] == ["held", "held"] and len(runner.calls) == 2
    fake_time.at += timedelta(minutes=1)
    runner.back = then == "running again"
    a_pass(p)
    if then == "running again":
        assert slack.replies == ["Yes, paid.", "7 is fine."] and slack.channels == ["D1", "G1"]
        assert kept(store) == [] and store.get_meta("held_since") is None
        return
    assert len(runner.calls) == 3 and "is it paid?" in runner.calls[2][0]
    assert slack.replies == [main.HELD, main.HELD_GROUP] and slack.channels == ["D1", "G1"]
    for ev in (dm(f"{AT + 70:.1f}", "and the plumber?"), dm(f"{AT + 71:.1f}", "out Tuesday", channel="D2", user="U2")):
        keep(store, ev)
        settle(p, p.handle_slack(ev))
    assert slack.replies[2:] == [main.HELD] and slack.channels[2:] == ["D2"]


def test_the_hold_is_tried_at_each_pass_one_try_at_a_time_and_at_a_start(tmp_path, monkeypatch, fake_time):
    """From a minute after the refusal, each pass tries the message held
    longest, none while a try runs, and a start's first pass too."""
    fake_time.at = datetime.fromtimestamp(AT, timezone.utc)
    runner = Limited()
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    for ev in (dm(f"{AT:.1f}", "is it paid?"), dm(f"{AT + 1:.1f}", "out Tuesday", channel="D2", user="U2")):
        keep(store, ev)
        settle(p, p.handle_slack(ev))
    fake_time.at += timedelta(seconds=30)
    a_pass(p)
    assert len(runner.calls) == 2
    fake_time.at += timedelta(seconds=30)
    runner.gate = asyncio.Event()

    async def two_passes():
        await p.drain_mail()
        await until(lambda: runner.running, "a try ran")
        await p.drain_mail()
        await asyncio.sleep(0.05)
        running = runner.running
        runner.gate.set()
        await until(lambda: not p._bg, "every turn ended")
        return running
    assert asyncio.run(two_passes()) == 1
    runner.gate = None
    assert len(runner.calls) == 3 and "is it paid?" in runner.calls[2][0]
    fake_time.at += timedelta(minutes=1)
    a_pass(p)
    assert len(runner.calls) == 4
    started = Processor(p.cfg, store, asyncio.Queue(), slack, runner)
    fake_time.at += timedelta(minutes=1)
    a_pass(started)
    assert len(runner.calls) == 5 and [r[1] for r in kept(store)] == ["held", "held"]


def test_a_message_held_is_answered_within_a_pass_of_claude_code_running_again(tmp_path, monkeypatch, fake_time):
    """Forty minutes refused at each pass, her note once; then a pass's try
    answers it."""
    fake_time.at = datetime.fromtimestamp(AT, timezone.utc)
    runner = Limited(answer("Yes, paid."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    keep(store, dm(f"{AT:.1f}", "is it paid?"))
    settle(p, p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    for _ in range(39):
        fake_time.at += timedelta(minutes=1)
        a_pass(p)
    assert len(runner.calls) == 40 and slack.replies == [main.HELD]
    fake_time.at += timedelta(minutes=1)
    runner.back = True
    a_pass(p)
    assert slack.replies == [main.HELD, "Yes, paid."] and kept(store) == []


def test_a_hold_of_hours_says_no_more_and_ends_at_the_first_session_that_runs(tmp_path, monkeypatch, fake_time):
    """Five hours of tries: her note once, her reaction kept, Claude Code's
    reason in one `failed` alert, the tries in none. Then a clock session in
    mei's DM runs, which ends the hold: fan's message is taken up, framed
    late."""
    fake_time.at = datetime.fromtimestamp(AT, timezone.utc)
    runner = Limited(answer("Morning, mei."), answer("Yes, paid."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    monkeypatch.setattr(main, "LATE_TURN_S", LATE_TURN_S)
    keep(store, dm(f"{AT:.1f}", "is it paid?"))
    settle(p, p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    for _ in range(30):
        fake_time.at += timedelta(minutes=10)
        a_pass(p)
    assert len(runner.calls) == 31 and slack.replies == [main.HELD] and slack.unreacted == []
    [alert] = [a for a in slack.alerts if "message session(s) failed" in a]
    assert re.fullmatch(r"1 message session\(s\) failed \(usage limit\): run 1 at 16:40, Claude Code said: "
                        r"You've hit your limit · resets 5pm, not tried again, held until Claude Code runs again",
                        alert)
    assert json.loads(store.get_meta("failed_runs")) == []
    runner.back = True
    store.create_task(None, "D2", "conversation", kind="dm")
    meis = store.get_task_by_thread("D2", "conversation")

    async def look():
        await p.memory_turn(meis, "(a look)", fake_time.at.astimezone(p.cfg.zone), channel="D2", reply_thread=None,
                            owed=False)
        await until(lambda: not p._bg, "every turn ended")
    asyncio.run(look())
    assert slack.replies == [main.HELD, "Morning, mei.", "Yes, paid."] and kept(store) == []
    assert "What fan says below was sent at 16:40 and reaches me only now." in runner.calls[-1][0]


def test_her_note_on_a_hold_comes_once_a_day_where_someone_has_written_since(tmp_path, monkeypatch, fake_time):
    """Held at 23:30 and confirmed at 23:31, her note in fan's DM. Tries past
    midnight with nobody writing say nothing more; mei writing the next
    morning is told at once, in her DM alone. A second hold that day is said
    again there."""
    fake_time.at = datetime(2026, 10, 2, 6, 30, tzinfo=timezone.utc)  # 23:30 in Los Angeles
    runner = Limited(answer("Yes, paid."), answer("Noted."), answer("Sure."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)

    def says(ev):
        keep(store, ev)
        settle(p, p.handle_slack(ev))
    says(dm(f"{fake_time.at.timestamp():.1f}", "is it paid?"))
    fake_time.at += timedelta(minutes=1)
    a_pass(p)
    assert slack.replies == [main.HELD]
    for _ in range(3):
        fake_time.at += timedelta(minutes=20)
        a_pass(p)
    assert slack.replies == [main.HELD] and len(runner.calls) == 5
    fake_time.at = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)  # 08:00
    says(dm(f"{fake_time.at.timestamp():.1f}", "out Tuesday", channel="D2", user="U2"))
    assert slack.replies == [main.HELD, main.HELD] and slack.channels == ["D1", "D2"]
    runner.back = True
    fake_time.at += timedelta(minutes=1)
    a_pass(p)
    assert slack.replies[2:] == ["Yes, paid.", "Noted."] and kept(store) == []
    runner.back = False
    fake_time.at += timedelta(hours=1)
    says(dm(f"{fake_time.at.timestamp():.1f}", "and on Wednesday?", channel="D2", user="U2"))
    fake_time.at += timedelta(minutes=1)
    a_pass(p)
    assert slack.replies[4:] == [main.HELD] and slack.channels[4:] == ["D2"]


def test_her_note_on_a_hold_is_said_once_in_the_households_day_across_a_utc_midnight(tmp_path, monkeypatch,
                                                                                      fake_time):
    """Held and confirmed at 16:40 in Los Angeles, before UTC midnight; fan
    writes again at 17:10, after it, still the same day there: nothing more
    is said."""
    fake_time.at = datetime.fromtimestamp(AT, timezone.utc)
    runner = Limited()
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    keep(store, dm(f"{AT:.1f}", "is it paid?"))
    settle(p, p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    fake_time.at += timedelta(minutes=1)
    a_pass(p)
    assert slack.replies == [main.HELD]
    fake_time.at = datetime.fromtimestamp(AT + 1800, timezone.utc)
    keep(store, dm(f"{AT + 1800:.1f}", "and the plumber?"))
    settle(p, p.handle_slack(dm(f"{AT + 1800:.1f}", "and the plumber?")))
    assert slack.replies == [main.HELD] and [r[1] for r in kept(store)] == ["held", "held"]


def test_a_message_the_cap_kept_that_a_hold_then_refuses_is_told_at_once(tmp_path, monkeypatch, fake_time):
    """Kept until just after midnight, its take-up then meets Claude Code's
    limit while a hold stands confirmed: the cap's word has failed, so her
    note on the hold goes there at that pass."""
    fake_time.at = datetime(2026, 10, 2, 7, 1, tzinfo=timezone.utc)  # 00:01 in Los Angeles
    runner = Limited()
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    store.settle(Settled(capped=(keep(store, dm(f"{AT:.1f}", "is it paid?")),)))
    since = (fake_time.at - timedelta(minutes=5)).isoformat()
    store.set_meta("held_since", since)
    store.set_meta("held_confirmed", since)
    a_pass(p)
    assert len(runner.calls) == 1 and slack.replies == [main.HELD] and [r[1] for r in kept(store)] == ["held"]


def test_a_message_held_with_no_hold_standing_is_taken_up_at_a_pass(tmp_path, monkeypatch):
    """As when a stop cancelled the take-ups the hold's end made, the hold
    gone with it: the next pass takes the held message up."""
    runner = RecordingRunner(answer("Yes, paid."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    store.settle(Settled(held=((keep(store, dm(f"{AT:.1f}", "is it paid?")), None),)))
    a_pass(p)
    assert slack.replies == ["Yes, paid."] and kept(store) == []


def test_her_note_on_a_hold_goes_nowhere_more_once_the_hold_has_ended(tmp_path, monkeypatch, fake_time):
    """Confirming the hold tells each conversation where a message is held,
    one after another; a session that ends the hold while one is posted
    leaves the rest unsaid, and no mark of a note that day, so that a second
    hold says it again."""
    fake_time.at = datetime.fromtimestamp(AT, timezone.utc)
    runner, slack = Held(answer("Yes, paid."), answer("Noted.")), HeldNote(note=main.HELD, history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    for channel, user, text in (("D1", "U1", "is it paid?"), ("D2", "U2", "out Tuesday")):
        store.create_task(None, channel, "conversation", kind="dm")
        store.settle(Settled(held=((keep(store, dm(f"{AT:.1f}", text, channel=channel, user=user)), None),)))
    store.set_meta("held_since", (fake_time.at - timedelta(minutes=3)).isoformat())

    async def go():
        confirming = asyncio.create_task(p._claude_refused())
        await until(lambda: slack.waiting, "her first note is being posted")
        p._claude_ran(fake_time.at)
        # the other conversation's take-up runs its session, its message
        # still held, as the note's post ends
        await until(lambda: runner.running, "a take-up's session runs")
        slack.gate.set()
        await confirming
        runner.release.set()
        await until(lambda: not p._bg, "every turn ended")
        await reactions_end(p)
    asyncio.run(go())
    assert slack.replies.count(main.HELD) == 1 and sorted(slack.replies) == sorted([main.HELD, "Yes, paid.", "Noted."])
    assert store.get_meta("held_since") is None and store.meta_starting("held_noted:") == {}


def test_a_session_that_began_after_the_refusal_in_the_same_second_ends_the_hold(tmp_path, monkeypatch, fake_time):
    """The starts are compared to the microsecond: one second's clock would
    read this session as begun before the refusal."""
    fake_time.at = datetime.fromtimestamp(AT + 0.6, timezone.utc)
    runner = RecordingRunner(answer("Yes, paid."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    store.set_meta("held_since", datetime.fromtimestamp(AT + 0.2, timezone.utc).isoformat())
    keep(store, dm(f"{AT:.1f}", "is it paid?"))
    settle(p, p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    assert slack.replies == ["Yes, paid."] and store.get_meta("held_since") is None


def test_a_wake_that_cannot_start_holds_back_no_held_message(tmp_path, monkeypatch, fake_time):
    """mei's 07:00 reminder is the hold's oldest waiting thing, but her DM
    will not open, so it starts no session at any tick: fan's 08:00 message,
    held, is tried at the pass after the tick, and answered once Claude Code
    runs again."""
    fake_time.at = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)  # 08:00 in Los Angeles
    runner = Limited(answer("Yes, paid."))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    tick = ticking(p, fake_time)

    async def unopened(user):
        raise RuntimeError("channel_not_found")
    p.slack.dm_channel = unopened
    line = dm(f"{fake_time.at.timestamp():.1f}", "is it paid?")
    keep(store, line)
    settle(p, p.handle_slack(line))
    fake_time.at += timedelta(minutes=1)
    tick(wake("b6647b", "2026-10-01T07:00", person="U2", asked="mei"))
    runner.back = True
    a_pass(p)
    assert p.slack.replies == ["Yes, paid."] and kept(store) == []


def test_a_new_message_in_a_held_conversation_runs_one_session_for_both(tmp_path, monkeypatch, fake_time):
    fake_time.at = datetime.fromtimestamp(AT, timezone.utc)
    runner = Limited(answer("Yes, and at 5."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    for i, text in ((0, "is it paid?"), (30, "and the plumber?")):
        keep(store, dm(f"{AT + i:.1f}", text))
        settle(p, p.handle_slack(dm(f"{AT + i:.1f}", text)))
        runner.back = True
    assert len(runner.calls) == 2
    assert "16:40 fan: is it paid?\n\nfan now says:\n\n    and the plumber?" in runner.calls[1][0]
    assert slack.replies == ["Yes, and at 5."] and kept(store) == []


@pytest.mark.parametrize("apart", [5, 0.6], ids=["seconds apart", "in one second"])
def test_a_session_that_began_before_the_refusal_ends_no_hold(tmp_path, monkeypatch, fake_time, apart):
    """Two at once: mei's session works when fan's is refused, however soon
    after hers began; hers answers, and the hold stands, so the try a minute
    on confirms it, and her note goes to fan's DM alone."""
    class Mixed(Limited):
        """Refuses every session but mei's, which waits for `release`."""

        def __init__(self):
            super().__init__(answer("Noted."))
            self.release = asyncio.Event()

        async def run(self, prompt, **kw):
            if "out Tuesday" not in prompt:
                return await super().run(prompt, **kw)
            await self.release.wait()
            return await RecordingRunner.run(self, prompt, **kw)

    fake_time.at = datetime.fromtimestamp(AT + 0.2, timezone.utc)
    runner = Mixed()
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)

    async def go():
        meis = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "out Tuesday", channel="D2", user="U2")))
        await asyncio.sleep(0.05)
        fake_time.at += timedelta(seconds=apart)
        keep(store, dm(f"{AT + apart:.1f}", "is it paid?"))
        await p.handle_slack(dm(f"{AT + apart:.1f}", "is it paid?"))
        runner.release.set()
        await meis
        await until(lambda: not p._bg, "every turn ended")
        await reactions_end(p)
    asyncio.run(go())
    assert slack.replies == ["Noted."] and len(runner.calls) == 2 and store.get_meta("held_since")
    fake_time.at += timedelta(minutes=1)
    a_pass(p)
    assert slack.replies == ["Noted.", main.HELD] and slack.channels == ["D2", "D1"]


def wake(about, by, person="U1", asked="fan"):
    """A timed reminder come due for the clock to give."""
    return clock.Wake(f"clock:due:{about}:{by}:{asked}", person, f"It is {by[11:]}, and this has come due:\n"
                      f"    `trajectory:{about}`  {by}, today  I undertook to remind {asked}", about, by, asked)


def ticking(p, fake_time):
    """The clock's tick with these wakes, at the time it stands at, and each
    session it starts, to its end."""
    async def dm_channel(user):
        return {"U1": "D1", "U2": "D2"}[user]
    p.slack.dm_channel = dm_channel

    def tick(*wakes):
        async def go():
            p._wake(list(wakes), fake_time.at.astimezone(p.cfg.zone))
            await until(lambda: not p._bg, "every turn ended")
        asyncio.run(go())
    return tick


def test_a_wake_claude_code_refused_is_released_and_is_the_holds_try(tmp_path, monkeypatch, fake_time):
    """With nothing held, a reminder Claude Code refuses is released, as one
    the budget refuses is; no other wake starts while the hold lasts but as
    its try. From a minute on the wake due longest is the try, and once one
    runs the hold has ended and the other wakes."""
    runner = Limited(answer("It is 8: the bins."), answer("It is 8:01: the gift."))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    tick = ticking(p, fake_time)
    bins, gift = wake("b6647b", "2026-10-01T08:00"), wake("c7758c", "2026-10-01T08:01")
    tick(bins)
    assert store.get_meta(bins.key) == "" and store.get_meta("held_since") and len(runner.calls) == 1
    fake_time.at += timedelta(seconds=30)
    tick(bins, gift)
    assert len(runner.calls) == 1
    fake_time.at += timedelta(seconds=30)
    tick(gift, bins)
    fake_time.at += timedelta(minutes=1)
    runner.back = True
    tick(gift, bins)
    tick(gift)
    assert ["b6647b" in prompt for prompt, _ in runner.calls] == [True, True, True, False]
    assert p.slack.replies == ["It is 8: the bins.", "It is 8:01: the gift."] and not store.get_meta("held_since")


@pytest.mark.parametrize("due", ["08:00", "07:58"])
def test_the_holds_try_is_what_has_waited_longest_a_message_or_a_wake(tmp_path, monkeypatch, fake_time, due):
    """fan's message held since 07:59, and a reminder for mei: due at 08:00,
    the clock's tick starts no wake and the pass tries the message; due at
    07:58, the tick starts the reminder as the try and the pass tries
    nothing."""
    fake_time.at = datetime(2026, 10, 1, 14, 59, tzinfo=timezone.utc)  # 07:59 in Los Angeles
    runner = Limited()
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    tick = ticking(p, fake_time)
    keep(store, dm(f"{fake_time.at.timestamp():.1f}", "is it paid?"))
    settle(p, p.handle_slack(dm(f"{fake_time.at.timestamp():.1f}", "is it paid?")))
    fake_time.at += timedelta(minutes=1)
    tick(wake("b6647b", f"2026-10-01T{due}", person="U2", asked="mei"))
    a_pass(p)
    tried = [("trajectory:b6647b" in prompt, "is it paid?" in prompt) for prompt, _ in runner.calls[1:]]
    assert tried == ([(False, True)] if due == "08:00" else [(True, False)])


def test_a_reminder_due_in_a_hold_a_names_session_began_is_its_next_try(tmp_path, monkeypatch, fake_time):
    runner = Limited()
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    tick = ticking(p, fake_time)
    store.create_task(None, "", "names", kind="names")
    names = store.get_task_by_thread("", "names")
    asyncio.run(p.memory_turn(names, vault.renamed_text("fan", "Fan Zhu"), fake_time.at.astimezone(p.cfg.zone),
                              channel=None, reply_thread=None, owed=False))
    assert store.get_meta("held_since") and len(runner.calls) == 1
    bins = wake("b6647b", "2026-10-01T08:00")
    tick(bins)
    assert len(runner.calls) == 1
    fake_time.at += timedelta(minutes=1)
    tick(bins)
    assert len(runner.calls) == 2 and "trajectory:b6647b" in runner.calls[1][0]


def test_a_held_and_a_capped_message_cut_short_at_two_starts_get_her_note_once(tmp_path, monkeypatch):
    """Each start's first pass takes both up as one turn, which the stop cuts
    short; at the third start, her note in their place, and nothing runs."""
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(), monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    held, capped = keep(store, dm(f"{AT:.1f}", "is it paid?")), keep(store, dm(f"{AT + 10:.1f}", "and the plumber?"))
    store.settle(Settled(capped=(capped,), held=((held, None),)))

    def cut_short_at_a_pass(runner):
        q = Processor(p.cfg, store, asyncio.Queue(), slack, runner)

        async def go():
            await q.drain_mail()
            await until(lambda: runner.running, "a session ran")
            await q.shutdown(grace_s=1)
        asyncio.run(go())
    cut_short_at_a_pass(Held())
    cut_short_at_a_pass(Held())
    assert [r[1:3] for r in kept(store)] == [("held", 2), ("capped", 2)]
    third = RecordingRunner(answer("never"))
    q = started_again(p, third)
    a_pass(q)
    assert third.calls == [] and slack.replies == [main.CUT_SHORT] and kept(store) == []


def test_doctor_counts_the_days_runs_from_the_households_midnight_and_what_is_held(tmp_path, capsys):
    from wanda.main import run_doctor

    c = Config(_env_file=None, data_dir=tmp_path, claude_bin="/bin/true", email_triage=False,
               slack_owner_user_ids="U1,U2", tz="America/Los_Angeles")
    store = Store(c.db_path)
    now = datetime.now(timezone.utc)
    for at, status in ((now, "ok"), (now, "refused"), (now, "refused"), (now - timedelta(hours=26), "ok")):
        store.record_run(kind="agent", task_id=None, session_id=None, started_at=at.isoformat(timespec="seconds"),
                         exit_code=0, cost_usd=0.1, status=status)
    capped, held = (keep(store, dm(f"{now.timestamp() - i:.1f}", "x")) for i in (60, 30))
    store.settle(Settled(capped=(capped,), held=((held, None),)))
    since = now - timedelta(minutes=10)
    store.set_meta("held_since", since.isoformat(timespec="seconds"))
    asyncio.run(run_doctor(c, smoke=False))
    stamp = vault.stamp(since.timestamp(), datetime.now(c.zone))
    assert (f"✓ claude runs today — 1 since 00:00 America/Los_Angeles of 200, 2 refused and not counted; "
            f"1 message(s) held until midnight; 1 held while Claude Code cannot run, since {stamp}\n"
            ) in capsys.readouterr().out


def test_compose_passes_the_run_cap_and_empty_means_200(monkeypatch):
    compose = (Path(__file__).resolve().parent.parent / "compose.wanda.yaml").read_text()
    assert "\n      WANDA_DAILY_RUN_CAP:\n" in compose
    monkeypatch.setenv("WANDA_DAILY_RUN_CAP", "")
    assert Config(_env_file=None).daily_run_cap == 200
    monkeypatch.setenv("WANDA_DAILY_RUN_CAP", "50")
    assert Config(_env_file=None).daily_run_cap == 50


def opening_blocks(tmp_path):
    """How many text blocks each session's opening message holds."""
    out = []
    for f in sorted(vault.transcripts_dir(tmp_path / "vault").glob("*.jsonl")):
        entries = [json.loads(x) for x in f.read_text().splitlines()]
        out.append(len(next(e["message"]["content"] for e in entries if e.get("type") == "user")))
    return out


def opening_text(tmp_path, i):
    """The opening message of the `i`th session to start, as it was handed it."""
    files = sorted(vault.transcripts_dir(tmp_path / "vault").glob("*.jsonl"), key=lambda f: f.stat().st_ctime_ns)
    entries = [json.loads(x) for x in files[i].read_text().splitlines()]
    return next(e["message"]["content"][0]["text"] for e in entries if e.get("type") == "user")


def test_a_message_waiting_when_its_session_starts_is_answered_once(tmp_path, monkeypatch):
    """At one session at a time fan's second line arrives while his turn waits
    behind mei's session: his turn's frame, built once it holds the slot,
    takes it, and his session is not handed it again."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, startup_s=1.0, steps=[1.0, 0.2])
    conversation(p, (0, dm(f"{AT:.1f}", "what time is dinner?", channel="D2", user="U2")),
                 (0.3, dm(f"{AT + 10:.1f}", "can you remind me at 5")),
                 (0.3, dm(f"{AT + 20:.1f}", "to call the plumber")))
    assert slack.replies == ["one answer to 1: what time is dinner?", "one answer to 1: to call the plumber"]
    assert len(store._query("SELECT * FROM runs")) == 2
    assert opening_blocks(tmp_path) == [1, 1]
    assert [h for h in handed_texts(tmp_path) if h] == []
    # the earlier line is in his frame, among the conversation so far
    firsts = [next(e["message"]["content"][0]["text"] for e in map(json.loads, f.read_text().splitlines())
                   if e.get("type") == "user")
              for f in vault.transcripts_dir(tmp_path / "vault").glob("*.jsonl")]
    assert sum("can you remind me at 5" in t for t in firsts) == 1


def test_a_burst_during_its_sessions_startup_is_one_session(tmp_path, monkeypatch):
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, startup_s=1.0, steps=[1.0, 0.2])
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me")), (0.1, dm(f"{AT + 1:.1f}", "to call the plumber")),
                 (0.1, dm(f"{AT + 2:.1f}", "at 5")))
    assert slack.replies == ["one answer to 3: can you remind me | to call the plumber | at 5"]
    assert len(store._query("SELECT * FROM runs")) == 1
    assert opening_blocks(tmp_path) == [1]


def test_two_messages_after_the_last_step_are_one_further_turn(tmp_path, monkeypatch):
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5)
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber")),
                 (0.05, dm(f"{AT + 31:.1f}", "and the electrician")))
    assert slack.replies == ["one answer to 3: can you remind me at 5 | to call the plumber | and the electrician"]
    assert handed_texts(tmp_path) == [[vault.added_text("dm", "fan", "to call the plumber", "16:40"),
                                       vault.added_text("dm", "fan", "and the electrician", "16:40")]]


def test_a_follow_up_that_needs_no_answer_leaves_the_first_answer(tmp_path, monkeypatch):
    """A further turn that says nothing does not take back the answer before it."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, silent_later=True)
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5 to call the plumber?")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "thanks!")))
    assert slack.replies == ["one answer to 1: can you remind me at 5 to call the plumber?"]
    assert store._query("SELECT status FROM runs")[0]["status"] == "ok"


def test_a_further_turn_that_answers_only_its_own_line_is_what_is_posted(tmp_path, monkeypatch):
    """Only the last answer that says something is posted, so a further turn
    begun by "thanks!" that answers the thanks alone leaves the question
    unanswered. The added message's frame says which answer is sent and that
    it has to answer everything; nothing in the harness posts the question's
    answer instead."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, answer_new=True)
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5 to call the plumber?")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "thanks!")))
    assert slack.replies == ["one answer to 1: thanks!"]
    assert handed_texts(tmp_path) == [[vault.added_text("dm", "fan", "thanks!", "16:40")]]
    assert "so that answer has to answer everything in this session that was said to me" in handed_texts(tmp_path)[0][0]
    assert store._query("SELECT status FROM runs")[0]["status"] == "ok"


def test_a_failed_last_turn_after_an_answer_posts_the_answer_then_answers_the_rest(tmp_path, monkeypatch):
    """The message that began the failed turn is run again as the
    conversation's next turn, by a session told of the one that failed."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, fail="later")
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber")))
    assert slack.replies == ["one answer to 1: can you remind me at 5", "one answer to 1: to call the plumber"]
    assert store.pending_deliveries() == []
    (first, failed), (_, again) = [(r["session_id"], r["error"]) for r in store._query("SELECT * FROM runs")]
    assert failed == "error_during_execution" and again is None
    assert vault.RETRIED.format(sid8=first[:8]) in opening_text(tmp_path, -1)


class RefusingAt(ConversationSlack):
    """A Slack that refuses the posts at these places in the order they are
    made, as a rate limit or a blip would, and takes the rest."""

    def __init__(self, refused, **kw):
        super().__init__(**kw)
        self.refused, self.made = set(refused), 0

    async def reply(self, thread_ts, text, channel=None):
        self.made += 1
        if self.made - 1 in self.refused:
            raise RuntimeError("ratelimited")
        return await super().reply(thread_ts, text, channel=channel)


@pytest.mark.parametrize("refused", ["the answer", "the answer twice", "the rest's answer"])
def test_the_rest_is_answered_after_the_answer_when_slack_refuses_a_post(tmp_path, monkeypatch, refused):
    """The follow-up a later turn failed on is answered by the next turn:
    when Slack refuses the first answer's post, that turn posts it before its
    frame; refused there too, or when the second's is refused, each is kept,
    and delivery posts the second after the first, never before it."""
    slack = RefusingAt({"the answer": {0}, "the answer twice": {0, 1}, "the rest's answer": {1}}[refused],
                       history=[])
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, slack=slack, steps=[0.2], reply_s=1.5,
                                           fail="later")
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber")))
    # the first session handed it, the second framed it
    assert sorted(handed_texts(tmp_path), key=len) == [
        [], [vault.added_text("dm", "fan", "to call the plumber", "16:40")]]
    answered, rest = "one answer to 1: can you remind me at 5", "one answer to 1: to call the plumber"
    assert slack.replies == {"the answer": [answered, rest], "the answer twice": [],
                             "the rest's answer": [answered]}[refused]
    assert len(store.pending_deliveries()) == {"the answer": 0, "the answer twice": 2, "the rest's answer": 1}[refused]
    asyncio.run(p.deliver_pending())
    assert slack.replies == [answered, rest]
    assert [dict(r) for r in store._query("SELECT status, error, result_text, notified FROM runs")] == [
        {"status": "ok", "error": "error_during_execution", "result_text": answered, "notified": 1},
        {"status": "ok", "error": None, "result_text": rest, "notified": 1}]


def test_a_turn_begun_by_a_background_notice_that_says_nothing_leaves_the_answer(tmp_path, monkeypatch):
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], notify="after")
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")))
    assert slack.replies == ["one answer to 1: can you remind me at 5"] and handed_texts(tmp_path) == [[]]


@pytest.mark.parametrize("first", ["failed", "ended without its report"])
@pytest.mark.parametrize("later", ["says nothing", "answers the follow-up alone"])
def test_a_failed_first_turn_and_a_further_one(tmp_path, monkeypatch, first, later):
    """A first turn that failed or ended without its report, then a further
    turn for a follow-up written after its last step: with nothing said, the
    failure is told, never the model's text; an answer the further turn
    gives is posted instead."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5,
                                           **({"fail": "first"} if first == "failed" else {"no_report": "first"}),
                                           **({"silent_later": True} if later == "says nothing" else
                                              {"answer_new": True}))
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5 to call the plumber?")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "thanks!" if later == "says nothing" else
                                              "and the electrician")))
    runs = [dict(r) for r in store._query("SELECT kind, status, error FROM runs")]
    if later == "says nothing":
        error = "error_during_execution" if first == "failed" else "the session ended without its report"
        assert slack.replies == [main.FAILED]
        assert runs == [{"kind": "agent", "status": "error", "error": error},
                        {"kind": "note", "status": "ok", "error": None}]
    else:
        assert slack.replies == ["one answer to 1: and the electrician"]
        assert runs == [{"kind": "agent", "status": "ok", "error": None}]
    assert store.pending_deliveries() == []


def test_an_exit_after_the_answer_posts_the_answer_then_answers_the_rest(tmp_path, monkeypatch):
    """Claude Code ended from outside, or by a crash, in a further turn after
    its answer: the answer is posted, and the message that began the turn
    with no result is run again as the next turn; the run names the exit and
    never carries the report as its error."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, crash="later")
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber")))
    assert slack.replies == ["one answer to 1: can you remind me at 5", "one answer to 1: to call the plumber"]
    assert store.pending_deliveries() == []
    assert [dict(r) for r in store._query("SELECT status, error FROM runs")] == [
        {"status": "ok", "error": "claude exited 1 after its last result"}, {"status": "ok", "error": None}]


def test_a_session_that_ended_without_its_report_is_tried_once_more(tmp_path, monkeypatch):
    """By a session framed as its retry, with the turn's message and the one
    the first was handed; its answer alone is posted."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, sessions=[
        {"steps": [1.0, 0.2], "no_report": "first"}, {"steps": [0.2]}])
    conversation(p, (0, dm(f"{AT:.1f}", "one")), (("tool_use", 1, 0.1), dm(f"{AT + 10:.1f}", "two")))
    assert slack.replies == ["one answer to 1: two"] and store.pending_deliveries() == []
    (first, failed), (_, ok) = [(r["session_id"], r["status"]) for r in store._query("SELECT * FROM runs")]
    assert (failed, ok) == ("error", "ok")
    retry = opening_text(tmp_path, -1)
    assert f"In a direct message that fan and I read. {vault.RETRIED.format(sid8=first[:8])}\n\n" in retry
    assert "    16:40 fan: one\n\nfan now says:\n\n    two" in retry


def test_a_session_claude_code_refused_holds_its_message(tmp_path, monkeypatch):
    """Known by the error its streamed output gives."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, refuse={
        "turn": "first", "error": "rate_limit", "said": "You've hit your limit · resets 5pm"})
    keep(store, dm(f"{AT:.1f}", "hello"))
    conversation(p, (0, dm(f"{AT:.1f}", "hello")))
    assert slack.replies == [] and [r[1] for r in kept(store)] == ["held"]
    assert failed_runs(store) == [("agent", "refused", "You've hit your limit · resets 5pm")]
    assert store.runs_today(p.cfg.zone)[0] == 0


@pytest.mark.parametrize("before", [None, "2026-10-01T23:00:00+00:00"], ids=["no hold", "a hold"])
def test_a_clock_session_claude_code_refused_is_known_by_its_words(tmp_path, monkeypatch, before):
    """Run with --output-format json, it prints no assistant event. It
    begins a hold, or ends none."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, refuse={
        "turn": "first", "error": None, "said": "Login expired · Please run /login"})
    if before:
        store.set_meta("held_since", before)
    store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")
    got = asyncio.run(p.memory_turn(task, "It is 17:00, as asked:\n\n    call the plumber",
                                    datetime.fromtimestamp(AT, p.cfg.zone), channel="D1", reply_thread=None,
                                    owed=False))
    assert got == "Login expired · Please run /login" and slack.replies == []
    assert failed_runs(store) == [("agent", "refused", "Login expired · Please run /login")]
    assert store.runs_today(p.cfg.zone)[0] == 0
    held = store.get_meta("held_since")
    assert held == before if before else held


@pytest.mark.parametrize("channel_type,rest", [("im", main.FAILED_REST), ("mpim", main.FAILED_REST_GROUP)],
                         ids=["a DM", "a group DM"])
def test_a_later_turn_that_fails_again_run_as_the_next_gets_her_note_after_the_answer(tmp_path, monkeypatch,
                                                                                    channel_type, rest):
    """That next turn is the failed turn's one retry, not tried once more."""
    slack = ConversationSlack(members=["U1", "UBOT"], history=[])
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, slack=slack, sessions=[
        {"steps": [0.2], "reply_s": 1.5, "fail": "later"}, {"fail": "first"}])
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5", channel_type=channel_type)),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber", channel_type=channel_type)))
    assert slack.replies == ["one answer to 1: can you remind me at 5", rest]
    assert [r[:2] for r in failed_runs(store)] == [("agent", "ok"), ("agent", "error"), ("note", "ok")]
    asyncio.run(p._flush_failed())
    [alert] = slack.alerts
    assert re.fullmatch(r"2 message session\(s\) failed \(other\): run 1 at \d\d:\d\d, Claude Code said: "
                        r"error_during_execution, run again as the next turn; run 2 at \d\d:\d\d, Claude Code said: "
                        r"error_during_execution, not tried again, a note asked for it again", alert)


class BrokenOnceItAnswers(ConversationSlack):
    """Will not say who anyone is once her first answer is posted."""

    async def users(self, ids):
        if self.replies:
            raise RuntimeError("boom")
        return await super().users(ids)


def test_a_later_turn_run_again_whose_frame_fails_gets_her_note_after_the_answer(tmp_path, monkeypatch):
    """The turn that runs it again is that failure's one retry, whatever
    fails it."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, slack=BrokenOnceItAnswers(history=[]),
                                           steps=[0.2], reply_s=1.5, fail="later")
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber")))
    assert slack.replies == ["one answer to 1: can you remind me at 5", main.FAILED_REST]
    assert [r[:2] for r in failed_runs(store)] == [("agent", "ok"), ("agent", "error"), ("note", "ok")]


def test_a_line_sent_after_the_answer_joins_the_turn_that_runs_the_rest_again(tmp_path, monkeypatch):
    """One that came once the session had answered, and so was never handed
    to it, is framed beside the message a later turn failed on: that turn is
    still the failure's one retry, not tried once more."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, sessions=[
        {"steps": [0.2], "reply_s": 1.5, "fail": "later"}, {"fail": "first"}])
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber")),
                 (("structured_output", 1, 0.3), dm(f"{AT + 40:.1f}", "and the electrician")))
    assert slack.replies == ["one answer to 1: can you remind me at 5", main.FAILED_REST]
    assert [r[:2] for r in failed_runs(store)] == [("agent", "ok"), ("agent", "error"), ("note", "ok")]
    assert "    16:40 fan: to call the plumber\n\nfan now says:\n\n    and the electrician" in opening_text(
        tmp_path, -1)


def test_a_later_turn_claude_code_refused_holds_only_its_message_after_the_answer(tmp_path, monkeypatch):
    """A second session would meet the same refusal: nothing is run again,
    and the message that began the turn it refused is held, the one before
    answered."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, refuse={
        "turn": "later", "error": "rate_limit", "said": "You've hit your limit · resets 5pm"})
    asked, follow = dm(f"{AT:.1f}", "can you remind me at 5"), dm(f"{AT + 30:.1f}", "to call the plumber")
    keep(store, asked), keep(store, follow)
    conversation(p, (0, asked), (("tool_result", 1, 0.2), follow))
    assert slack.replies == ["one answer to 1: can you remind me at 5"]
    assert failed_runs(store) == [("agent", "ok", "You've hit your limit · resets 5pm")]
    assert [r[:2] for r in kept(store)] == [(f"{AT + 30:.1f}", "held")]


def test_a_hold_after_a_first_turn_that_ran_names_its_session_for_the_take_up(tmp_path, monkeypatch):
    """fan's first turn failed after a step, and Claude Code refused the turn
    his added line began, the last: both are held, each naming this
    session, which may have written to memory before it was refused, so
    that the session that takes them up is told of it."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, sessions=[
        {"steps": [0.2], "reply_s": 1.5, "fail": "first",
         "refuse": {"turn": "later", "error": "rate_limit", "said": "You've hit your limit · resets 5pm"}},
        {"steps": [0.2]}])
    asked, follow = dm(f"{AT:.1f}", "can you remind me at 5"), dm(f"{AT + 30:.1f}", "to call the plumber")
    keep(store, asked), keep(store, follow)
    conversation(p, (0, asked), (("tool_result", 1, 0.2), follow))
    assert slack.replies == [] and failed_runs(store) == [("agent", "refused", "You've hit your limit · resets 5pm")]
    [sid] = [r["session_id"] for r in store._query("SELECT session_id FROM runs")]
    assert [(r[1], r[3]) for r in kept(store)] == [("held", sid), ("held", sid)]
    store.end_hold()
    a_pass(p)
    assert slack.replies == ["one answer to 1: to call the plumber"] and kept(store) == []
    taken_up = opening_text(tmp_path, -1)
    assert vault.RETRIED.format(sid8=sid[:8]) in taken_up and "can you remind me at 5" in taken_up


def test_a_failed_turn_a_background_commands_notice_began_leaves_the_answer_alone(tmp_path, monkeypatch):
    """Nothing said beyond the answer: the failure goes to the log and the
    alert."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], notify="after", fail="later")
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")))
    assert slack.replies == ["one answer to 1: can you remind me at 5"]
    assert failed_runs(store) == [("agent", "ok", "error_during_execution")]
    asyncio.run(p._flush_failed())
    assert slack.alerts[0].endswith(", Claude Code said: error_during_execution, not tried again, no note")

def test_a_clock_sessions_answer_stands_when_a_turn_after_it_says_nothing(tmp_path, monkeypatch):
    """A session the clock starts has its input closed after the prompt, and
    Claude Code prints its last turn's result alone: a turn a background
    command's end began after the reminder, saying nothing, leaves the
    reminder to be posted."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], notify="after")
    store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")
    got = asyncio.run(p.memory_turn(task, "It is 17:00, as asked:\n\n    call the plumber",
                                    datetime.fromtimestamp(AT, p.cfg.zone), channel="D1", reply_thread=None,
                                    owed=False))
    assert got is None and slack.replies == ["one answer to 1: call the plumber"]
    assert [dict(r) for r in store._query("SELECT status, notified FROM runs")] == [{"status": "ok", "notified": 1}]


@pytest.mark.parametrize("later", ["fails", "runs out of time"])
def test_a_clock_session_that_fails_after_its_reminder_posts_it(tmp_path, monkeypatch, later):
    """A session the clock starts owes nobody, but a reminder it already gave
    is posted when a turn after it fails or runs out of time; the run reads
    as spoken, and the failure is what it returns, for the clock to alert."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], notify="after",
                                           **({"fail": "later"} if later == "fails" else {"hang": "later"}))
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 4)
    store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")
    got = asyncio.run(p.memory_turn(task, "It is 17:00, as asked:\n\n    call the plumber",
                                    datetime.fromtimestamp(AT, p.cfg.zone), channel="D1", reply_thread=None,
                                    owed=False))
    error = "error_during_execution" if later == "fails" else "timed out after 4s"
    assert got == error and slack.replies == ["one answer to 1: call the plumber"]
    assert [dict(r) for r in store._query("SELECT status, error, notified FROM runs")] == [
        {"status": "ok", "error": error, "notified": 1}]
    assert store.pending_deliveries() == []


def test_a_message_deleted_after_its_session_took_it_is_not_put_back(tmp_path, monkeypatch):
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, hang=True)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 4)
    deleted = Event(source="slack", dedupe_key="D1:del", payload={"kind": "deleted", "channel": "D1",
                                                                   "ts": f"{AT + 30:.1f}"})
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (1.5, dm(f"{AT + 30:.1f}", "that was meant for mei")), (0.5, deleted))
    assert slack.replies == [main.FAILED]
    assert [r["kind"] for r in store._query("SELECT kind FROM runs")] == ["agent", "note"]


@pytest.mark.parametrize("ending", ["the session answered", "its frame given up", "the session still working"])
def test_a_message_deleted_while_it_is_framed_is_not_answered(tmp_path, monkeypatch, ending):
    """Deleted while the session framed it: whether the frame is then given
    up or built while the session still works, it is neither handed to the
    session nor put back for a turn of its own."""
    real = Processor._added_text

    async def slow_frame(self, p, more):
        await asyncio.sleep(2.0)
        return None if ending == "its frame given up" else await real(self, p, more)
    monkeypatch.setattr(Processor, "_added_text", slow_frame)
    p, store, _, slack = standin_processor(tmp_path, monkeypatch,
                                           steps=[1.0, 0.2] if ending == "the session answered" else [4.0, 0.2])
    deleted = Event(source="slack", dedupe_key="D1:del", payload={"kind": "deleted", "channel": "D1",
                                                                   "ts": f"{AT + 30:.1f}"})
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_use", 1, 0.1), dm(f"{AT + 30:.1f}", "that was meant for mei")), (0.3, deleted))
    assert slack.replies == ["one answer to 1: can you remind me at 5"]
    assert len(store._query("SELECT * FROM runs")) == 1 and handed_texts(tmp_path) == [[]]


# --- what is said for a message deleted before it is posted ---

def deletion(ts, channel="D1"):
    return Event(source="slack", dedupe_key=f"{channel}:del:{ts}", payload={"kind": "deleted", "channel": channel,
                                                                             "ts": ts})


def session_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("memory session ")]


def test_an_answer_to_a_message_deleted_while_its_session_works_is_not_posted(tmp_path, monkeypatch, caplog):
    """Recorded as an answer posted nowhere is, and owed to no one."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[1.0])
    line = dm(f"{AT:.1f}", "the dentist moved to the 14th")
    keep(store, line)
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, line), (("tool_use", 1, 0.1), deletion(f"{AT:.1f}")))
    assert slack.replies == [] and store.pending_deliveries() == [] and kept(store) == []
    answered = "one answer to 1: the dentist moved to the 14th"
    assert [dict(r) for r in store._query("SELECT kind, status, result_text, notified FROM runs")] == [
        {"kind": "agent", "status": "ok", "result_text": answered, "notified": 1}]
    [said] = session_lines(caplog)
    assert said.endswith(f", {len(answered)} characters, not posted: its messages were deleted")


def test_one_of_two_deleted_while_their_session_works_leaves_the_answer_posted(tmp_path, monkeypatch, caplog):
    """The answer is for the rest; the session line says how many were taken
    back."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[1.0, 0.2])
    first, added = dm(f"{AT:.1f}", "the dentist moved to the 14th"), dm(f"{AT + 30:.1f}", "and Joan's swim is off")
    keep(store, first), keep(store, added)
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, first), (("tool_use", 1, 0.1), added), (0.3, deletion(f"{AT:.1f}")))
    answered = "one answer to 2: the dentist moved to the 14th | and Joan's swim is off"
    assert slack.replies == [answered] and store.pending_deliveries() == [] and kept(store) == []
    [said] = session_lines(caplog)
    assert said.endswith(f", {len(answered)} characters to post, 1 of its 2 messages deleted")


def test_a_failed_session_whose_one_message_was_deleted_gets_no_note(tmp_path, monkeypatch, caplog):
    """Nor a second session: nothing is left to answer."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[1.0], fail="first")
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, line), (("tool_use", 1, 0.1), deletion(f"{AT:.1f}")))
    assert slack.replies == [] and store.pending_deliveries() == [] and kept(store) == []
    assert failed_runs(store) == [("agent", "error", "error_during_execution")]
    assert len(handed_texts(tmp_path)) == 1
    [said] = session_lines(caplog)
    assert said.endswith(", failed: error_during_execution, not posted: its messages were deleted")
    asyncio.run(p._flush_failed())
    assert slack.alerts[-1].endswith(", Claude Code said: error_during_execution, not tried again, no note")


def test_a_refused_session_whose_one_message_was_deleted_holds_nothing(tmp_path, monkeypatch, caplog):
    """With a hold confirmed, a message newly held there would be told so
    (HELD): one deleted while its session was refused is not held, and
    nothing is said there. The refusal still holds sessions."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, startup_s=1.0, refuse={
        "turn": "first", "error": "rate_limit", "said": "You've hit your limit · resets 5pm"})
    since = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    store.set_meta("held_since", since)
    store.set_meta("held_confirmed", since)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, line), (0.5, deletion(f"{AT:.1f}")))
    assert slack.replies == [] and kept(store) == [] and store.get_meta("held_since") == since
    assert failed_runs(store) == [("agent", "refused", "You've hit your limit · resets 5pm")]
    [said] = session_lines(caplog)
    assert not said.endswith("; held")
    assert said.endswith(", failed: You've hit your limit · resets 5pm, not posted: its messages were deleted")
    asyncio.run(p._flush_failed())
    assert slack.alerts[-1].endswith(", not tried again, no note")


def test_a_later_turn_that_failed_on_a_message_deleted_since_runs_nothing_again(tmp_path, monkeypatch, caplog):
    """Her answer to the first is posted; the added message its later turn
    failed on was taken back while that turn ran, so it is neither run again
    nor asked for again (FAILED_REST)."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, steps_later=[1.0],
                                           fail="later")
    asked, added = dm(f"{AT:.1f}", "can you remind me at 5"), dm(f"{AT + 30:.1f}", "to call the plumber")
    keep(store, asked), keep(store, added)
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, asked), (("tool_result", 1, 0.2), added),
                     (("tool_use", 2, 0.2), deletion(f"{AT + 30:.1f}")))
    answered = "one answer to 1: can you remind me at 5"
    assert slack.replies == [answered] and store.pending_deliveries() == [] and kept(store) == []
    assert failed_runs(store) == [("agent", "ok", "error_during_execution")]
    assert len(handed_texts(tmp_path)) == 1
    [said] = session_lines(caplog)
    assert said.endswith(f", {len(answered)} characters to post, then failed: error_during_execution, "
                         "1 of its 2 messages deleted")
    asyncio.run(p._flush_failed())
    assert slack.alerts[-1].endswith(", Claude Code said: error_during_execution, not tried again, no note")


@pytest.mark.parametrize("later", [
    {"refuse": {"turn": "later", "error": "rate_limit", "said": "You've hit your limit · resets 5pm"}},
    {"fail": "later", "fail_subtype": "error_max_budget_usd"},
], ids=["refused", "out of budget"])
def test_with_the_transcript_unreadable_a_later_turn_on_a_message_deleted_since_asks_nothing(tmp_path, monkeypatch,
                                                                                              later):
    """Which turn took what cannot be read, and every message the session
    was handed was taken back: no note asks for it again, though a second
    session would meet the same refusal or budget."""
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: None)
    monkeypatch.setattr("wanda.vault.handed", lambda v, sid: None)
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, **later)
    asked, added = dm(f"{AT:.1f}", "can you remind me at 5"), dm(f"{AT + 30:.1f}", "to call the plumber")
    keep(store, asked), keep(store, added)
    conversation(p, (0, asked), (("tool_result", 1, 0.2), added), (("tool_result", 1, 0.6), deletion(f"{AT + 30:.1f}")))
    assert slack.replies == ["one answer to 1: can you remind me at 5"]
    assert store.pending_deliveries() == [] and kept(store) == [] and len(handed_texts(tmp_path)) == 1
    asyncio.run(p._flush_failed())
    assert slack.alerts[-1].endswith(", not tried again, no note")


@pytest.mark.parametrize("later", [
    {"fail": "later", "fail_subtype": "error_max_budget_usd"},
    {"refuse": {"turn": "later", "error": "rate_limit", "said": "You've hit your limit · resets 5pm"}},
], ids=["out of budget", "refused"])
def test_with_the_transcript_unreadable_a_later_failed_turn_is_run_again_not_asked_for(tmp_path, monkeypatch,
                                                                                       later):
    """What the session was handed is back on the waiting list already, and
    the conversation's next turn answers it: no note asks for it again
    first, whether the later turn's budget ran out or Claude Code refused
    it."""
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: None)
    monkeypatch.setattr("wanda.vault.handed", lambda v, sid: None)
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, **later)
    asked, added = dm(f"{AT:.1f}", "can you remind me at 5"), dm(f"{AT + 30:.1f}", "to call the plumber")
    keep(store, asked), keep(store, added)
    conversation(p, (0, asked), (("tool_result", 1, 0.2), added))
    assert slack.replies == ["one answer to 1: can you remind me at 5", "one answer to 1: to call the plumber"]
    assert store.pending_deliveries() == [] and kept(store) == []


def test_a_frame_that_fails_after_its_one_message_was_deleted_gets_no_note(tmp_path, monkeypatch):
    """The turn's failure outside its session is the alert's alone."""
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), RecordingRunner(), monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)

    async def gathered(self, *args, **kw):
        await self.handle_slack(deletion(f"{AT:.1f}"))
        raise RuntimeError("boom")
    monkeypatch.setattr(Processor, "_memory_arrival", gathered)
    settle(p, p.handle_slack(line))
    assert p.runner.calls == [] and p.slack.replies == [] and kept(store) == []
    assert failed_runs(store) == [("agent", "error", "could not gather what was said here: boom")]
    assert store.pending_deliveries() == []
    asyncio.run(p._flush_failed())
    assert p.slack.alerts[-1].endswith(", could not gather what was said here: boom, not tried again, no note")


def test_a_retrys_frame_that_fails_after_its_one_message_was_deleted_gets_no_note(tmp_path, monkeypatch):
    """The turn is the next start's retry of a first try a stop left: the
    alert names that first try, tried once more, and nothing is posted."""
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), RecordingRunner(), monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    line = dm(f"{AT:.1f}", "is it paid?")
    key = keep(store, line)
    tried = ["s0", 7, utcnow(), "error_during_execution", True, "other"]
    store.settle(Settled(first_try=((key, line.payload | {"first_try": tried}),)))

    async def gathered(self, *args, **kw):
        await self.handle_slack(deletion(f"{AT:.1f}"))
        raise RuntimeError("boom")
    monkeypatch.setattr(Processor, "_memory_arrival", gathered)
    q = started_again(p, RecordingRunner())
    assert q.runner.calls == [] and q.slack.replies == [] and kept(store) == []
    assert store.pending_deliveries() == []
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert (failed["id"], failed["then"]) == (7, "tried once more, no note")


def test_a_first_try_a_stop_left_whose_message_is_deleted_before_it_is_framed_is_alerted(tmp_path, monkeypatch):
    """The next start's take-up finds nothing left to run: the first try's
    failure is alerted as one nothing followed, and nothing is posted."""
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), RecordingRunner(), monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    line = dm(f"{AT:.1f}", "is it paid?")
    key = keep(store, line)
    tried = ["s0", 7, utcnow(), "error_during_execution", True, "other"]
    store.settle(Settled(first_try=((key, line.payload | {"first_try": tried}),)))
    late = Processor._late_answered

    async def deleted_first(self, keys):
        await self.handle_slack(deletion(f"{AT:.1f}"))
        return await late(self, keys)
    monkeypatch.setattr(Processor, "_late_answered", deleted_first)
    q = started_again(p, RecordingRunner())
    assert q.runner.calls == [] and q.slack.replies == [] and kept(store) == []
    assert store.pending_deliveries() == []
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert (failed["id"], failed["then"]) == (7, "not tried again, no note")


def test_a_message_deleted_before_its_retry_is_framed_leaves_the_first_trys_failure_alerted(tmp_path, monkeypatch):
    """Nothing is left for the retry to run, and nothing is posted; the first
    try's failure is alerted all the same."""
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), RecordingRunner(ok=False), monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)
    budget, checked = p.check_budget, []

    async def checking(**kw):
        # the third check is the retry's, after the first try was recorded
        checked.append(kw)
        if len(checked) == 3:
            await p.handle_slack(deletion(f"{AT:.1f}"))
        return await budget(**kw)
    monkeypatch.setattr(p, "check_budget", checking)
    settle(p, p.handle_slack(line))
    assert len(p.runner.calls) == 1 and p.slack.replies == [] and kept(store) == []
    assert store.pending_deliveries() == []
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert (failed["said"], failed["then"]) == ("claude reported an error", "not tried again, no note")


def test_a_retrys_frame_that_fails_names_the_first_try_once(tmp_path, monkeypatch):
    """The first try fails, and the retry's frame cannot be built: her note
    is posted, and the first try is named once in the alert, by the note."""
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), RecordingRunner(ok=False), monkeypatch)
    line = dm(f"{AT:.1f}", "is it paid?")
    keep(store, line)
    arrival, calls = Processor._memory_arrival, []

    async def second_fails(self, *args, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return await arrival(self, *args, **kw)
    monkeypatch.setattr(Processor, "_memory_arrival", second_fails)
    settle(p, p.handle_slack(line))
    assert len(p.runner.calls) == 1 and p.slack.replies == [main.FAILED]
    failed = json.loads(store.get_meta("failed_runs"))
    assert [f["then"] for f in failed] == ["tried once more, a note asked for it again"]


def test_an_answer_slack_refused_whose_message_is_then_deleted_is_not_posted(tmp_path, monkeypatch, caplog):
    """At the next pass the run is owed no longer, and nothing is posted."""
    import logging

    slack = Refusing(history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("On Wednesday the 14th, then.")),
                                   monkeypatch)
    line = dm(f"{AT:.1f}", "the dentist moved to the 14th")
    key = keep(store, line)
    settle(p, p.handle_slack(line))
    assert slack.refused == ["On Wednesday the 14th, then."] and [r[1] for r in kept(store)] == ["answered"]
    settle(p, p.handle_slack(deletion(key[1])))
    assert [r["deleted"] for r in store.kept()] == [1]
    p.slack = ConversationSlack(history=[])
    with caplog.at_level(logging.INFO, logger="wanda"):
        a_pass(p)
    assert p.slack.replies == [] and store.pending_deliveries() == [] and kept(store) == []
    [run] = store._query("SELECT id, session_id, notified FROM runs")
    assert run["notified"] == 1
    assert f"run {run['id']} of session {run['session_id']} not posted: its messages were deleted" in caplog.text


def test_a_message_deleted_as_its_answer_is_posted_reads_not_posted(tmp_path, monkeypatch, caplog):
    """The session ended with its message standing, which is deleted before
    the answer reaches Slack: the answer is not posted, and the session line
    says so."""
    import logging

    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), RecordingRunner(answer("At 5, then.")),
                                   monkeypatch)
    line = dm(f"{AT:.1f}", "can you remind me at 5")
    keep(store, line)
    post = p._post

    async def deleted_first(run, *a, **kw):
        await p.handle_slack(deletion(f"{AT:.1f}"))
        return await post(run, *a, **kw)
    monkeypatch.setattr(p, "_post", deleted_first)
    with caplog.at_level(logging.INFO, logger="wanda"):
        settle(p, p.handle_slack(line))
    assert p.slack.replies == []
    [said] = session_lines(caplog)
    assert said.endswith(", 11 characters, not posted: its messages were deleted"), said


@pytest.mark.parametrize("deleted,posted", [("both", []), ("the first", [main.FAILED_REST]),
                                            ("the second", ["At 5, then."])])
def test_an_answer_and_her_note_after_it_are_each_posted_only_while_a_message_of_its_own_stands(
        tmp_path, monkeypatch, deleted, posted):
    """Her answer holds the first message and her note the second, which it
    asks for again: each is posted while its own stands, in their order."""
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), RecordingRunner(), monkeypatch)
    keys = (keep(store, dm(f"{AT:.1f}", "can you remind me at 5")),
            keep(store, dm(f"{AT + 30:.1f}", "to call the plumber")))
    tid = store.create_task(None, "D1", "conversation", kind="dm")
    store.record_run_and_note(
        main.FAILED_REST, settled=Settled(answered=keys[:1]), noted=Settled(answered=keys[1:]), kind="agent",
        task_id=tid, session_id="s1", started_at=utcnow(), exit_code=0, cost_usd=0.4, status="ok",
        error="error_during_execution", result_text="At 5, then.", notified=0)
    for key in {"both": keys, "the first": keys[:1], "the second": keys[1:]}[deleted]:
        settle(p, p.handle_slack(deletion(key[1])))
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == posted
    assert store.pending_deliveries() == [] and kept(store) == []


@pytest.mark.parametrize("deleted,posted", [("both", []), ("the second", ["At 5, then.", main.FAILED_REST])])
def test_an_answer_whose_messages_are_kept_on_her_note_is_checked_by_the_notes(tmp_path, monkeypatch, caplog,
                                                                               deleted, posted):
    """An answer and her note recorded with every message on the note, as
    when the note can name none of what it asks for again, still waiting:
    the answer has none of its own, and is posted while one of the note's
    stands."""
    import logging

    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), RecordingRunner(), monkeypatch)
    keys = (keep(store, dm(f"{AT:.1f}", "can you remind me at 5")),
            keep(store, dm(f"{AT + 30:.1f}", "to call the plumber")))
    tid = store.create_task(None, "D1", "conversation", kind="dm")
    run_id, note_id = store.record_run_and_note(
        main.FAILED_REST, noted=Settled(answered=keys), kind="agent", task_id=tid, session_id="s1",
        started_at=utcnow(), exit_code=0, cost_usd=0.4, status="ok", error="error_during_execution",
        result_text="At 5, then.", notified=0)
    for key in keys if deleted == "both" else keys[1:]:
        settle(p, p.handle_slack(deletion(key[1])))
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(p.deliver_pending())
    assert p.slack.replies == posted
    assert store.pending_deliveries() == [] and kept(store) == []
    if deleted == "both":
        for run in (run_id, note_id):
            assert f"run {run} of session s1 not posted: its messages were deleted" in caplog.text


def test_her_note_for_a_turn_that_failed_outside_its_session_logs_no_session_when_not_posted(tmp_path,
                                                                                            monkeypatch, caplog):
    """A note recorded for a turn whose frame failed has no session: once its
    message is deleted, delivery's line names none."""
    import logging

    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), RecordingRunner(), monkeypatch)
    key = keep(store, dm(f"{AT:.1f}", "is it paid?"))
    tid = store.create_task(None, "D1", "conversation", kind="dm")
    _, note_id = store.record_run_and_note(
        main.FAILED, noted=Settled(answered=(key,)), kind="agent", task_id=tid, session_id=None,
        started_at=utcnow(), exit_code=None, cost_usd=0.0, status="error", error="could not gather what was said")
    settle(p, p.handle_slack(deletion(key[1])))
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(p.deliver_pending())
    assert p.slack.replies == [] and store.pending_deliveries() == []
    assert f"run {note_id} not posted: its messages were deleted" in caplog.text


@pytest.mark.parametrize("deleted,posted", [(0, [main.FAILED_REST]), (1, ["one answer to 1: can you remind me at 5"])],
                         ids=["the one answered", "the one asked for again"])
def test_an_answer_and_her_note_slack_refused_are_posted_for_what_still_stands(tmp_path, monkeypatch, deleted,
                                                                                posted):
    """Her answer to the first message and her note asking again for the one
    added after it, whose turn ran out of budget, both wait while Slack
    refuses them; once one of the two messages is deleted, the next pass
    posts only what answers the other."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, slack=Refusing(history=[]), steps=[0.2],
                                           reply_s=1.5, fail="later", fail_subtype="error_max_budget_usd")
    lines = (dm(f"{AT:.1f}", "can you remind me at 5"), dm(f"{AT + 30:.1f}", "to call the plumber"))
    for line in lines:
        keep(store, line)
    conversation(p, (0, lines[0]), (("tool_result", 1, 0.2), lines[1]))
    assert slack.refused == ["one answer to 1: can you remind me at 5"] and len(store.pending_deliveries()) == 2
    settle(p, p.handle_slack(deletion(lines[deleted].payload["ts"])))
    p.slack = ConversationSlack(history=[])
    a_pass(p)
    assert p.slack.replies == posted and store.pending_deliveries() == [] and kept(store) == []


@pytest.mark.parametrize("later,posted,line,held", [
    ({"fail": "later", "fail_subtype": "error_max_budget_usd"}, [main.FAILED_REST],
     "error_max_budget_usd; a note asks for it again", []),
    ({"fail": "later"}, ["one answer to 1: to call the plumber"],
     "error_during_execution; run again as the next turn", []),
    ({"refuse": {"turn": "later", "error": "rate_limit", "said": "You've hit your limit · resets 5pm"}}, [],
     "You've hit your limit · resets 5pm; held", ["held"]),
], ids=["out of budget", "another failure", "refused"])
def test_an_answer_whose_one_message_is_deleted_while_she_works_is_not_posted_beside_what_follows(
        tmp_path, monkeypatch, caplog, later, posted, line, held):
    """The first message is deleted while she works, before her answer to it,
    and the turn the added one began fails after that answer: it is kept as
    one whose every message was deleted, and only what follows for the added
    one goes on, her note asking for it again, its run again or its hold."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, steps_later=[1.0],
                                           **later)
    asked, added = dm(f"{AT:.1f}", "can you remind me at 5"), dm(f"{AT + 30:.1f}", "to call the plumber")
    keep(store, asked), keep(store, added)
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, asked), (("tool_result", 1, 0.2), added), (0.3, deletion(f"{AT:.1f}")))
    assert slack.replies == posted and store.pending_deliveries() == []
    assert [r[1] for r in kept(store)] == held
    answered = "one answer to 1: can you remind me at 5"
    [run] = store._query("SELECT notified FROM runs WHERE kind='agent' AND result_text=?", (answered,))
    assert run["notified"] == 1
    said = session_lines(caplog)[0]
    assert said.endswith(f", {len(answered)} characters, not posted: its messages were deleted, then failed: {line}")


def test_an_answer_beside_a_note_that_names_none_of_what_it_asks_for_is_posted_while_one_stands(tmp_path,
                                                                                               monkeypatch):
    """Two messages framed together, the second deleted while she works; her
    answer covers both, and a turn the transcript does not show then runs out
    of budget, so her note can name none of what it asks for and holds them
    all: the answer is posted, the first standing, and the note after it."""
    def result(out=None):
        return ({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(out),
                 "structured_output": out} if out else
                {"type": "result", "subtype": "error_max_budget_usd", "is_error": True})
    results = [result(answer("Yes, at 5.")), result()]
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [vault.Turn(True, [])])
    runner = RecordingRunner(RunResult(ok=False, envelope=results[-1], error="error_max_budget_usd",
                                       results=results))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    keep(store, dm(f"{AT:.1f}", "is the plumber coming?"))
    keep(store, dm(f"{AT + 10:.1f}", "and when?"))
    run = runner.run

    async def deleted_while_it_runs(prompt, **kw):
        await p.handle_slack(deletion(f"{AT + 10:.1f}"))
        return await run(prompt, **kw)
    monkeypatch.setattr(runner, "run", deleted_while_it_runs)

    async def go():
        for task, keys in p.kept():
            p.take_up(task, keys)
        while p._bg:
            await asyncio.sleep(0.01)
    asyncio.run(go())
    assert len(runner.calls) == 1 and p.slack.replies == ["Yes, at 5.", main.FAILED_REST]


def test_a_refused_session_with_one_of_its_two_messages_deleted_holds_the_other(tmp_path, monkeypatch, caplog):
    """Nothing said, and Claude Code refusing the session: the message that
    stands is held, so the line names the one deleted and not the turn's as
    posted nowhere."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, startup_s=1.0, refuse={
        "turn": "first", "error": "rate_limit", "said": "You've hit your limit · resets 5pm"})
    first, second = dm(f"{AT:.1f}", "is the plumber coming?"), dm(f"{AT + 10:.1f}", "and when?")
    keep(store, first), keep(store, second)

    async def go():
        for task, keys in p.kept():
            p.take_up(task, keys)
        await asyncio.sleep(0.5)
        await p.handle_slack(deletion(f"{AT + 10:.1f}"))
        while p._bg:
            await asyncio.sleep(0.01)
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(go())
    assert [r[:2] for r in kept(store)] == [(f"{AT:.1f}", "held")]
    [said] = session_lines(caplog)
    assert said.endswith(", failed: You've hit your limit · resets 5pm; held, 1 of its 2 messages deleted"), said


def test_a_silent_session_whose_every_message_was_deleted_says_so(tmp_path, monkeypatch, caplog):
    """The line reads `silent, 1 of its 1 messages deleted`, not a bare
    `silent`, which would read as a DM left unanswered."""
    import logging

    runner = RecordingRunner(answer(""))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    line = dm(f"{AT:.1f}", "the dentist moved to the 14th")
    keep(store, line)
    run = runner.run

    async def deleted_while_it_runs(prompt, **kw):
        await p.handle_slack(deletion(f"{AT:.1f}"))
        return await run(prompt, **kw)
    monkeypatch.setattr(runner, "run", deleted_while_it_runs)
    with caplog.at_level(logging.INFO, logger="wanda"):
        settle(p, p.handle_slack(line))
    assert p.slack.replies == [] and kept(store) == []
    [said] = session_lines(caplog)
    assert said.endswith(", silent, 1 of its 1 messages deleted")


def test_a_framed_session_whose_turn_holds_no_message_posts_its_answer(tmp_path, monkeypatch):
    """Nothing it answers was deleted: a frame whose turn holds no message
    still posts what she says."""
    runner = RecordingRunner(answer("Time to call the plumber."))
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")
    now = datetime.fromtimestamp(AT, p.cfg.zone)

    async def frame(again):
        return "an arrival", now, Additions([], lambda m: None, main.Holding(store))
    asyncio.run(p.memory_turn(task, None, None, channel="D1", reply_thread=None, owed=False, frame=frame))
    assert p.slack.replies == ["Time to call the plumber."]


def test_a_stop_after_an_answer_to_a_message_then_deleted_leaves_nothing_owed(tmp_path, monkeypatch):
    """The answer the session gave before the stop cut a later turn short is
    the run's, and with its one message deleted is owed to no one: the next
    start posts nothing."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], notify="after", hang="later")
    line = dm(f"{AT:.1f}", "can you remind me at 5")
    keep(store, line)

    async def go():
        t = asyncio.create_task(p.handle_slack(line))
        p._bg.add(t)
        await moment(("structured_output", 1, 0.2), p.cfg.vault_dir)
        await p.handle_slack(deletion(f"{AT:.1f}"))
        await p.shutdown(grace_s=1)
    asyncio.run(go())
    [run] = [dict(r) for r in store._query("SELECT status, notified, result_text FROM runs")]
    assert run == {"status": "ok", "notified": 1, "result_text": "one answer to 1: can you remind me at 5"}
    assert kept(store) == [] and store.pending_deliveries() == []
    again = RecordingRunner()
    q = started_again(p, again, slack)
    asyncio.run(q.deliver_pending())
    assert again.calls == [] and slack.replies == []


def test_a_stop_during_a_further_turn_leaves_an_answer_to_a_message_deleted_meanwhile_owed_to_no_one(tmp_path,
                                                                                                      monkeypatch):
    """The first message is answered, a follow-up runs a further turn, and the
    first is deleted while that turn runs, before the stop: the answer
    answers the deleted message alone, so it is owed to no one, and the next
    start posts only the follow-up's answer."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.0, steps_later=[30])

    async def go():
        for at, ev in ((0, dm(f"{AT:.1f}", "can you remind me at 5")),
                       (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber"))):
            await moment(at, p.cfg.vault_dir)
            keep(store, ev)
            t = asyncio.create_task(p.handle_slack(ev))
            p._bg.add(t)
        await moment(("tool_use", 2, 0.2), p.cfg.vault_dir)
        await p.handle_slack(deletion(f"{AT:.1f}"))
        await p.shutdown(grace_s=5)
    asyncio.run(go())
    [run] = [dict(r) for r in store._query("SELECT status, notified, result_text FROM runs")]
    assert run == {"status": "ok", "notified": 1, "result_text": "one answer to 1: can you remind me at 5"}
    assert [r[:2] for r in kept(store)] == [(f"{AT + 30:.1f}", "due")]
    started_again(p, RecordingRunner(answer("At 5, then.")), slack)
    assert slack.replies == ["At 5, then."] and kept(store) == []


def test_a_stop_during_a_further_turn_leaves_an_answer_to_two_messages_deleted_meanwhile_owed_to_no_one(
        tmp_path, monkeypatch):
    """The first message and one handed into the first turn are answered
    together, a third runs a further turn, and both are deleted while it
    runs, before the stop: the answer answers nothing that stands, and the
    next start posts only the third's answer."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[1.0, 0.2], reply_s=1.0, steps_later=[30])

    async def go():
        for at, ev in ((0, dm(f"{AT:.1f}", "can you remind me at 5")),
                       (("tool_use", 1, 0.1), dm(f"{AT + 10:.1f}", "to call the plumber")),
                       (("tool_result", 2, 0.2), dm(f"{AT + 30:.1f}", "and the dentist"))):
            await moment(at, p.cfg.vault_dir)
            keep(store, ev)
            t = asyncio.create_task(p.handle_slack(ev))
            p._bg.add(t)
        await moment(("tool_use", 3, 0.2), p.cfg.vault_dir)
        await p.handle_slack(deletion(f"{AT:.1f}"))
        await p.handle_slack(deletion(f"{AT + 10:.1f}"))
        await p.shutdown(grace_s=5)
    asyncio.run(go())
    [run] = [dict(r) for r in store._query("SELECT status, notified, result_text FROM runs")]
    assert run == {"status": "ok", "notified": 1,
                   "result_text": "one answer to 2: can you remind me at 5 | to call the plumber"}
    assert [r[:2] for r in kept(store)] == [(f"{AT + 30:.1f}", "due")]
    started_again(p, RecordingRunner(answer("The dentist, noted.")), slack)
    assert slack.replies == ["The dentist, noted."] and kept(store) == []


# --- a direct message or a mention whose first answer was empty: the session
# is told nothing would be sent, once, and its answer to that stands ---

def told_nothing(whom="fan"):
    return vault.NOTHING_SENT.format(whom=whom)


def answered_with(*said):
    """The stand-in's answer to the turns it was handed, in order."""
    return "one answer to {}: {}".format(len(said), " | ".join(said))


@pytest.mark.parametrize("late", [False, True], ids=["its entry at once", "its entry written late"])
def test_an_empty_first_answer_to_a_dm_is_told_and_its_next_answer_posted(tmp_path, monkeypatch, caplog, late):
    """Her answer to the line is posted, with no note; with the line's turn's
    opening entry written after its result, the input still closes at that
    result, by the read between results, not at the session's timeout."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], silent="first", late_entry=late)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    line = dm(f"{AT:.1f}", "the dentist moved to the 14th")
    keep(store, line)
    start = time.monotonic()
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, line))
    reply = answered_with("the dentist moved to the 14th", told_nothing())
    assert slack.replies == [reply] and time.monotonic() - start < 10
    assert failed_runs(store) == [("agent", "ok", None)] and kept(store) == []
    assert store.get_meta("failed_runs") is None
    [said] = session_lines(caplog)
    assert said.endswith(f", {len(reply)} characters to post; told nothing would be sent")


def test_the_line_answered_with_nothing_stands(tmp_path, monkeypatch, caplog):
    """Her silence after it is hers: no note follows. Its entry written late,
    the input closes at its result by the read between results."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], silent="all", late_entry=True)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    line = dm(f"{AT:.1f}", "the dentist moved to the 14th")
    keep(store, line)
    start = time.monotonic()
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, line))
    assert slack.replies == [] and time.monotonic() - start < 10 and kept(store) == []
    assert failed_runs(store) == [("agent", "ok", None)] and store.get_meta("failed_runs") is None
    [said] = session_lines(caplog)
    assert said.endswith(", silent; told nothing would be sent")


@pytest.mark.parametrize("where", ["a DM", "a group DM", "a rerun turn"])
def test_the_lines_turn_failing_with_nothing_to_post_gets_her_note(tmp_path, monkeypatch, caplog, where):
    """FAILED wherever it is, since the message was said to her: one entry
    for the alert, and no second session."""
    import logging

    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    nudged = {"steps": [0.2], "silent": "first", "fail": 1}
    sessions = [{"steps": [0.2], "reply_s": 1.5, "fail": "later"}, nudged] if where == "a rerun turn" else [nudged]
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, slack=slack, sessions=sessions)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    first = dm(f"{AT:.1f}", "can you remind me at 5", channel_type="mpim" if where == "a group DM" else "im")
    if where == "a group DM":
        first.payload["mentioned"] = True
    with caplog.at_level(logging.INFO, logger="wanda"):
        if where == "a rerun turn":
            conversation(p, (0, first), (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber")))
        else:
            conversation(p, (0, first))
    assert slack.replies == ([answered_with("can you remind me at 5")] if where == "a rerun turn" else []) + [
        main.FAILED]
    assert len(list(vault.transcripts_dir(tmp_path / "vault").glob("*.jsonl"))) == len(sessions)
    *_, entry = json.loads(store.get_meta("failed_runs"))
    assert entry["then"] == "not tried again, a note asked for it again"
    assert len(json.loads(store.get_meta("failed_runs"))) == len(sessions)
    said = session_lines(caplog)[-1]
    assert said.endswith(": error_during_execution; a note asks for it again; told nothing would be sent")


@pytest.mark.parametrize("notify, after", [("after", False), ({"after": 1}, False), ({"after": 1}, True)],
                         ids=["a notice's turn before the line", "a notice's turn after it",
                              "a notice's turn after it, the line's entry written late"])
def test_the_lines_turn_failing_beside_a_notices_turn_that_says_nothing_gets_her_note(tmp_path, monkeypatch,
                                                                                    notify, after):
    """The line's result is the one its turn gave, however the notice's turn
    falls beside it."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], silent="first", notify=notify,
                                           fail=2 if notify == "after" else 1, late_entry=after)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    keep(store, line := dm(f"{AT:.1f}", "the dentist moved to the 14th"))
    conversation(p, (0, line))
    assert slack.replies == [main.FAILED] and kept(store) == []


def test_a_notices_turn_that_answers_after_the_lines_failed_one_is_posted(tmp_path, monkeypatch):
    """What she said there is posted, and her note would contradict it."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], silent="first",
                                           notify={"after": 1, "says": True}, fail=1)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    keep(store, line := dm(f"{AT:.1f}", "the dentist moved to the 14th"))
    conversation(p, (0, line))
    assert slack.replies == [answered_with("the dentist moved to the 14th", told_nothing())]
    assert main.FAILED not in slack.replies


def test_a_notices_turn_that_answers_before_the_lines_failed_one_is_posted(tmp_path, monkeypatch):
    """A notice's turn between the empty first answer and the line says
    something, and the line's turn then fails, the session's last: her
    answer is posted, and no note contradicts it."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], silent="first",
                                           notify={"after": 0, "says": True}, fail=2)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    keep(store, line := dm(f"{AT:.1f}", "the dentist moved to the 14th"))
    conversation(p, (0, line))
    assert slack.replies == [answered_with("the dentist moved to the 14th")]


def test_with_the_transcript_unreadable_the_lines_turn_failing_after_a_notices_gets_her_note(tmp_path,
                                                                                            monkeypatch):
    """The result taken as the line's turn's, the next after it, is a
    notice's turn's that said nothing: the failure after it is read as the
    line's turn's, and her note follows."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], silent="first",
                                           notify={"after": 0}, fail=2)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    monkeypatch.setattr("wanda.vault.transcripts_dir", lambda v: tmp_path / "nowhere")
    keep(store, line := dm(f"{AT:.1f}", "the dentist moved to the 14th"))
    conversation(p, (0, line))
    assert slack.replies == [main.FAILED] and kept(store) == []


def test_with_the_transcript_unreadable_a_silence_to_the_line_then_a_notices_failed_turn_gets_her_note(
        tmp_path, monkeypatch, caplog):
    """The line's turn says nothing, and a notice's turn after it fails: with
    no transcript that reads as a notice's silence and the line's turn
    failing, and her note follows, so that no message said to her is left
    with nothing, though here she chose to say nothing."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], silent="all",
                                           notify={"after": 1}, fail=2)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    monkeypatch.setattr("wanda.vault.transcripts_dir", lambda v: tmp_path / "nowhere")
    keep(store, line := dm(f"{AT:.1f}", "the dentist moved to the 14th"))
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, line))
    assert slack.replies == [main.FAILED] and kept(store) == []
    [said] = session_lines(caplog)
    assert said.endswith(", failed: error_during_execution; a note asks for it again; told nothing would be sent")


def test_a_guess_at_the_lines_turn_gives_way_to_the_transcript_once_the_session_has_ended(tmp_path, monkeypatch,
                                                                                          caplog):
    """The line's turn fails, a notice's turn reports after it, and the
    transcript shows nothing for the line while the session runs: once the
    session sits between turns, the input is closed with the notice's result
    taken as the line's, but the transcript read once the session has ended
    names the line's own result, which failed, and her note follows."""
    import logging

    from wanda import runner

    monkeypatch.setattr(runner, "LINE_SHOWN_WITHIN_S", 1.0)
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], silent="first",
                                           notify={"after": 1}, fail=1)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    ended, shown, run = [], vault.line_turn, p.runner.run
    monkeypatch.setattr(vault, "line_turn", lambda v, sid, line: shown(v, sid, line) if ended else False)

    async def run_to_its_end(*a, **kw):
        try:
            return await run(*a, **kw)
        finally:
            ended.append(True)
    monkeypatch.setattr(p.runner, "run", run_to_its_end)
    keep(store, line := dm(f"{AT:.1f}", "the dentist moved to the 14th"))
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, line))
    assert "its last result is taken as that turn's" in caplog.text
    assert slack.replies == [main.FAILED] and kept(store) == []
    said = session_lines(caplog)[-1]
    assert said.endswith(", failed: error_during_execution; a note asks for it again; told nothing would be sent")


def test_the_lines_turn_running_out_of_time_in_a_quiet_step_gets_her_note(tmp_path, monkeypatch, caplog):
    """A notice's turn reports after the empty answer, and the line's turn,
    its entry written after its result, runs a step longer than
    LINE_SHOWN_WITHIN_S and the session's time: a turn under way is never
    taken for the session's quiet, so nothing is guessed, the session ended
    before the line's turn gave a result, and her note follows."""
    import logging

    from wanda import runner

    monkeypatch.setattr(runner, "LINE_SHOWN_WITHIN_S", 1.0)
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], silent="first",
                                           notify={"after": 0}, late_entry=True, steps_later=[30.0])
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 6)
    keep(store, line := dm(f"{AT:.1f}", "the dentist moved to the 14th"))
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, line))
    assert "its last result is taken as that turn's" not in caplog.text
    assert slack.replies == [main.FAILED] and kept(store) == []
    said = session_lines(caplog)[-1]
    assert said.endswith(", failed: timed out after 6s; a note asks for it again; told nothing would be sent"), said


def test_the_lines_turn_refused_holds_the_message_naming_the_session(tmp_path, monkeypatch, caplog):
    """No note: held while Claude Code refuses, every row naming the session,
    which ran a turn and may have written to memory, so that the session that
    takes it up is told of it."""
    import logging

    p, store, _, slack = standin_processor(tmp_path, monkeypatch, sessions=[
        {"steps": [0.2], "silent": "first", "notify": "after",
         "refuse": {"turn": 2, "error": "rate_limit", "said": "You've hit your limit · resets 5pm"}},
        {"steps": [0.2]}])
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    keep(store, line := dm(f"{AT:.1f}", "the dentist moved to the 14th"))
    with caplog.at_level(logging.INFO, logger="wanda"):
        conversation(p, (0, line))
    assert slack.replies == [] and failed_runs(store) == [("agent", "refused", "You've hit your limit · resets 5pm")]
    [sid] = [r["session_id"] for r in store._query("SELECT session_id FROM runs")]
    assert [(r[1], r[3]) for r in kept(store)] == [("held", sid)]
    assert session_lines(caplog)[-1].endswith("; held; told nothing would be sent")
    store.end_hold()
    a_pass(p)
    assert slack.replies == [answered_with("the dentist moved to the 14th")] and kept(store) == []
    assert vault.RETRIED.format(sid8=sid[:8]) in opening_text(tmp_path, -1)


def test_the_lines_turn_refused_before_a_notices_report_holds_the_message(tmp_path, monkeypatch):
    """The refusal is read from the line's turn's own result, not from the
    session's last, a notice's turn's report: held, naming the session, the
    hold begun, and alerted as Claude Code's usage limit."""
    monkeypatch.setattr("wanda.vault.line_turn", lambda v, sid, line: 1)
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [
        vault.Turn(True, []), vault.Turn(False, []), vault.Turn(False, [])])
    runner = Told([turn_result(answer("")), turn_result(said="You've hit your limit · resets 5pm"),
                   turn_result(answer(""))], [None, "rate_limit", None])
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    keep(store, line := dm(f"{AT:.1f}", "the dentist moved to the 14th"))
    asyncio.run(p.handle_slack(line))
    sid = runner.calls[0][1]["session_id"]
    assert p.slack.replies == [] and kept(store) == [(f"{AT:.1f}", "held", 0, sid)]
    assert store.get_meta("held_since") is not None
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert failed["why"] == "usage limit"


def test_a_dm_deleted_while_the_lines_turn_runs_gets_no_note_when_it_fails(tmp_path, monkeypatch):
    """A DM deleted while the line's turn runs, which then fails: no note, as
    for any turn whose messages were all deleted."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], steps_later=[1.0], silent="first",
                                           fail=1)
    monkeypatch.setattr(p.cfg, "agent_timeout_s", 20)
    keep(store, line := dm(f"{AT:.1f}", "the dentist moved to the 14th"))
    conversation(p, (0, line), (("tool_use", 2, 0.1), deletion(f"{AT:.1f}")))
    assert slack.replies == [] and kept(store) == [] and store.pending_deliveries() == []


class Told(RecordingRunner):
    """A session that writes the line after its first result, as the runner
    would, and gives `results`; it runs no reader, so which turn took the
    line is read only once it has ended."""

    def __init__(self, results, api_errors=None):
        super().__init__(ended(results, api_errors))
        self.results = results

    async def run(self, prompt, **kw):
        kw["feed"].nothing_sent(self.results[0], 0, kw["timeout_s"])
        return await super().run(prompt, **kw)


@pytest.mark.parametrize("given, note", [(2, []), (1, [main.FAILED])], ids=["its result given", "none given"])
def test_the_lines_turn_is_read_once_the_session_has_ended(tmp_path, monkeypatch, caplog, given, note):
    """A session that ended with its input still open after the line: the
    transcript names the turn that took it, and a result that turn gave is
    the line's, her silence there standing; with none, the session ended
    before answering it."""
    import logging

    monkeypatch.setattr("wanda.vault.line_turn", lambda v, sid, line: 1)
    runner = Told([turn_result(answer(""))] * given)
    p, store, _ = memory_processor(tmp_path, ConversationSlack(history=[]), runner, monkeypatch)
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "the dentist moved to the 14th")))
    assert len(runner.calls) == 1 and p.slack.replies == note
    [said] = session_lines(caplog)
    assert said.endswith("silent; told nothing would be sent" if given == 2 else
                         "; a note asks for it again; told nothing would be sent")


def test_a_clock_run_with_no_kept_messages_is_posted_later_as_ever(tmp_path, monkeypatch):
    """Nothing it answers can be deleted."""
    p, store, _ = memory_processor(tmp_path, Refusing(history=[]), RecordingRunner(answer("Time to call the plumber.")),
                                   monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")
    asyncio.run(p.memory_turn(task, "It is 17:00, as asked:\n\n    call the plumber",
                              datetime.fromtimestamp(AT, p.cfg.zone), channel="D1", reply_thread=None, owed=False))
    assert len(store.pending_deliveries()) == 1
    p.slack = ConversationSlack(history=[])
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == ["Time to call the plumber."] and store.pending_deliveries() == []


class ChangingSlack(ConversationSlack):
    """A private channel whose member list is `first` when the session's
    frame is built and `later` after it; a message added while the session
    works reads no list. A list given as None raises, as one Slack will not
    give does."""

    def __init__(self, first, later):
        super().__init__(members=first, history=[])
        self.later = later
        self.calls = 0

    async def members(self, channel):
        self.calls += 1
        ids = self.member_ids if self.calls == 1 else self.later
        if ids is None:
            raise RuntimeError("ratelimited")
        return ids


def test_a_message_added_after_someone_joined_is_taken_in(tmp_path, monkeypatch):
    """The one answer reaches whoever reads when it is posted, the newcomer
    included, as any answer does."""
    slack = ChangingSlack(["U1", "UBOT"], ["U1", "U2", "UBOT"])
    p, _, _, slack = standin_processor(tmp_path, monkeypatch, slack=slack, steps=[1.0, 0.2])
    conversation(p, (0, dm(f"{AT:.1f}", "what should I get mei for her birthday?", channel_type="group",
                          channel="C1")),
                 (("tool_use", 1, 0.1), dm(f"{AT + 30:.1f}", "keep it quiet from <@U3>", channel_type="group",
                                            channel="C1")))
    said = "keep it quiet from @“jane” (outside the household)"
    assert slack.replies == [f"one answer to 2: what should I get mei for her birthday? | {said}"]
    assert slack.calls == 1
    assert handed_texts(tmp_path) == [[vault.added_text("channel", "fan", said, "16:40")]]


def test_a_session_that_could_not_list_its_readers_takes_an_added_message(tmp_path, monkeypatch):
    p, _, _, slack = standin_processor(tmp_path, monkeypatch, slack=ChangingSlack(None, None), steps=[1.0, 0.2])
    conversation(p, (0, dm(f"{AT:.1f}", "dinner at 7?", channel_type="group", channel="C1")),
                 (("tool_use", 1, 0.1), dm(f"{AT + 30:.1f}", "and the gift", channel_type="group",
                                            channel="C1")))
    assert slack.replies == ["one answer to 2: dinner at 7? | and the gift"]
    assert handed_texts(tmp_path) == [[vault.added_text("channel", "fan", "and the gift", "16:40")]]


def test_a_follow_up_in_a_thread_a_mention_began_is_framed_where_the_mention_was(tmp_path, monkeypatch):
    slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
    p, _, _, slack = standin_processor(tmp_path, monkeypatch, slack=slack, steps=[1.0, 0.2])
    mention = dm(f"{AT:.1f}", "what's on saturday?", channel_type="group", channel="C1", thread=f"{AT:.1f}")
    mention.payload["in_thread"] = False
    conversation(p, (0, mention), (("tool_use", 1, 0.1), dm(f"{AT + 30:.1f}", "and sunday", channel_type="group",
                                                            channel="C1", thread=f"{AT:.1f}")))
    assert handed_texts(tmp_path) == [[vault.added_text("channel", "fan", "and sunday", "16:40")]]


# --- the household's names, from Slack ---

def not_found(error="user_not_found"):
    from slack_sdk.errors import SlackApiError
    return SlackApiError("The request to the Slack API failed.", {"ok": False, "error": error})


def daemon(tmp_path, monkeypatch, answers=None, *, snapshot="none", then=None, alert_ok=True):
    """A daemon start against fakes, stopped after the mail loop's first
    pass, which runs `then` first if given, with the processor and the ids
    whose names have been read so far. Returns its settings, the alerts
    Slack took and the ids whose names were read, in order. `snapshot` is
    what snapshots.git holds, or an error reading it raises."""
    posted = []

    async def alert(self, text):
        if not alert_ok:
            raise RuntimeError("no network")
        posted.append(text)

    async def one_pass(self):
        if then is not None:
            await then(self, read)
        await self.drain_mail()
        os.kill(os.getpid(), signal.SIGTERM)

    def last_snapshot(cfg):
        if isinstance(snapshot, Exception):
            raise snapshot
        return snapshot

    monkeypatch.setattr("wanda.actions.slack.SlackActions.alert", alert)
    monkeypatch.setattr("wanda.main.SlackWatcher.start", connected)
    monkeypatch.setattr("wanda.main.SlackWatcher.stop", lambda self: None)
    monkeypatch.setattr("wanda.main.Processor.loop", one_pass)
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: None)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)
    read = slack_names(monkeypatch, answers)
    monkeypatch.setattr("wanda.vault.last_snapshot", last_snapshot)
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y", alert_channel="C9",
               slack_owner_user_ids="U1,U2", tz="America/Los_Angeles", email_triage=False, claude_bin="/bin/true")
    asyncio.run(main.run_daemon(c))
    return c, posted, read


def names_in(c) -> Household:
    store = Store(c.db_path)
    try:
        return Household.load(store, c.slack_owner_user_ids)
    finally:
        store.close()


FAN = {"profile": {"display_name": "fan", "real_name": "Fan Zhu"}}
MEI = {"profile": {"display_name": "mei"}}


def test_the_start_reads_every_name_and_logs_them(tmp_path, monkeypatch, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="wanda"):
        c, posted, read = daemon(tmp_path, monkeypatch)
    assert read == ["U1", "U2"] and posted == []
    assert "names: U1 as fan (display name), U2 as mei (display name)" in [r.getMessage() for r in caplog.records]
    assert names_in(c).told_names() == {"U1": "fan", "U2": "mei"}


SHOWS_NO_ONE = [not_found(), not_found("user_not_visible"), {"is_bot": True, "profile": {"display_name": "mei"}},
                {"deleted": True, "profile": {"display_name": "mei"}}]


@pytest.mark.parametrize("slack_says", SHOWS_NO_ONE + [{"profile": {"display_name": "wanda", "real_name": ""}}])
def test_an_id_slack_gives_no_usable_name_is_not_let_in_until_it_does(tmp_path, monkeypatch, caplog, slack_says):
    import logging
    with caplog.at_level(logging.INFO, logger="wanda"):
        c, posted, _ = daemon(tmp_path, monkeypatch, {"U1": FAN, "U2": slack_says})
    assert names_in(c).told_names() == {"U1": "fan"}
    assert posted == ["1 id(s) in WANDA_SLACK_OWNER_USER_IDS are not let in: Slack has no member for them, does "
                      "not show them, or gives no usable name; doctor says whose"]
    assert "names: U1 as fan (display name), U2 not let in" in [r.getMessage() for r in caplog.records]
    # its DM runs nothing and posts nothing, and is said once
    caplog.clear()
    runner = RecordingRunner(answer("Hi."))
    store = Store(c.db_path)
    p = Processor(c, store, asyncio.Queue(), ConversationSlack(), runner)
    with caplog.at_level(logging.WARNING, logger="wanda"):
        for i in range(2):
            asyncio.run(p.handle_slack(dm(f"{AT + i:.1f}", "hello?", channel="D2", user="U2")))
    assert runner.calls == [] and p.slack.replies == [] and store._query("SELECT * FROM runs") == []
    assert [r.getMessage() for r in caplog.records] == [
        "not taking part in D2: U2 is not let in until Slack gives a name for them (doctor)"]
    # alerted once: the next start, with the same answer, says nothing new
    store.close()
    c, posted, _ = daemon(tmp_path, monkeypatch, {"U1": FAN, "U2": slack_says})
    assert posted == []
    # a usable read lets it in
    store = Store(c.db_path)
    p = Processor(c, store, asyncio.Queue(), ConversationSlack(), runner)

    async def user_now(uid):
        return {"U1": FAN, "U2": MEI}[uid]
    p.slack.user_now = user_now
    asyncio.run(p.read_names(datetime.now(timezone.utc)))
    asyncio.run(p.handle_slack(dm(f"{AT + 9:.1f}", "hello?", channel="D2", user="U2")))
    assert p.household.told_names() == {"U1": "fan", "U2": "mei"} and len(runner.calls) == 1


@pytest.mark.parametrize("slack_says", SHOWS_NO_ONE)
def test_a_member_slack_stops_showing_stays_let_in_by_the_name_sessions_know(tmp_path, monkeypatch, slack_says):
    store = Store(tmp_path / "wanda.db")
    told(store)
    store.set_meta("vault_since", "2026-09-01")
    store.close()
    c, posted, _ = daemon(tmp_path, monkeypatch, {"U1": FAN, "U2": slack_says})
    h = names_in(c)
    assert h.told_names() == {"U1": "fan", "U2": "mei"}
    assert h.rows["U2"]["slack"]["read"] == "2026-09-01T00:00:00+00:00", "the answer does not stand as a read"
    assert posted == ["1 household member(s) are no longer shown by Slack, and are let in under the name sessions "
                      "know; doctor says whose"]
    c, posted, _ = daemon(tmp_path, monkeypatch, {"U1": FAN, "U2": slack_says})
    assert posted == [], "alerted once"


@pytest.mark.parametrize("answers,named,exits", [
    # every read failed, and no id has a name: nothing to start on
    ({"U1": RuntimeError("timed out"), "U2": RuntimeError("timed out")}, None, "timed out"),
    ({"U1": not_found("invalid_auth"), "U2": not_found("invalid_auth")}, None, "invalid_auth"),
    # one failed read goes on, its id left out
    ({"U1": RuntimeError("timed out"), "U2": MEI}, {"U2": "mei"}, None),
    # Slack's answer about every id is an answer, and so is one about one id
    ({"U1": not_found(), "U2": not_found()}, {}, None),
    ({"U1": RuntimeError("timed out"), "U2": not_found()}, {}, None),
])
def test_a_start_with_no_names_stops_only_when_slack_said_nothing_about_anyone(tmp_path, monkeypatch, answers,
                                                                              named, exits):
    if exits:
        with pytest.raises(SystemExit, match=f"^could not read any name from Slack: {exits}; a session is told who "
                                             "is speaking by it$"):
            daemon(tmp_path, monkeypatch, answers)
        return
    c, _, _ = daemon(tmp_path, monkeypatch, answers)
    assert names_in(c).told_names() == named


def test_a_start_with_names_goes_on_whatever_slack_answers(tmp_path, monkeypatch):
    store = Store(tmp_path / "wanda.db")
    told(store)
    store.close()
    down = not_found("service_unavailable")
    c, _, read = daemon(tmp_path, monkeypatch, {"U1": down, "U2": down})
    assert read == ["U1", "U2"] and names_in(c).told_names() == {"U1": "fan", "U2": "mei"}


@pytest.mark.parametrize("snapshot,lost", [
    ("none", False),
    ("1a2b3c4 after 6f0d", True),
    (OSError("git log: exit 128: fatal: not a git repository"), True),
])
def test_a_run_store_started_afresh_beside_a_vault_with_history_is_said(tmp_path, monkeypatch, snapshot, lost):
    """Every name sessions were told before is gone with the old store: the
    first start says so in the log and the alerts, until Slack takes it,
    even when no id gives a name. A first start says nothing."""
    answers = {"U1": not_found(), "U2": not_found()}
    c, posted, _ = daemon(tmp_path, monkeypatch, answers, snapshot=snapshot, alert_ok=False)
    store = Store(c.db_path)
    row = json.loads(store.get_meta("store_lost") or "null")
    assert (row is not None) == lost
    store.close()
    if not lost:
        return
    assert row["alerted"] is False and row["snapshot"] != "none"
    # the next start finds the store knows the vault, and flushes what waits
    c, posted, _ = daemon(tmp_path, monkeypatch, answers, snapshot="1a2b3c4 after 6f0d")
    assert posted == ["the run store was started afresh beside a vault with history: the names household "
                      "members had before are not known (README, State)\n2 id(s) in WANDA_SLACK_OWNER_USER_IDS "
                      "are not let in: Slack has no member for them, does not show them, or gives no usable name; "
                      "doctor says whose"]
    store = Store(c.db_path)
    assert json.loads(store.get_meta("store_lost"))["alerted"] is True
    store.close()
    c, posted, _ = daemon(tmp_path, monkeypatch, answers, snapshot="1a2b3c4 after 6f0d")
    assert posted == []


def test_the_names_alert_waits_for_slack_and_says_each_event_once(tmp_path):
    p, store = make(tmp_path, slack_owner_user_ids="U1,U2,U3", tz="America/Los_Angeles")
    told(store, {"U1": "fan", "U3": "jo"})
    store.set_meta("store_lost", json.dumps({"at": "2026-10-03T08:00:00+00:00", "snapshot": "1a2b3c4 x",
                                             "alerted": False}))
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    now = datetime.now(timezone.utc)
    p.household.unread("U2", "user_not_found", now)
    p.household.unread("U3", "deleted", now)

    async def refused(text):
        raise RuntimeError("no network")
    p.slack.alert = refused
    asyncio.run(p._flush_names())
    assert json.loads(store.get_meta("store_lost"))["alerted"] is False
    p.slack = FakeSlack()
    asyncio.run(p._flush_names())
    asyncio.run(p._flush_names())
    [said] = p.slack.alerts
    assert said.split("\n") == [
        "the run store was started afresh beside a vault with history: the names household members had before are "
        "not known (README, State)",
        "1 id(s) in WANDA_SLACK_OWNER_USER_IDS are not let in: Slack has no member for them, does not show them, or "
        "gives no usable name; doctor says whose",
        "1 household member(s) are no longer shown by Slack, and are let in under the name sessions know; doctor "
        "says whose"]
    assert not any(w in said for w in ("U1", "U2", "U3", "fan", "jo"))
    # one that flaps is said once a UTC day
    p.household.observe("U3", {"profile": {"display_name": "jo"}}, now)
    p.household.unread("U3", "deleted", now)
    asyncio.run(p._flush_names())
    assert len(p.slack.alerts) == 1
    p.household.rows["U3"]["out_day"] = "2026-01-01"
    asyncio.run(p._flush_names())
    assert p.slack.alerts[1] == ("1 household member(s) are no longer shown by Slack, and are let in under the name "
                                 "sessions know; doctor says whose")
    # what was marked is in the store
    assert Household.load(store, ["U3"]).rows["U3"]["out"]["alerted"] is True


def test_the_names_alert_marks_what_it_said_whatever_a_read_does_meanwhile(tmp_path):
    """A read, a re-look or a names session while Slack takes the message
    can end or replace the event it was about, and the flush marks the event
    it said. A keep made meanwhile is alerted by the next flush; an answer
    showing no one that began meanwhile waits for the next UTC day's."""
    p, store = make(tmp_path, slack_owner_user_ids="U1,U2,U3,U4", tz="America/Los_Angeles")
    told(store, {"U1": "fan", "U2": "mei", "U3": "jo", "U4": "ann"})
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    now = datetime.now(timezone.utc)
    p.household.unread("U2", "user_not_visible", now)
    p.household.unread("U4", "user_not_visible", now)
    p.household.keep("U1", "Fan Zhu", "s-1", "two people are Fan Zhu", False, now)
    p.household.keep("U3", "Jo Li", "s-3", "two people are Jo Li", False, now)
    sent = []

    async def alert(text):
        sent.append(text)
        await asyncio.sleep(0)
        if len(sent) == 1:
            # the refresh reads mei and Slack now answers that ann's account
            # is deleted, a re-look ends fan's keep, and a names session ends
            # in another keep for jo, while the message is in flight
            p.household.observe("U2", {"profile": {"display_name": "mei"}}, now)
            p.household.unread("U4", "deleted", now)
            p.household.kept_again("U1", True, "fan stays fan")
            p.household.keep("U3", "Jo L", "s-4", "two people are Jo L", False, now)
    p.slack.alert = alert
    asyncio.run(p._flush_names())
    assert len(sent) == 1 and p.household.rows["U2"]["out"] is None and p.household.rows["U1"]["kept"] is None
    assert p.household.rows["U4"]["out"]["alerted"] is False
    assert p.household.unalerted_keeps() == ["U3"]
    asyncio.run(p._flush_names())
    assert sent[1:] == ["1 change(s) of a household member's name in Slack were handed to memory, which did not "
                        "take the new name as theirs alone; sessions use the earlier name; doctor says whose"]
    p.household.unread("U2", "user_not_visible", now)
    asyncio.run(p._flush_names())
    assert len(sent) == 2, "said once a UTC day"
    p.household.rows["U4"]["out_day"] = "2026-01-01"
    asyncio.run(p._flush_names())
    assert sent[2:] == ["1 household member(s) are no longer shown by Slack, and are let in under the name sessions "
                        "know; doctor says whose"]


def test_a_change_seen_at_the_start_waits_and_frames_keep_the_name_sessions_know(tmp_path, monkeypatch, caplog):
    import logging
    store = Store(tmp_path / "wanda.db")
    told(store)
    store.close()
    with caplog.at_level(logging.INFO, logger="wanda"):
        c, _, _ = daemon(tmp_path, monkeypatch, {"U1": {"profile": {"display_name": "Fan Zhu"}}, "U2": MEI})
    said = [r.getMessage() for r in caplog.records]
    assert "names: U1 is Fan Zhu in Slack now; sessions say fan until memory has been told" in said
    assert "names: U1 as fan (Slack shows Fan Zhu), U2 as mei (display name)" in said
    h = names_in(c)
    assert h.awaiting("U1") == "Fan Zhu" and h.told("U1") == "fan"
    runner = RecordingRunner()
    store = Store(c.db_path)
    p = Processor(c, store, asyncio.Queue(), ConversationSlack(), runner)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "the March one")))
    assert "fan now says:\n\n    the March one" in runner.calls[0][0]


def test_the_names_are_read_again_each_round_until_the_stop(tmp_path, monkeypatch):
    """By a task of the daemon's own, which a stop cancels; each round reads
    every allowed id once, in the allowlist's order."""
    monkeypatch.setattr("wanda.main.NAMES_EVERY_S", 0.05)
    ended = []
    real = main.Processor.names_loop

    async def names_loop(self):
        try:
            await real(self)
        finally:
            ended.append(True)
    monkeypatch.setattr("wanda.main.Processor.names_loop", names_loop)

    async def three_rounds(p, read):
        while len(read) < 6:
            await asyncio.sleep(0.01)
    c, _, read = daemon(tmp_path, monkeypatch, then=three_rounds)
    assert read[:6] == ["U1", "U2"] * 3 and ended == [True]


def test_a_name_read_that_hangs_holds_back_no_session(tmp_path, monkeypatch):
    p, store, _ = memory_processor(tmp_path, ConversationSlack(), RecordingRunner(), monkeypatch)
    monkeypatch.setattr("wanda.main.NAMES_EVERY_S", 0)
    hung = asyncio.Event()

    async def user_now(uid):
        hung.set()
        await asyncio.Event().wait()
    p.slack.user_now = user_now
    started = []

    async def clock_session(w, now):
        started.append(w.key)
    p._clock_session = clock_session

    async def go():
        names = asyncio.create_task(p.names_loop())
        await hung.wait()
        p._wake([main.clock.Wake("clock:morning:U1", "U1", "x")], datetime.now(p.cfg.zone))
        await asyncio.gather(*p._bg)
        await p.handle_slack(dm(f"{AT:.1f}", "the March one"))
        names.cancel()
    asyncio.run(go())
    assert started == ["clock:morning:U1"] and len(p.runner.calls) == 1


def test_one_ids_read_that_raises_holds_back_none_after_it(tmp_path, monkeypatch, caplog):
    import logging
    p, store, _ = memory_processor(tmp_path, ConversationSlack(), RecordingRunner(), monkeypatch)
    read = []

    async def user_now(uid):
        read.append(uid)
        return {"U1": {"profile": {"display_name": "fan"}}, "U2": {"profile": {"display_name": "Mei Chen"}}}[uid]
    p.slack.user_now = user_now
    real = p.household.observe

    def observe(uid, user, now):
        if uid == "U1":
            raise ValueError("a record with no shape")
        return real(uid, user, now)
    p.household.observe = observe
    with caplog.at_level(logging.WARNING, logger="wanda"):
        asyncio.run(p.read_names(datetime.now(timezone.utc)))
    assert read == ["U1", "U2"] and p.household.awaiting("U2") == "Mei Chen"
    assert "names: reading U1 failed" in [r.getMessage() for r in caplog.records]


def test_a_round_of_reads_that_raises_is_followed_by_the_next(tmp_path, monkeypatch):
    p, store, _ = memory_processor(tmp_path, ConversationSlack(), RecordingRunner(), monkeypatch)
    monkeypatch.setattr("wanda.main.NAMES_EVERY_S", 0.01)
    rounds = []

    async def read_names(now, *, start=False):
        rounds.append(now)
        if len(rounds) == 1:
            raise RuntimeError("a round that raises")
        return []
    p.read_names = read_names

    async def go():
        names = asyncio.create_task(p.names_loop())
        try:
            while len(rounds) < 2:
                await asyncio.sleep(0.01)
        finally:
            names.cancel()
    asyncio.run(asyncio.wait_for(go(), 3))


def test_an_allowed_id_not_let_in_is_of_the_household_and_starts_nothing(tmp_path, monkeypatch, caplog):
    """An allowed id Slack has given no usable name is not let in: its own
    messages start nothing, logged once. In a member's frame it is of the
    household, quoted by the name Slack shows, not marked outside."""
    import logging

    runner = RecordingRunner()
    p, store, _ = memory_processor(tmp_path, ConversationSlack(members=["U1", "U2", "UBOT"], history=[]),
                                   runner, monkeypatch)
    store._exec("DELETE FROM meta WHERE key='names:U2'")
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    with caplog.at_level(logging.WARNING, logger="wanda"):
        for i in range(2):
            asyncio.run(p.handle_slack(dm(f"{AT + i:.1f}", "hello?", channel_type="mpim", channel="G1", user="U2")))
        asyncio.run(p.handle_slack(dm(f"{AT + 2:.1f}", "dinner at 7", channel_type="mpim", channel="G1")))
    assert len(runner.calls) == 1
    assert ("In a group direct message that fan, “mei” and I read. Everyone in it sees what I say there.\n\n"
            in runner.calls[0][0])
    assert caplog.text.count("not taking part in G1: U2 is not let in") == 1


def test_a_member_keeps_one_spelling_through_a_session(tmp_path, monkeypatch):
    """A change of capitals while a session works: a message added to it is
    framed with the name its opening frame used, and the next turn's frame
    with the new one."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[1.0, 0.2])

    async def go():
        first = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "can you remind me at 5")))
        await moment(("tool_use", 1, 0.1), p.cfg.vault_dir)
        p.household.observe("U1", {"profile": {"display_name": "FAN"}}, datetime.now(timezone.utc))
        added = asyncio.create_task(p.handle_slack(dm(f"{AT + 30:.1f}", "to call the plumber")))
        await asyncio.gather(first, added)
    asyncio.run(go())
    assert handed_texts(tmp_path) == [[vault.added_text("dm", "fan", "to call the plumber", "16:40")]]
    assert p.household.told("U1") == "FAN"


def test_doctor_shows_each_members_name_from_the_store_alone(tmp_path, capsys):
    from wanda.main import run_doctor

    c = Config(_env_file=None, data_dir=tmp_path, claude_bin="/bin/true", email_triage=False,
               slack_owner_user_ids="U1,U2,U3", tz="America/Los_Angeles")
    asyncio.run(run_doctor(c, smoke=False))
    out = capsys.readouterr().out
    assert "✓ memory settings — U1, U2, U3 allowed, each let in once Slack gives a name (slack: below); time zone " \
           "America/Los_Angeles; 1 session(s) at once" in out
    assert "✓ U1 — no name yet; the first start reads Slack" in out
    store = Store(c.db_path)
    told(store, {"U1": "fan"}, at=datetime.now(timezone.utc))
    h = Household.load(store, c.slack_owner_user_ids)
    h.unread("U2", "user_not_found", datetime.now(timezone.utc))
    h.save(store, "U2")
    store.set_meta("store_lost", json.dumps({"at": "2026-10-03T08:00:00+00:00", "snapshot": "1a2b3c4 x",
                                             "alerted": False}))
    store.close()
    asyncio.run(run_doctor(c, smoke=False))
    out = capsys.readouterr().out
    assert "  ✗ run store — started afresh on 2026-10-03 beside a vault with history\n" in out
    assert "  ✓ U1 — sessions say fan, since " in out and "Slack shows fan (display name fan, full name empty)" in out
    assert "  ✗ U2 — not let in: WANDA_SLACK_OWNER_USER_IDS lists U2, which Slack has no member for\n" in out
    assert "  ✓ U3 — no name yet; the first start reads Slack\n" in out
    store = Store(c.db_path)
    assert store._query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0, "doctor writes no task"
    assert sorted(store.meta_starting("names:")) == ["names:U1", "names:U2"]


def test_names_come_from_slack_alone(monkeypatch):
    """compose no longer asks for a name map, nor does .env.example; an .env
    that still has one starts as before."""
    root = Path(__file__).resolve().parent.parent
    for f in ("compose.wanda.yaml", ".env.example", "README.md"):
        assert "WANDA_SLACK_NAMES" not in (root / f).read_text(), f
    monkeypatch.setenv("WANDA_SLACK_NAMES", "U1:fan,U2:mei")
    assert Config(_env_file=None, slack_owner_user_ids="U1,U2").slack_owner_user_ids == ["U1", "U2"]


# --- a change of name, handed to memory ---

ROUND = timedelta(seconds=NAMES_EVERY_S)


class Memory:
    """Stands in for `mem show "person:<name>"` on a vault of people, each a
    label, the names a rename struck and the session that made it: found by
    label or by a struck name, in any capitals, and printed as the build
    prints one person, several or none. `fail` is what a read raises while
    it is set, and `hold`, while set, an event each read waits for."""

    def __init__(self, *names):
        self.people: dict[str, dict] = {}
        self.fail: Exception | None = None
        self.hold: asyncio.Event | None = None
        self.reads: list[str] = []
        for name in names:
            self.make(name, "s-hand")

    def make(self, name, made):
        pid = f"person:{len(self.people) + 1:06x}"
        self.people[pid] = {"name": name, "was": [], "made": made}
        return pid

    def named(self, name):
        return [pid for pid, p in self.people.items() if name.lower() in (n.lower() for n in (p["name"], *p["was"]))]

    def rename(self, old, new):
        [pid] = [pid for pid, p in self.people.items() if p["name"].lower() == old.lower()]
        self.people[pid]["was"].append(self.people[pid]["name"])
        self.people[pid]["name"] = new

    def forget(self, pid):
        del self.people[pid]

    async def call(self, now, *args):
        if args[0] != "show":
            return 0, "", ""  # the clock's due check, with nothing due
        name = args[1].removeprefix("person:")
        self.reads.append(name)
        if self.hold is not None:
            await self.hold.wait()
        if self.fail is not None:
            raise self.fail
        hits = self.named(name)
        if len(hits) == 1:
            p = self.people[hits[0]]
            return 0, (f"{hits[0]}\n---\nname: {json.dumps(p['name'])}\n" + f"made: {json.dumps(p['made'])}\n"
                       "---\n\n" + "".join(f"~~was named: {w}~~\n" for w in p["was"])), ""
        if hits:
            listed = "; ".join(f"{pid} ({self.people[pid]['name']})" for pid in hits)
            return 1, f"({name!r} is more than one node: {listed}. An id says which.)\n", ""
        return 1, (f"(no person is named {name!r}; people/CLAUDE.md lists the people, and `mem search` finds by "
                   "other words)\n"), ""


class Naming(RecordingRunner):
    """Stands in for claude in a names session: each run does to memory what
    the next of `acts` does, given the memory and the session's id, and
    answers, unless the act returns a result of its own; an act may be a
    coroutine, which the run awaits."""

    def __init__(self, memory, *acts):
        super().__init__()
        self.memory, self.acts = memory, list(acts)

    async def run(self, prompt, **kw):
        self.calls.append((prompt, kw))
        act = self.acts.pop(0) if self.acts else None
        out = act(self.memory, kw["session_id"]) if act else None
        if asyncio.iscoroutine(out):
            out = await out
        if isinstance(out, RunResult):
            return out
        said = answer("I renamed fan's node to Fan Zhu.")
        return RunResult(ok=True, structured=said, result_text=json.dumps(said), session_id="ignored")


def renames(old="fan", new="Fan Zhu"):
    return lambda m, sid: m.rename(old, new)


def makes(name="Fan Zhu"):
    return lambda m, sid: m.make(name, sid)


def ends(how, then=None):
    """An act that does `then`, if given, and ends the run as `how`: timed
    out, or failed."""
    def act(m, sid):
        if then:
            then(m, sid)
        return RunResult(ok=False, timed_out=how == "timeout", error="timed out after 420s" if how == "timeout"
                         else "claude reported an error")
    return act


def waits(then=None):
    """An act that does `then`, if given, and never ends, for a stop."""
    async def act(m, sid):
        if then:
            then(m, sid)
        await asyncio.Event().wait()
    return act


def names_processor(tmp_path, monkeypatch, memory, *acts, slack=None):
    """A processor for fan and mei, told since September, whose `mem` is the
    stand-in memory and whose claude does `acts`; its Slack keeps every alert
    it is sent."""
    p, store, snaps = memory_processor(tmp_path, slack or ConversationSlack(), Naming(memory, *acts), monkeypatch)
    p._mem_call = memory.call
    return p, store, snaps


def shows(p, uid, name, at, *, full=""):
    """One read of `uid` in which Slack shows `name`."""
    p.household.observe(uid, {"profile": {"display_name": name, "real_name": full}}, at)
    p.household.save(p.store, uid)


def changed(p, uid="U1", name="Fan Zhu", at=None):
    """Slack shows `name` for `uid` in two reads a round apart, ending at
    `at`: a change that is due."""
    at = at or datetime.now(timezone.utc).replace(microsecond=0)
    shows(p, uid, name, at - ROUND)
    shows(p, uid, name, at)
    return at


def hand(p, now):
    """One idle tick's handoff, run to its end. Returns whether a session
    started."""
    async def go():
        p._hand_names(now)
        started = bool(p._bg)
        await asyncio.gather(*p._bg)
        return started
    return asyncio.run(go())


def restarted(p):
    """The processor the next start makes on the same run store, with the
    same stand-ins and slots of its own."""
    q = Processor(p.cfg, p.store, asyncio.Queue(), p.slack, p.runner)
    q.runner.agent_sem = asyncio.Semaphore(2)
    q._mem_call = p._mem_call
    return q


def tried(p, uid="U1"):
    return p.household.rows[uid]["tried"]


def names_alerts(p):
    return [a for a in p.slack.alerts if "name in Slack" in a]


def ran(store, sid, status="ok"):
    """A run recorded under session `sid`."""
    store.record_run(kind="agent", task_id=None, session_id=sid, started_at=utcnow(), exit_code=0, cost_usd=0.0,
                     status=status)


def test_a_due_change_starts_one_session_when_nothing_runs(tmp_path, monkeypatch):
    memory = Memory("fan", "mei")
    p, store, _ = names_processor(tmp_path, monkeypatch, memory, renames(), renames("mei", "Mei Chen"))
    now = changed(p)
    changed(p, "U2", "Mei Chen", now)
    other = []

    async def busy():
        await asyncio.sleep(0)

    async def go():
        # nothing while a session or a wake runs, or a run is reserved
        p._bg.add(asyncio.create_task(busy()))
        p._hand_names(now)
        await asyncio.gather(*p._bg)
        p._bg.clear()
        p._inflight_runs = 1
        p._hand_names(now)
        p._inflight_runs = 0
        assert not p._bg

        async def clock_session(w, at):
            other.append(w.key)
        p._clock_session = clock_session
        p._wake([main.clock.Wake("clock:morning:U2", "U2", "x")], now)
        p._hand_names(now)
        assert len(p._bg) == 1
        await asyncio.gather(*p._bg)
        # then one id at a time, in the allowlist's order
        p._hand_names(now)
        p._hand_names(now)
        assert len(p._bg) == 1
        await asyncio.gather(*p._bg)
        p._hand_names(now)
        await asyncio.gather(*p._bg)
    asyncio.run(go())
    assert other == ["clock:morning:U2"] and len(p.runner.calls) == 2
    assert p.household.told_names() == {"U1": "Fan Zhu", "U2": "Mei Chen"}


def test_the_clock_hands_a_change_in_quiet_hours(tmp_path, monkeypatch):
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), renames())
    monkeypatch.setattr(p.cfg, "quiet_hours", "00:01-00:00")
    monkeypatch.setattr("wanda.main.CLOCK_TICK_S", 0.01)
    changed(p)

    async def go():
        ticking = asyncio.create_task(p.clock_loop())
        while not p.runner.calls or p._bg:
            await asyncio.sleep(0.01)
        ticking.cancel()
    asyncio.run(go())
    assert len(p.runner.calls) == 1 and p.household.told("U1") == "Fan Zhu"


def test_a_session_whose_change_is_no_longer_due_does_not_run(tmp_path, monkeypatch):
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"))
    now = changed(p)
    shows(p, "U1", "fan", now + timedelta(seconds=1))
    asyncio.run(p._names_session("U1", "fan", "Fan Zhu", now))
    assert p.runner.calls == [] and tried(p) is None and "U1" not in p._naming


def test_an_advance_outlasts_a_read_in_flight(tmp_path, monkeypatch):
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), renames())
    now = changed(p)
    reading = asyncio.Event()

    async def user_now(uid):
        reading.set()
        await asyncio.sleep(0.1)
        return {"profile": {"display_name": "Fan Zhu" if uid == "U1" else "mei"}}
    p.slack.user_now = user_now

    async def go():
        refresh = asyncio.create_task(p.read_names(now + ROUND))
        await reading.wait()
        await p._names_session("U1", "fan", "Fan Zhu", now)
        await refresh
    asyncio.run(go())
    assert p.household.told("U1") == "Fan Zhu"
    assert Household.load(store, ["U1"]).told("U1") == "Fan Zhu"


def test_a_names_session_reaches_no_one(tmp_path, monkeypatch, caplog):
    """Its try is written before it runs; it is handed the frame whole, posts
    nothing, owes nothing, and its snapshot follows."""
    import logging
    memory = Memory("fan")
    seen = {}

    def act(m, sid):
        seen["tried"] = json.loads(store.get_meta("names:U1"))["tried"]
        seen["sid"] = sid
        m.rename("fan", "Fan Zhu")
    p, store, snaps = names_processor(tmp_path, monkeypatch, memory, act)
    # the tick's time, in the household's zone, dates the session
    now = changed(p).astimezone(p.cfg.zone)
    with caplog.at_level(logging.INFO, logger="wanda"):
        assert hand(p, now)
    sid = seen["sid"]
    assert seen["tried"]["session"] == sid and seen["tried"]["name"] == "Fan Zhu"
    assert seen["tried"]["error"] == "did not end" and seen["tried"]["count"] == 1
    [(prompt, kw)] = p.runner.calls
    assert prompt == vault.prompt(now.date().isoformat(), vault.renamed_text("fan", "Fan Zhu"))
    assert kw["append_system_prompt"].endswith(vault.date_paragraph(now))
    assert vault.NO_WAKE not in kw["append_system_prompt"]
    assert kw["session_id"] == sid and kw["feed"] is None
    assert p.slack.replies == []
    [run] = store._query("SELECT r.*, t.kind AS task_kind, t.thread_ts FROM runs r JOIN tasks t ON t.id = r.task_id")
    assert (run["task_kind"], run["thread_ts"], run["session_id"], run["status"], run["notified"]) == (
        "names", "names", sid, "ok", 1)
    assert run["result_text"] == "I renamed fan's node to Fan Zhu."
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == [] and snaps == [f"after {sid}"]
    said = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(f"memory session {sid} in no conversation: ") and m.endswith(" characters, posted nowhere")
               for m in said)
    assert f"names: U1 is Fan Zhu to sessions from now on (session {sid})" in said
    assert p.household.told("U1") == "Fan Zhu" and tried(p) is None and memory.reads == ["fan", "Fan Zhu"]


def test_a_names_session_whose_last_turn_says_nothing_posts_nothing(tmp_path, monkeypatch):
    """Its answer, kept by its transcript from an earlier turn, is recorded
    and posted nowhere, and there is no failure note."""
    p, store, snaps, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], notify="after")
    p._mem_call = Memory("fan").call
    now = changed(p)
    asyncio.run(p._names_session("U1", "fan", "Fan Zhu", now))
    runs = [dict(r) for r in store._query("SELECT status, notified, result_text FROM runs")]
    assert len(runs) == 1 and runs[0]["status"] == "ok" and runs[0]["notified"] == 1
    assert runs[0]["result_text"].startswith("one answer to 1: The person I have known in this Slack as fan")
    asyncio.run(p.deliver_pending())
    assert slack.replies == [] and len(snaps) == 1


def test_a_names_session_that_reports_only_filler_failed(tmp_path, monkeypatch):
    """Its run is an error, so its try failed and is made again, where an ok
    run that left fan alone would keep his name."""
    def filler(m, sid):
        return RunResult(ok=True, structured=FILLER, result_text=json.dumps(FILLER), session_id=sid)
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), filler)
    hand(p, changed(p))
    [run] = store._query("SELECT status, error FROM runs")
    assert (run["status"], run["error"]) == ("error", "the session ended without its report")
    assert tried(p)["error"] == "the session ended without its report" and p.household.rows["U1"]["kept"] is None


def frame_name(p):
    """The name the next frame of fan's DM gives him."""
    before = len(p.runner.calls)
    asyncio.run(p.handle_slack(dm(f"{AT + len(p.runner.calls):.1f}", "the March one")))
    [(prompt, _)] = p.runner.calls[before:]
    return prompt.split(" now says:")[0].rsplit("\n", 1)[1]


@pytest.mark.parametrize("act,name,kept", [
    (renames(), "Fan Zhu", None),
    (None, "fan", True),
    (makes(), "fan", False),
])
def test_each_outcome_sets_the_name_later_frames_use(tmp_path, monkeypatch, act, name, kept):
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), act or (lambda m, sid: None))
    now = changed(p)
    hand(p, now)
    assert frame_name(p) == name
    k = p.household.rows["U1"]["kept"]
    assert (k["plain"] if k else None) == kept
    if kept is False:
        # alerted once, naming no one, and not handed again while Slack shows it
        asyncio.run(p._flush_names())
        asyncio.run(p._flush_names())
        assert p.slack.alerts == ["1 change(s) of a household member's name in Slack were handed to memory, which "
                                  "did not take the new name as theirs alone; sessions use the earlier name; doctor "
                                  "says whose"]
        shows(p, "U1", "Fan Zhu", now + 2 * ROUND)
        assert not hand(p, now + 2 * ROUND)


def test_an_advance_to_a_name_another_member_was_told_meanwhile_is_held(tmp_path, monkeypatch, caplog):
    import logging

    def act(m, sid):
        m.rename("fan", "Fan Zhu")
        p.household.rows["U2"]["told"].append({"name": "fan zhu", "since": utcnow(), "session": "s-mei"})
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), act)
    with caplog.at_level(logging.INFO, logger="wanda"):
        hand(p, changed(p))
    assert p.household.told("U1") == "fan" and tried(p) is None
    assert "names: U1 stays fan to sessions: Fan Zhu is U2's" in [r.getMessage() for r in caplog.records]


def test_a_name_another_member_takes_during_a_session_is_held_for_them(tmp_path, monkeypatch):
    """fan's change to Fan Zhu is being handed when Slack moves him to Fan Z
    and mei to Fan Zhu: her read finds Fan Zhu held by his try, so her full
    name awaits instead, and no session is told mei goes by Fan Zhu."""
    def act(m, sid):
        later = datetime.now(timezone.utc) + ROUND
        shows(p, "U1", "Fan Z", later)
        shows(p, "U2", "Fan Zhu", later, full="Mei Chen")
        m.rename("fan", "Fan Zhu")
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan", "mei"), act, renames("mei", "Mei Chen"))
    now = changed(p)
    hand(p, now)
    assert p.household.told_names() == {"U1": "Fan Zhu", "U2": "mei"}
    assert p.household.awaiting("U2") == "Mei Chen"
    later = now + 3 * ROUND
    shows(p, "U2", "Fan Zhu", later, full="Mei Chen")
    shows(p, "U1", "Fan Z", later)
    assert p.household.due(store, later) == ("U1", "Fan Zhu", "Fan Z")
    assert p.household.awaiting("U2") == "Mei Chen"


def test_a_change_back_is_handed_by_a_second_session(tmp_path, monkeypatch):
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), renames(), renames("Fan Zhu", "fan"),
                                  renames("fan", "Fan Z"), renames("Fan Z", "Fan Zhu"))
    now = changed(p)
    hand(p, now)
    now = changed(p, name="fan", at=now + 2 * ROUND)
    hand(p, now)
    assert "The person I have known in this Slack as Fan Zhu is named fan there now." in p.runner.calls[1][0]
    assert p.household.told("U1") == "fan"
    # and to a name the person had two changes ago
    hand(p, changed(p, name="Fan Z", at=now + 2 * ROUND))
    hand(p, changed(p, name="Fan Zhu", at=now + 4 * ROUND))
    assert "known in this Slack as Fan Z is named Fan Zhu there now" in p.runner.calls[3][0]
    assert [t["name"] for t in p.household.rows["U1"]["told"]] == ["fan", "Fan Zhu", "fan", "Fan Z", "Fan Zhu"]


def test_a_rename_that_timed_out_with_a_change_back_seen_meanwhile_advances(tmp_path, monkeypatch):
    def act(m, sid):
        shows(p, "U1", "fan", now + timedelta(seconds=30))
        return ends("timeout", renames())(m, sid)
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), act, renames("Fan Zhu", "fan"))
    now = changed(p)
    hand(p, now)
    assert p.household.told("U1") == "Fan Zhu" and tried(p) is None
    shows(p, "U1", "fan", now + ROUND)
    assert p.household.due(store, now + ROUND) == ("U1", "Fan Zhu", "fan")


def stop_during(p, coro):
    """Runs `coro` until its session's runner is under way, then stops it as
    a shutdown does."""
    async def go():
        t = asyncio.create_task(coro)
        while not p.runner.calls:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
    asyncio.run(go())


def test_a_rename_stopped_mid_run_and_then_set_back_is_handed_back(tmp_path, monkeypatch):
    """The session renames fan and the daemon stops; at the next start Slack
    shows fan again: the start's read advances, and the change back is
    handed by a session of its own."""
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), waits(renames()),
                                  renames("Fan Zhu", "fan"))
    now = changed(p)
    stop_during(p, p._names_session("U1", "fan", "Fan Zhu", now))
    assert tried(p)["error"] == "stopped mid-run" and tried(p)["count"] == 0
    assert store._query("SELECT status FROM runs")[0]["status"] == "cancelled"
    q = restarted(p)
    start = now + ROUND
    shows(q, "U1", "fan", start)
    asyncio.run(q.relook_names(start, start=True))
    assert q.household.told("U1") == "Fan Zhu" and q.household.awaiting("U1") == "fan"
    shows(q, "U1", "fan", start + ROUND)
    hand(q, start + ROUND)
    assert q.household.told("U1") == "fan"


def test_a_rename_whose_run_failed_and_whose_read_failed_is_handed_back_after_a_change_back(tmp_path, monkeypatch):
    def act(m, sid):
        m.rename("fan", "Fan Zhu")
        m.fail = RuntimeError("mem show took longer than 60 s")
        return ends("error")(m, sid)
    memory = Memory("fan")
    p, store, _ = names_processor(tmp_path, monkeypatch, memory, act, renames("Fan Zhu", "fan"))
    now = changed(p)
    hand(p, now)
    assert tried(p)["error"] == "memory could not be read after the session: mem show took longer than 60 s"
    assert tried(p)["count"] == 1 and len(names_alerts(p)) == 1
    # set back within the hour
    shows(p, "U1", "fan", now + timedelta(minutes=10))
    shows(p, "U1", "fan", now + timedelta(minutes=20))
    memory.fail = None
    asyncio.run(p.relook_names(now + timedelta(minutes=20)))
    assert p.household.told("U1") == "Fan Zhu" and tried(p) is None
    hand(p, now + timedelta(minutes=20))
    assert p.household.told("U1") == "fan" and len(p.runner.calls) == 2


def test_a_crash_mid_run_waits_out_its_backoff_and_is_alerted_once(tmp_path, monkeypatch):
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), renames())
    now = changed(p)
    # what a crash mid-run leaves: the try as written, with no run
    p.household.trying("U1", "Fan Zhu", "s-crashed", now)
    p.household.save(store, "U1")
    q = restarted(p)
    asyncio.run(q.relook_names(now + ROUND, start=True))
    assert tried(q)["error"] == "cut short" and tried(q)["count"] == 1
    assert names_alerts(q) == ["a change of a household member's name in Slack could not be handed to memory (1 "
                               "try); sessions go on using the earlier name; doctor says whose"]
    assert not hand(q, now + ROUND) and not hand(q, now + timedelta(minutes=59))
    # a start on a later day says nothing more
    store.set_meta("names_alert_date", "2026-01-01")
    asyncio.run(restarted(q).relook_names(now + 2 * ROUND, start=True))
    assert len(names_alerts(q)) == 1
    assert hand(q, now + timedelta(hours=1)) and q.household.told("U1") == "Fan Zhu"


def test_an_answer_that_cannot_be_read_after_a_run_ok_is_read_again(tmp_path, monkeypatch):
    """A run ok that renamed fan, whose answer cannot be read: the id is held,
    whatever Slack shows meanwhile, the alert goes once a day after the run,
    another member first seen with the name falls to their full name, and
    the next good read advances."""
    memory = Memory("fan")

    def act(m, sid):
        m.rename("fan", "Fan Zhu")
        m.fail = RuntimeError("mem show took longer than 60 s")
    p, store, _ = names_processor(tmp_path, monkeypatch, memory, act)
    store._exec("DELETE FROM meta WHERE key='names:U2'")
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    now = changed(p)
    hand(p, now)
    assert tried(p)["error"] == "mem show took longer than 60 s" and tried(p)["count"] == 0
    assert names_alerts(p) == [] and p.household.told("U1") == "fan"
    ok, line = p.household.state(store, "U1", now, p.cfg.zone)
    assert not ok and ": memory's answer could not be read (session " in line
    # Slack moves on, and a new member shows the name being handed
    changed(p, name="Fan Z", at=now + 2 * ROUND)
    shows(p, "U2", "Fan Zhu", now + 2 * ROUND, full="Mei Chen")
    assert p.household.told("U2") == "Mei Chen"
    assert not hand(p, now + 2 * ROUND)
    asyncio.run(p.relook_names(now + 2 * ROUND))
    assert names_alerts(p) == []
    day = datetime.now(timezone.utc) + timedelta(days=1, minutes=1)
    asyncio.run(p.relook_names(day))
    asyncio.run(p.relook_names(day))
    assert names_alerts(p) == ["memory's answer to a change of a household member's name in Slack has not been read "
                               "for a day after its session; sessions go on using the earlier name; doctor says "
                               "whose"]
    memory.fail = None
    asyncio.run(p.relook_names(day))
    assert p.household.told("U1") == "Fan Zhu" and tried(p) is None and len(p.runner.calls) == 1
    assert p.household.awaiting("U1") == "Fan Z"


@pytest.mark.parametrize("ending", ["cancelled", "raised"])
def test_an_ok_run_that_left_fan_alone_is_kept_whatever_ends_its_snapshot(tmp_path, monkeypatch, ending):
    """A stop during the snapshot leaves the try for the next start, which
    records the keep; a raise there records it at once. Neither is a failed
    try."""
    def snapshot(cfg, message):
        raise asyncio.CancelledError() if ending == "cancelled" else OSError("snapshots.git: no space left")
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), lambda m, sid: None)
    monkeypatch.setattr("wanda.vault.snapshot", snapshot)
    now = changed(p)
    if ending == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(p._names_session("U1", "fan", "Fan Zhu", now))
        assert tried(p)["error"] == "did not end"
        p = restarted(p)
        asyncio.run(p.relook_names(now + ROUND, start=True))
    else:
        asyncio.run(p._names_session("U1", "fan", "Fan Zhu", now))
    assert tried(p) is None and p.household.rows["U1"]["kept"]["plain"] is True
    assert names_alerts(p) == [] and p.household.told("U1") == "fan"


@pytest.mark.parametrize("renamed,read,told,failed", [
    (True, True, "Fan Zhu", False),
    (False, True, "fan", True),
    (True, False, "fan", True),
])
def test_a_stop_after_a_run_that_timed_out_is_settled_at_the_start(tmp_path, monkeypatch, renamed, read, told,
                                                                   failed):
    def snapshot(cfg, message):
        raise asyncio.CancelledError()
    memory = Memory("fan")
    p, store, _ = names_processor(tmp_path, monkeypatch, memory, ends("timeout", renames() if renamed else None))
    monkeypatch.setattr("wanda.vault.snapshot", snapshot)
    now = changed(p)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(p._names_session("U1", "fan", "Fan Zhu", now))
    memory.fail = None if read else RuntimeError("mem show took longer than 60 s")
    q = restarted(p)
    asyncio.run(q.relook_names(now + ROUND, start=True))
    assert q.household.told("U1") == told
    assert (tried(q) is not None and tried(q)["count"] == 1) is failed and bool(names_alerts(q)) is failed


@pytest.mark.parametrize("made", [True, False])
def test_a_session_stopped_mid_run_holds_its_change_until_memory_is_read(tmp_path, monkeypatch, made):
    """Memory never knew him. With Fan Zhu made by the session, the first
    good read advances; with nothing made, it is a stop, due at once."""
    memory = Memory()
    p, store, _ = names_processor(tmp_path, monkeypatch, memory, waits(makes() if made else None))
    now = changed(p)
    stop_during(p, p._names_session("U1", "fan", "Fan Zhu", now))
    assert tried(p)["error"] == "stopped mid-run"
    memory.fail = RuntimeError("mem show took longer than 60 s")
    q = restarted(p)
    asyncio.run(q.relook_names(now + ROUND, start=True))
    assert tried(q)["error"] == "stopped mid-run" and not hand(q, now + ROUND)
    ok, line = q.household.state(store, "U1", now + ROUND, q.cfg.zone)
    assert not ok and ": stopped mid-run (session " in line and "memory is read again at each refresh" in line
    day = datetime.now(timezone.utc) + timedelta(days=1, minutes=1)
    asyncio.run(q.relook_names(day))
    asyncio.run(q.relook_names(day))
    assert len(names_alerts(q)) == 1
    memory.fail = None
    asyncio.run(q.relook_names(day))
    if made:
        assert q.household.told("U1") == "Fan Zhu" and tried(q) is None
    else:
        assert q.household.told("U1") == "fan" and tried(q)["error"] == "stopped" and tried(q)["count"] == 0
        assert q.household.due(store, day) == ("U1", "fan", "Fan Zhu")


def test_a_refresh_during_a_names_session_leaves_its_try_to_it(tmp_path, monkeypatch, caplog):
    """During the run and during the session's own reads after it: the round
    reads nothing for that id, logs nothing, and the session's own settle
    advances once."""
    import logging
    memory = Memory("fan")
    running = asyncio.Event()

    async def act(m, sid):
        m.rename("fan", "Fan Zhu")
        running.set()
        await asyncio.sleep(0.05)
    p, store, _ = names_processor(tmp_path, monkeypatch, memory, act)
    now = changed(p)

    async def go():
        session = asyncio.create_task(p._names_session("U1", "fan", "Fan Zhu", now))
        await running.wait()
        await p.relook_names(now)
        memory.hold = asyncio.Event()
        while not memory.reads:
            await asyncio.sleep(0.01)
        await p.relook_names(now)
        memory.hold.set()
        await session
    with caplog.at_level(logging.INFO, logger="wanda"):
        asyncio.run(go())
    assert memory.reads == ["fan", "Fan Zhu"]
    assert not [r for r in caplog.records if "names:" in r.getMessage() and "U1 is Fan Zhu" not in r.getMessage()
                and "telling memory" not in r.getMessage()]
    assert [t["name"] for t in p.household.rows["U1"]["told"]] == ["fan", "Fan Zhu"]


def test_a_try_refused_while_a_relook_reads_keeps_what_it_wrote(tmp_path, monkeypatch):
    memory = Memory("fan")
    p, store, _ = names_processor(tmp_path, monkeypatch, memory)
    now = changed(p)
    p.household.trying("U1", "Fan Zhu", "s-0", now)
    p.household.stopped("U1", now)

    async def refused(reserve_usd=0.0):
        return "busy"

    async def go():
        memory.hold = asyncio.Event()
        relook = asyncio.create_task(p.relook_names(now))
        while not memory.reads:
            await asyncio.sleep(0.01)
        p.check_budget = refused
        await p._names_session("U1", "fan", "Fan Zhu", now)
        # Slack moves on, so the read the re-look began, applied, would end
        # the try it was not read for
        shows(p, "U1", "Fan Z", now + ROUND)
        memory.hold.set()
        await relook
    asyncio.run(go())
    assert tried(p) is not None and tried(p)["error"] == "busy" and tried(p)["session"] != "s-0"


def test_a_keep_ended_while_a_relook_reads_is_not_settled_by_that_read(tmp_path, monkeypatch):
    memory = Memory("fan")
    p, store, _ = names_processor(tmp_path, monkeypatch, memory)
    now = changed(p)
    p.household.keep("U1", "Fan Zhu", "s-1", "kept", True, now)
    p.household.save(store, "U1")
    memory.rename("fan", "Fan Zhu")

    async def go():
        memory.hold = asyncio.Event()
        relook = asyncio.create_task(p.relook_names(now))
        while not memory.reads:
            await asyncio.sleep(0.01)
        # Slack shows fan again in two reads a round apart, which ends the
        # keep, so the read the re-look began, applied, would advance a
        # change the member has taken back
        shows(p, "U1", "fan", now + ROUND)
        shows(p, "U1", "fan", now + 2 * ROUND)
        assert p.household.rows["U1"]["kept"] is None
        memory.hold.set()
        await relook
    asyncio.run(go())
    assert p.household.told("U1") == "fan"


def test_a_try_slack_moved_on_from_ends_at_a_relook(tmp_path, monkeypatch):
    """Memory did not take it, and there is nothing left to hand."""
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"))
    now = changed(p)
    p.household.trying("U1", "Fan Zhu", "s-0", now)
    p.household.stopped("U1", now)
    shows(p, "U1", "Fan Z", now + ROUND)
    asyncio.run(p.relook_names(now + ROUND))
    assert tried(p) is None and p.household.told("U1") == "fan"


def test_each_round_looks_again_at_a_try(tmp_path, monkeypatch):
    """An ok run whose answer could not be read is settled by the refresh's
    next round."""
    monkeypatch.setattr("wanda.main.NAMES_EVERY_S", 0.01)
    memory = Memory("fan")
    memory.rename("fan", "Fan Zhu")
    p, store, _ = names_processor(tmp_path, monkeypatch, memory)
    now = changed(p)
    p.household.trying("U1", "Fan Zhu", "s-ok", now)
    p.household.unanswered("U1", "mem show took longer than 60 s")
    ran(store, "s-ok")

    async def user_now(uid):
        return {"profile": {"display_name": "Fan Zhu" if uid == "U1" else "mei"}}
    p.slack.user_now = user_now

    async def go():
        names = asyncio.create_task(p.names_loop())
        try:
            while p.household.told("U1") != "Fan Zhu":
                await asyncio.sleep(0.01)
        finally:
            names.cancel()
    asyncio.run(asyncio.wait_for(go(), 3))


def test_a_relook_that_raises_holds_back_no_other(tmp_path, monkeypatch, caplog):
    import logging
    memory = Memory("fan", "mei")
    p, store, _ = names_processor(tmp_path, monkeypatch, memory)
    now = changed(p)
    changed(p, "U2", "Mei Chen", now)
    for uid, name in (("U1", "Fan Zhu"), ("U2", "Mei Chen")):
        p.household.keep(uid, name, f"s-{uid}", "kept", True, now)
    memory.rename("mei", "Mei Chen")
    real = p._read_both

    async def read_both(old, new, at):
        if old == "fan":
            raise ValueError("a row with no shape")
        return await real(old, new, at)
    p._read_both = read_both
    with caplog.at_level(logging.WARNING, logger="wanda"):
        asyncio.run(p.relook_names(now))
    assert "names: looking again at U1's change failed" in [r.getMessage() for r in caplog.records]
    assert p.household.told("U2") == "Mei Chen"


@pytest.mark.parametrize("first", ["refused", "stopped"])
def test_one_ids_try_is_never_settled_by_anothers_run(tmp_path, monkeypatch, first):
    """U1's try refused by the run cap, or stopped before its run; then U2's
    change, listed first and due by then, is handed and recorded ok on the
    same task, before the next round or at the next start. Nothing settles
    U1's try from that run, and U1 gets a session of its own when it is
    due."""
    memory = Memory("fan", "mei")
    p, store, _ = names_processor(tmp_path, monkeypatch, memory, renames("mei", "Mei Chen"), renames())
    monkeypatch.setattr(p.cfg, "slack_owner_user_ids", ["U2", "U1"])
    p.household.allowed = ["U2", "U1"]
    now = changed(p)
    shows(p, "U2", "Mei Chen", now)
    if first == "refused":
        real = p.check_budget

        async def refused(reserve_usd=0.0):
            return "busy"
        p.check_budget = refused
        asyncio.run(p._names_session("U1", "fan", "Fan Zhu", now))
        p.check_budget = real
    else:
        async def held():
            # both slots taken, so the session waits for one and is stopped there
            await p.runner.agent_sem.acquire()
            await p.runner.agent_sem.acquire()
            t = asyncio.create_task(p._names_session("U1", "fan", "Fan Zhu", now))
            await asyncio.sleep(0.05)
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        asyncio.run(held())
        p = restarted(p)
    assert store._query("SELECT * FROM runs") == []
    later = now + ROUND
    shows(p, "U2", "Mei Chen", later)
    asyncio.run(p.relook_names(later, start=first == "stopped"))
    assert hand(p, later) and p.household.told("U2") == "Mei Chen"
    asyncio.run(p.relook_names(later))
    assert tried(p)["error"] == ("busy" if first == "refused" else "stopped") and p.household.told("U1") == "fan"
    assert names_alerts(p) == [] and memory.people["person:000001"]["name"] == "fan"
    due = now + timedelta(hours=1) if first == "refused" else later
    assert first == "stopped" or not hand(p, due - timedelta(seconds=1))
    assert hand(p, due) and p.household.told("U1") == "Fan Zhu"


def test_an_outcome_is_stamped_with_when_it_is_applied(tmp_path, monkeypatch):
    """Not with the tick's time, which is a session old by then."""
    real, ahead = time.monotonic, [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: real() + ahead[0])

    def act(m, sid):
        m.rename("fan", "Fan Zhu")
        ahead[0] = 300.0
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), act)
    now = changed(p)
    hand(p, now)
    since = datetime.fromisoformat(p.household.rows["U1"]["told"][-1]["since"])
    assert timedelta(seconds=300) <= since - now < timedelta(seconds=310)


def test_memory_is_read_on_the_households_date_whatever_time_it_is_given(tmp_path, monkeypatch):
    p, store = make(tmp_path, tz="America/Los_Angeles")
    env = {}

    async def spawn(*args, **kw):
        env.update(kw["env"])
        raise RuntimeError("no mem here")
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    # 20:00 on the 3rd in Los Angeles
    with pytest.raises(RuntimeError):
        asyncio.run(p._mem_call(datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc), "show", "person:fan"))
    assert (env["MEM_DATE"], env["MEM_UTC_OFFSET"]) == ("2026-10-03", str(-7 * 3600))


def test_a_refused_names_session_takes_no_other_sessions_run(tmp_path, monkeypatch):
    """It finds its run by its own session id, not as the newest in the
    store."""
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"))
    ran(store, "s-message")

    async def refused(reserve_usd=0.0):
        return "busy"
    p.check_budget = refused
    now = changed(p)
    asyncio.run(p._names_session("U1", "fan", "Fan Zhu", now))
    assert tried(p)["error"] == "busy" and p.household.rows["U1"]["kept"] is None


def test_a_failed_try_backs_off_and_is_alerted_once_a_day(tmp_path, monkeypatch):
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), ends("error"), ends("error"))
    now = changed(p)
    hand(p, now)
    assert (tried(p)["count"], tried(p)["error"]) == (1, "claude reported an error")
    assert not hand(p, now + timedelta(minutes=59))
    hand(p, now + timedelta(hours=1))
    assert tried(p)["count"] == 2
    assert datetime.fromisoformat(tried(p)["next"]) == now + timedelta(hours=3)
    assert names_alerts(p) == ["a change of a household member's name in Slack could not be handed to memory (1 "
                               "try); sessions go on using the earlier name; doctor says whose"]
    ok, line = p.household.state(store, "U1", now + timedelta(hours=1), p.cfg.zone)
    assert not ok and ": 2 tries, the last at " in line


def test_a_names_alert_slack_refused_is_posted_by_a_later_pass(tmp_path, monkeypatch):
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory("fan"), ends("error"))
    now = changed(p)
    real = p.slack.alert

    async def refused(text):
        raise RuntimeError("no network")
    p.slack.alert = refused
    hand(p, now)
    assert names_alerts(p) == []
    p.slack.alert = real

    async def nothing():
        return None
    p._housekeep = nothing
    asyncio.run(p.drain_mail())
    assert len(names_alerts(p)) == 1


def test_a_refusal_waits_an_hour_and_is_no_failure(tmp_path, monkeypatch):
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory(), renames())

    async def refused(reserve_usd=0.0):
        return "busy"
    p.check_budget = refused
    now = changed(p)
    hand(p, now)
    assert (tried(p)["error"], tried(p)["count"]) == ("busy", 0)
    assert datetime.fromisoformat(tried(p)["next"]) == now + timedelta(hours=1)
    q = restarted(p)
    asyncio.run(q.relook_names(now + ROUND, start=True))
    assert names_alerts(q) == [] and tried(q)["error"] == "busy"
    # memory holding neither name: not taken for an advance, which needs a run
    assert q.household.told("U1") == "fan"
    assert not hand(q, now + timedelta(minutes=59))
    assert hand(q, now + timedelta(hours=1)) and len(q.runner.calls) == 1


def test_a_stop_before_the_model_ran_is_due_again_at_once(tmp_path, monkeypatch):
    p, store, _ = names_processor(tmp_path, monkeypatch, Memory())
    now = changed(p)

    async def stopped():
        await p.runner.agent_sem.acquire()
        await p.runner.agent_sem.acquire()
        t = asyncio.create_task(p._names_session("U1", "fan", "Fan Zhu", now))
        await asyncio.sleep(0.05)
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
    asyncio.run(stopped())
    assert (tried(p)["error"], tried(p)["count"], tried(p)["next"]) == ("stopped", 0, now.isoformat())
    q = restarted(p)
    asyncio.run(q.relook_names(now + ROUND, start=True))
    assert q.household.told("U1") == "fan" and names_alerts(q) == []
    assert hand(q, now + ROUND)


def test_a_kept_change_is_looked_at_again_each_round(tmp_path, monkeypatch):
    """Renamed later by a message session, it advances. An alerted keep whose
    second person is forgotten ends, and the change is handed again; a
    plain keep beside a second person a message session then makes is
    alerted, once."""
    memory = Memory("fan")
    p, store, _ = names_processor(tmp_path, monkeypatch, memory, lambda m, sid: None, makes("Fan Z"),
                                  lambda m, sid: None)
    now = changed(p)
    hand(p, now)
    assert p.household.rows["U1"]["kept"]["plain"] is True
    memory.rename("fan", "Fan Zhu")
    asyncio.run(p.relook_names(now + ROUND))
    assert p.household.told("U1") == "Fan Zhu"
    # an alerted keep, its stray forgotten
    now = changed(p, name="Fan Z", at=now + 3 * ROUND)
    hand(p, now)
    kept = p.household.rows["U1"]["kept"]
    assert kept["plain"] is False
    asyncio.run(p._flush_names())
    stray = next(pid for pid, x in memory.people.items() if x["name"] == "Fan Z")
    memory.forget(stray)
    asyncio.run(p.relook_names(now + ROUND))
    assert p.household.rows["U1"]["kept"] is None and p.household.awaiting("U1") == "Fan Z"
    hand(p, now + ROUND)
    assert len(p.runner.calls) == 3 and p.household.rows["U1"]["kept"]["plain"] is True
    memory.make("Fan Z", "s-message")
    asyncio.run(p.relook_names(now + 2 * ROUND))
    asyncio.run(p.relook_names(now + 3 * ROUND))
    assert p.household.rows["U1"]["kept"]["plain"] is False
    asyncio.run(p._flush_names())
    asyncio.run(p._flush_names())
    assert len(p.slack.alerts) == 2


def test_a_message_session_outlasting_a_names_session_keeps_its_opening_names(tmp_path, monkeypatch):
    """On two slots, a names session that a message session's first step
    outlasts: what is added to the message session is framed with the name
    its opening frame used, whatever memory has since taken."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[3.0, 0.2])
    p.runner.agent_sem = asyncio.Semaphore(2)
    memory = Memory("fan")
    memory.rename("fan", "Fan Zhu")
    p._mem_call = memory.call
    now = changed(p)

    async def go():
        first = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "can you remind me at 5")))
        await moment(("tool_use", 1, 0.1), p.cfg.vault_dir)
        # the names session, started now, takes one quick step
        monkeypatch.setenv("STANDIN", json.dumps({"steps": [0.1]}))
        await p._names_session("U1", "fan", "Fan Zhu", now)
        added = asyncio.create_task(p.handle_slack(dm(f"{AT + 30:.1f}", "to call the plumber")))
        await asyncio.gather(first, added)
    asyncio.run(go())
    assert p.household.told("U1") == "Fan Zhu"
    assert [h for h in handed_texts(tmp_path) if h] == [[vault.added_text("dm", "fan", "to call the plumber",
                                                                           "16:40")]]


def test_the_start_settles_a_try_before_any_session(tmp_path, monkeypatch):
    """A run recorded ok whose outcome was never read, left by a stop: the
    daemon's start reads memory and advances before Slack connects."""
    store = Store(tmp_path / "wanda.db")
    told(store)
    store.set_meta("vault_since", "2026-09-01")
    h = Household.load(store, ["U1", "U2"])
    now = datetime.now(timezone.utc)
    h.trying("U1", "Fan Zhu", "s-stopped", now - ROUND)
    h.save(store, "U1")
    store.record_run(kind="agent", task_id=None, session_id="s-stopped", started_at=utcnow(), exit_code=0,
                     cost_usd=0.0, status="ok")
    store.close()
    memory = Memory("fan")
    memory.rename("fan", "Fan Zhu")
    monkeypatch.setattr("wanda.main.Processor._mem_call", lambda self, now, *args: memory.call(now, *args))
    c, _, _ = daemon(tmp_path, monkeypatch, {"U1": {"profile": {"display_name": "Fan Zhu"}}, "U2": MEI})
    h = names_in(c)
    assert h.told("U1") == "Fan Zhu" and h.rows["U1"]["tried"] is None


@pytest.mark.skipif(not os.environ.get("TEST_MEM_BIN"), reason="TEST_MEM_BIN names a mem build")
@pytest.mark.parametrize("does,told,kept", [
    (("rename", "person:fan", "Fan Zhu"), "Fan Zhu", None),
    (("entity", "--kind", "person", "--name", "Fan Zhu", "--new"), "fan", False),
    (("entity", "--kind", "person", "--name", "Fan Zhu", "--summary", "his cousin", "--new"), "fan", False),
    (("show", "person:fan"), "fan", True),
])
def test_memorys_own_answers_settle_a_change(tmp_path, monkeypatch, does, told, kept):
    """Through the build: the session's `mem` call, and the daemon's reads of
    memory after it, give the name sessions are told. fan was made by an
    earlier session; a second person Fan Zhu beside him is alerted."""
    root = Path(__file__).resolve().parent.parent
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "mem").symlink_to(os.environ["TEST_MEM_BIN"])
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("MEM_TEMPLATES", str(root / "memory" / "templates"))

    def mem(sid, *args):
        return subprocess.run(["mem", *args], env=vault.session_env(p.cfg, sid, now), capture_output=True,
                              text=True, check=True).stdout
    p, store, _ = memory_processor(tmp_path, ConversationSlack(), Naming(None, lambda m, sid: mem(sid, *does)),
                                   monkeypatch)
    now = changed(p)
    mem("s-earlier", "entity", "--kind", "person", "--name", "fan", "--summary", "the member")
    asyncio.run(p._names_session("U1", "fan", "Fan Zhu", now))
    k = p.household.rows["U1"]["kept"]
    assert p.household.told("U1") == told and (k["plain"] if k else None) == kept and tried(p) is None
    if kept is False:
        assert re.fullmatch(r"person:Fan Zhu finds person:[0-9a-f]{6} \(Fan Zhu\); person:fan finds person:"
                            r"[0-9a-f]{6} \(fan\)", k["memory"]), k["memory"]
