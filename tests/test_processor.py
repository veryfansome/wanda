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

import pytest
from slack_sdk.errors import SlackApiError

from wanda.config import Config
from wanda.events import Event
from wanda import main, vault
from wanda.household import NAMES_EVERY_S, Household
from wanda.main import ANCHOR, BUDGET_REPLIES, MAX_APPLY_ATTEMPTS, RETRY_BASE_S, Additions, Processor
from wanda.runner import RunResult, RunnerService
from wanda.store import Store, utcnow
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
        def __init__(self, **kw):
            self.socket_mode_request_listeners = []

        def connect(self):
            pass

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
    monkeypatch.setattr(slack_watcher, "SocketModeClient", Socket)
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
    """A DM in which fan asked something a minute ago and wanda answered."""

    def __init__(self, members=None, workspace=None, history=None, **kw):
        super().__init__(**kw)
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

    async def fetch_context(self, channel, thread_ts, since, counted=None):
        self.fetched.append((channel, thread_ts, since, counted))
        return list(self.history)

    def kept(self, ids):
        return {u: self.held[u] for u in ids if u in self.held}

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
    or ends as a RunResult it is given."""

    def __init__(self, *reports, ok=True):
        self.agent_sem = asyncio.Semaphore(2)
        self.calls = []
        self.reports = list(reports)
        self.ok = ok

    async def run(self, prompt, **kw):
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


def memory_processor(tmp_path, slack, runner, monkeypatch, snapshot=None):
    p, store = make(tmp_path, slack, data_dir=tmp_path, slack_owner_user_ids="U1,U2", tz="America/Los_Angeles")
    told(store)
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    p.runner = runner
    snaps = []
    monkeypatch.setattr("wanda.vault.snapshot", snapshot or (lambda cfg, message: snaps.append(message)))
    return p, store, snaps


def answer(text):
    return {"recalled": ["person:aaaaaa"], "answer": text, "recorded": ["event:x"]}


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


@pytest.mark.parametrize("retry,why,said", [
    (RunResult(ok=False, timed_out=True, error="timed out after 420s"), "timeout", "timed out after 420s"),
    (said_by_claude("You've hit your limit · resets 5pm", api_error="rate_limit"), "usage limit",
     "Claude Code said: You've hit your limit · resets 5pm"),
], ids=["out of time", "refused"])
def test_a_retry_that_fails_otherwise_than_its_first_try_is_alerted_under_its_own_class(tmp_path, monkeypatch,
                                                                                       retry, why, said):
    """A retry that ran out of time, or that Claude Code refused, is named by
    itself, in its own words, under its own class, where a token to renew or
    a limit is read; the first try it retried is named by its run."""
    runner = RecordingRunner("I filed it.", retry)
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert len(runner.calls) == 2 and slack.replies == [main.FAILED]
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert (failed["id"], failed["why"], failed["said"], failed["then"]) == (
        2, why, said, "the retry of run 1, a note asked for it again")


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
    (said_by_claude("You've hit your limit · resets 5pm", api_error="rate_limit"), "refused", "usage limit"),
    (said_by_claude("Usage limit reached ∙ resets at 5pm"), "refused", "usage limit"),
    (said_by_claude("Login expired · Please run /login"), "refused", "authentication"),
    (said_by_claude("OAuth token revoked · Please run /login"), "refused", "authentication"),
], ids=["a timeout", "its budget spent", "an output it cannot read", "rate_limit", "Usage limit reached",
        "Login expired", "OAuth token revoked"])
def test_a_failure_a_second_session_would_meet_gets_her_note_at_once(tmp_path, monkeypatch, ended, status, why):
    """Not tried once more; a session Claude Code refused, which ran
    nothing, and her note count toward no daily cap."""
    runner = RecordingRunner(ended, answer("Yes."))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert len(runner.calls) == 1 and slack.replies == [main.FAILED]
    assert [r[:2] for r in failed_runs(store)] == [("agent", status), ("note", "ok")]
    assert store.runs_today()[0] == (0 if status == "refused" else 1)
    [failed] = json.loads(store.get_meta("failed_runs"))
    assert failed["why"] == why and failed["then"] == "not tried again, a note asked for it again"


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
    monkeypatch.setattr(store, "create_task", full)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "dinner at 7?", channel_type="mpim")))
    assert slack.replies == [main.FAILED_GROUP] and store._query("SELECT * FROM runs") == []
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


def test_a_failed_turn_before_one_that_answers_is_not_run_again(tmp_path, monkeypatch):
    """A later turn of the session answered after it, and saw what it was
    begun by; a turn a background command's notice began then failed, which
    is logged and alerted alone."""
    def result(out=None):
        return ({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(out),
                 "structured_output": out} if out else
                {"type": "result", "subtype": "error_during_execution", "is_error": True})
    results = [result(answer("Noted.")), result(), result(answer("And the plumber's at 5.")), result()]
    monkeypatch.setattr("wanda.vault.turn_starts", lambda v, sid: [
        vault.Turn(True, []), vault.Turn(True, ["two"]), vault.Turn(True, ["three"]), vault.Turn(False, [])])
    runner = RecordingRunner(RunResult(ok=False, envelope=results[-1], error="error_during_execution",
                                       results=results))
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "one")))
    assert len(runner.calls) == 1 and slack.replies == ["And the plumber's at 5."]
    assert failed_runs(store) == [("agent", "ok", "error_during_execution")] and p._waiting[1] == []
    assert json.loads(store.get_meta("failed_runs"))[0]["then"] == "not tried again, no note"

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
    # a refusal's reply that Slack refuses leaves the budget's verdict
    store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                     cost_usd=p.cfg.daily_cost_cap_usd, status="ok")
    assert asyncio.run(p.memory_turn(task, "x", now, channel="D1", reply_thread=None, owed=True)) == "breaker"
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
    """Holds each session open until released, counting how many run at once."""

    def __init__(self, *reports):
        super().__init__(*reports)
        self.release = asyncio.Event()
        self.running = self.most = 0

    async def run(self, prompt, **kw):
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


def test_a_refusal_answers_every_message_waiting(tmp_path, monkeypatch):
    """A turn the budget refuses posts its one reply for all the messages it
    would have taken, as a session would have taken them all."""
    runner = Held()
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)

    async def go():
        first = asyncio.create_task(p.handle_slack(dm(f"{AT:.1f}", "one")))
        await asyncio.sleep(0.05)
        rest = [asyncio.create_task(p.handle_slack(dm(f"{AT + i:.1f}", text)))
                for i, text in ((10, "two"), (20, "three"))]
        await asyncio.sleep(0.05)
        store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                         cost_usd=p.cfg.daily_cost_cap_usd, status="ok")
        runner.release.set()
        await asyncio.gather(first, *rest)
    asyncio.run(go())
    assert len(runner.calls) == 1 and slack.replies == [BUDGET_REPLIES["breaker"]]


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


def test_a_restart_leaves_one_notice_per_conversation(tmp_path, monkeypatch):
    runner = Held()
    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)

    async def go():
        for i in range(3):
            t = asyncio.create_task(p.handle_slack(dm(f"{AT + i:.1f}", f"line {i}")))
            p._bg.add(t)
            await asyncio.sleep(0.02)
        await p.shutdown(grace_s=1)
    asyncio.run(go())
    rows = store._query("SELECT status, notified FROM runs")
    assert [r["notified"] for r in rows].count(0) == 1, [dict(r) for r in rows]
    asyncio.run(p.deliver_pending())
    assert slack.replies == ["⏸ I restarted while working on this — reply again to retry."]


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


def test_a_restart_while_a_session_holds_an_addition_leaves_one_notice(tmp_path, monkeypatch):
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[30])

    async def go():
        for at, ev in ((0, dm(f"{AT:.1f}", "one")), (("tool_use", 1, 0.3), dm(f"{AT + 10:.1f}", "two"))):
            await moment(at, p.cfg.vault_dir)
            t = asyncio.create_task(p.handle_slack(ev))
            p._bg.add(t)
        await asyncio.sleep(0.5)
        taken.extend(len(more.taken) for more in p._additions.values())
        await p.shutdown(grace_s=5)
    taken = []
    asyncio.run(go())
    assert taken == [1]  # written to the session's input when the restart came
    rows = store._query("SELECT status, notified FROM runs")
    assert [r["notified"] for r in rows].count(0) == 1, [dict(r) for r in rows]
    asyncio.run(p.deliver_pending())
    assert slack.replies == ["⏸ I restarted while working on this — reply again to retry."]


def test_a_restart_during_a_further_turn_delivers_the_answer_already_given(tmp_path, monkeypatch):
    """The first turn has answered, and a follow-up written after its last
    step runs a further turn, when the daemon stops: the answer is the run's,
    delivered at the next start, and the conversation's one restart notice
    is left for the follow-up."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.0, steps_later=[30])

    async def go():
        for at, ev in ((0, dm(f"{AT:.1f}", "can you remind me at 5")),
                       (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber"))):
            await moment(at, p.cfg.vault_dir)
            t = asyncio.create_task(p.handle_slack(ev))
            p._bg.add(t)
        # the further turn's tool call has begun
        await moment(("tool_use", 2, 0.2), p.cfg.vault_dir)
        await p.shutdown(grace_s=5)
    asyncio.run(go())
    rows = [dict(r) for r in store._query("SELECT status, notified, result_text FROM runs")]
    assert {"status": "ok", "notified": 0, "result_text": "one answer to 1: can you remind me at 5"} in rows, rows
    asyncio.run(p.deliver_pending())
    assert sorted(slack.replies) == ["one answer to 1: can you remind me at 5",
                                     "⏸ I restarted while working on this — reply again to retry."]


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


def test_a_session_claude_code_refused_gets_her_note_at_once(tmp_path, monkeypatch):
    """Known by the error its streamed output gives."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, refuse={
        "turn": "first", "error": "rate_limit", "said": "You've hit your limit · resets 5pm"})
    conversation(p, (0, dm(f"{AT:.1f}", "hello")))
    assert slack.replies == [main.FAILED]
    assert failed_runs(store) == [("agent", "refused", "You've hit your limit · resets 5pm"), ("note", "ok", None)]
    assert store.runs_today()[0] == 0


def test_a_clock_session_claude_code_refused_is_known_by_its_words(tmp_path, monkeypatch):
    """Run with --output-format json, it prints no assistant event."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, refuse={
        "turn": "first", "error": None, "said": "Login expired · Please run /login"})
    store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")
    got = asyncio.run(p.memory_turn(task, "It is 17:00, as asked:\n\n    call the plumber",
                                    datetime.fromtimestamp(AT, p.cfg.zone), channel="D1", reply_thread=None,
                                    owed=False))
    assert got == "Login expired · Please run /login" and slack.replies == []
    assert failed_runs(store) == [("agent", "refused", "Login expired · Please run /login")]
    assert store.runs_today()[0] == 0


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


def test_a_later_turn_claude_code_refused_gets_her_note_after_the_answer(tmp_path, monkeypatch):
    """A second session would meet the same refusal: nothing is run again."""
    p, store, _, slack = standin_processor(tmp_path, monkeypatch, steps=[0.2], reply_s=1.5, refuse={
        "turn": "later", "error": "rate_limit", "said": "You've hit your limit · resets 5pm"})
    conversation(p, (0, dm(f"{AT:.1f}", "can you remind me at 5")),
                 (("tool_result", 1, 0.2), dm(f"{AT + 30:.1f}", "to call the plumber")))
    assert slack.replies == ["one answer to 1: can you remind me at 5", main.FAILED_REST]
    assert failed_runs(store) == [("agent", "ok", "You've hit your limit · resets 5pm"), ("note", "ok", None)]


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


def test_messages_queued_at_a_stop_where_someone_outside_reads_leave_one_notice(tmp_path, monkeypatch):
    """Whether they are still on the queue or waiting for a session's slot
    when the stop cancels them."""
    p, store, _ = memory_processor(tmp_path, ConversationSlack(members=["U1", "U3", "UBOT"]), RecordingRunner(),
                                   monkeypatch)
    for i in range(2):
        p.slack_queue.put_nowait(dm(f"{AT + i:.1f}", "dinner at 7?", channel_type="mpim", channel="G1"))
    asyncio.run(p.shutdown(grace_s=1))
    owed = "SELECT t.slack_channel FROM runs r JOIN tasks t ON t.id = r.task_id WHERE r.notified=0"
    assert [r["slack_channel"] for r in store._query(owed)] == ["G1"]
    slack = ConversationSlack(members=["U1", "U3", "UBOT"], history=[])
    p, store, _, meis = one_slot_behind_a_dm(slack, tmp_path / "waiting", monkeypatch)

    async def go():
        for ev in (meis, dm(f"{AT:.1f}", "dinner at 7?", channel_type="mpim", channel="G1"),
                   dm(f"{AT + 1:.1f}", "or 8", channel_type="mpim", channel="G1")):
            t = asyncio.create_task(p.handle_slack(ev))
            p._bg.add(t)
            await asyncio.sleep(0.05)
        await p.shutdown(grace_s=1)
    asyncio.run(go())
    assert sorted(r["slack_channel"] for r in store._query(owed)) == ["D2", "G1"]


def test_a_queued_message_from_an_id_not_let_in_gets_no_restart_notice(tmp_path, monkeypatch):
    p, store, _ = memory_processor(tmp_path, ConversationSlack(), RecordingRunner(), monkeypatch)
    store._exec("DELETE FROM meta WHERE key='names:U2'")
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    for ev in (dm(f"{AT:.1f}", "hello?", channel="D2", user="U2"), dm(f"{AT:.1f}", "hello?")):
        p.slack_queue.put_nowait(ev)
    asyncio.run(p.shutdown(grace_s=1))
    owed = store._query("SELECT t.slack_channel FROM runs r JOIN tasks t ON t.id = r.task_id WHERE r.notified=0")
    assert [r["slack_channel"] for r in owed] == ["D1"]


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
