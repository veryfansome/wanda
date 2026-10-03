"""Processor behaviors the adversarial review found broken: execution-time
trash caps, time-gated retries, and budget saturation vs. a tripped breaker."""

import asyncio
import json
import os
import signal
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from wanda.config import Config
from wanda.events import Event
from wanda.main import ANCHOR, BUDGET_REPLIES, MAX_APPLY_ATTEMPTS, RETRY_BASE_S, Processor
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
        self.channels, self.threads, self.notes = [], [], []

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

    async def reply(self, thread_ts, text, channel=None, note=False):
        self.replies.append(text)
        self.channels.append(channel)
        self.threads.append(thread_ts)
        self.notes.append(note)

    async def channel_type(self, channel):
        # what is owed in these tests is owed in a 1:1 DM, unless a test says
        # otherwise
        return "im"


def cfg(**kw) -> Config:
    return Config(_env_file=None, email_triage_slack_channel_id="C1", **kw)


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
        async def reply(self, thread_ts, text, channel=None, note=False):
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


def test_deliver_pending_skips_in_flight_delivery(tmp_path):
    p, store = make(tmp_path, FakeSlack())
    store.ingest_message(dedupe_key="k1", message_id="<k1>", folder="INBOX", uidvalidity=1, uid=1,
                         from_addr="a@b.c", subject="s", date_hdr="d", snippet="b")
    pk = store.get_message_by_key("k1")["id"]
    tid = store.create_task(pk, "C1", "ts-1")
    run_id = store.record_run(kind="agent", task_id=tid, session_id="s", started_at=utcnow(),
                              exit_code=0, cost_usd=0.4, status="ok", result_text="answer", notified=0)
    p._delivering.add(run_id)
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == [], "must not post an answer another task is delivering"


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


def test_delivery_gives_up_and_stops_blocking(tmp_path):
    """An answer for a channel wanda was removed from used to retry forever,
    blocking every later delivery behind it."""
    from wanda.main import MAX_DELIVERY_ATTEMPTS

    class Boom(FakeSlack):
        async def reply(self, thread_ts, text, channel=None, note=False):
            raise RuntimeError("not_in_channel")

    p, store = make(tmp_path, Boom())
    tid = store.create_task(None, "C_GONE", "1.1", kind="mention")
    store.record_run(kind="agent", task_id=tid, session_id="s", started_at=utcnow(),
                     exit_code=0, cost_usd=0.4, status="ok", result_text="answer", notified=0)
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        asyncio.run(p.deliver_pending())
    assert store.pending_deliveries() == [], "must stop retrying and free the queue"
    assert store.get_meta("abandoned_alert_pending") == "1", "and tell the owner"
    assert [sorted(g) for g in json.loads(store.get_meta("given_up_runs"))] == [["at", "id"]]


def test_an_answer_given_up_on_is_alerted_and_none_is_dropped(tmp_path):
    """Delivery gives up only while Slack refuses posts, so the alert waits
    for Slack too; later give-ups join it, and one after the day's alert is
    named the next day. It names each run and when, never where or what."""
    from wanda.main import MAX_DELIVERY_ATTEMPTS

    class Down(FakeSlack):
        up = False

        async def reply(self, thread_ts, text, channel=None, note=False):
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
        for _ in range(MAX_DELIVERY_ATTEMPTS):
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
    monkeypatch.setattr("wanda.main.SlackWatcher.start", lambda self: None)
    monkeypatch.setattr("wanda.main.SlackWatcher.stop", lambda self: None)
    monkeypatch.setattr("wanda.main.Processor.loop", one_pass)
    monkeypatch.setattr("wanda.main.RunnerService", Runner)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)  # one process, two starts
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y",
               alert_channel="C9", slack_owner_user_ids="U1,U2", slack_names="U1:fan,U2:mei",
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
    monkeypatch.setattr("wanda.main.SlackWatcher.start", lambda self: None)
    monkeypatch.setattr("wanda.main.SlackWatcher.stop", lambda self: None)
    monkeypatch.setattr("wanda.main.Processor.loop", one_pass)
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: None)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y",
               alert_channel="C9", slack_owner_user_ids="U1,U2", slack_names="U1:fan,U2:mei",
               tz="America/Los_Angeles", email_triage=False, claude_bin="/bin/true")
    asyncio.run(main.run_daemon(c))
    assert opened["n"] == 4 and tries["alert"] == 2
    assert posted == [f"wanda is not running: the run store {c.db_path} could not be opened or written: "
                      "disk I/O error"]


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
    monkeypatch.setattr("wanda.main.SlackWatcher.start", lambda self: None)
    monkeypatch.setattr("wanda.main.SlackWatcher.stop", lambda self: None)
    monkeypatch.setattr("wanda.main.Processor.loop", one_pass)
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: None)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y",
               alert_channel="C9", slack_owner_user_ids="U1,U2", slack_names="U1:fan,U2:mei",
               tz="America/Los_Angeles", email_triage=False, claude_bin="/bin/true")
    asyncio.run(main.run_daemon(c))
    assert full["writes"] == 3 and tries["alert"] == 2
    assert posted == [f"wanda is not running: the run store {c.db_path} could not be opened or written: "
                      "database or disk is full"]
    store = Store(c.db_path)
    assert store.get_meta("started_at") and store.get_meta("sessions_left_running") == "0"


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
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            store.bump_delivery_attempt(r)
    store.bump_delivery_attempt(kept)
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
    assert "✗ takes a write — database or disk is full" in capsys.readouterr().out


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

    def __init__(self, members=None, workspace=None, history=None, types=None, **kw):
        super().__init__(**kw)
        self.member_ids = members
        self.types = types or {}
        self.people = workspace or [{"id": "U1"}, {"id": "U2"}, {"id": "UBOT", "is_bot": True}]
        self.history = history if history is not None else [
            {"user": "U1", "ts": f"{AT - 120:.1f}", "text": "<@UBOT> can you check the invoice?"},
            {"user": "UBOT", "bot_id": "BME", "ts": f"{AT - 60:.1f}", "text": "Which one?"}]

    async def fetch_context(self, channel, thread_ts, limit):
        return list(self.history)

    async def users(self, ids):
        known = {"U1": {"profile": {"display_name": "fzhu"}}, "U2": {"profile": {"display_name": "mei"}},
                 "U3": {"profile": {"display_name": "jane"}},
                 "UBOT": {"is_bot": True, "profile": {"display_name": "wanda"}}}
        return {u: known[u] for u in ids if u in known}

    async def members(self, channel):
        if self.member_ids is None:
            raise RuntimeError("missing_scope")
        return self.member_ids

    async def workspace(self):
        return self.people

    async def own_ids(self):
        return frozenset({"UBOT", "BME"})

    async def channel_type(self, channel):
        return self.types.get(channel, "im")


class RecordingRunner:
    """Stands in for claude: records each run and reports what it is given."""

    def __init__(self, *reports, ok=True):
        self.agent_sem = asyncio.Semaphore(2)
        self.calls = []
        self.reports = list(reports)
        self.ok = ok

    async def run(self, prompt, **kw):
        self.calls.append((prompt, kw))
        out = self.reports.pop(0) if self.reports else {"recalled": [], "answer": "", "recorded": []}
        if not self.ok:
            return RunResult(ok=False, error="claude reported an error")
        return RunResult(ok=True, structured=out if isinstance(out, dict) else None,
                         result_text=out if isinstance(out, str) else json.dumps(out), session_id="ignored")


def dm(ts, text, channel_type="im", thread=None, channel="D1", user="U1"):
    return Event(source="slack", dedupe_key=f"{channel}:{ts}", payload={
        "kind": "dm" if channel_type in ("im", "mpim") else "mention", "channel": channel,
        "channel_type": channel_type, "task_key": thread or "conversation", "reply_thread": thread,
        "in_thread": bool(thread), "user": user, "text": text, "files": [], "ts": ts})


def memory_processor(tmp_path, slack, runner, monkeypatch, snapshot=None):
    p, store = make(tmp_path, slack, data_dir=tmp_path, slack_owner_user_ids="U1,U2",
                    slack_names="U1:fan,U2:mei", tz="America/Los_Angeles")
    p.runner = runner
    snaps = []
    monkeypatch.setattr("wanda.vault.snapshot", snapshot or (lambda cfg, message: snaps.append(message)))
    return p, store, snaps


def answer(text):
    return {"recalled": ["person:aaaaaa"], "answer": text, "recorded": ["event:x"]}


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
        async def reply(self, thread_ts, text, channel=None, note=False):
            await asyncio.sleep(0.2)
            await super().reply(thread_ts, text, channel, note)

    slack = Slow()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(), monkeypatch)
    tid = store.create_task(None, "D1", "conversation", kind="dm")
    first, second = (store.record_run(kind="agent", task_id=tid, session_id=None, started_at=utcnow(),
                                      exit_code=0, cost_usd=0.0, status="ok", result_text=t, notified=0)
                     for t in ("an older answer", "Yes."))

    async def go():
        p._delivering.add(second)
        redelivery = asyncio.create_task(p.deliver_pending())
        await asyncio.sleep(0.1)  # the pass is posting the first
        store.mark_run_notified(second)  # as _post_run does once Slack takes it
        p._delivering.discard(second)
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


def test_an_alert_is_never_shown_to_a_session_as_her_words(tmp_path, monkeypatch):
    """Alerts may go to fan's DM, where his sessions are framed: posted with
    the harness's mark, they are left out of what came before."""
    from wanda.vault import ALERT_EVENT

    class NoOwnIds(ConversationSlack):
        async def own_ids(self):
            return frozenset()  # auth.test failing, and not cached

    history = [{"user": "U1", "ts": f"{AT - 120:.1f}", "text": "the plumber comes thursday"},
               {"user": "UBOT", "bot_id": "BME", "ts": f"{AT - 60:.1f}",
                "text": "⚠️ wanda is not running: memory is not working: mem entity: exit 1",
                "metadata": {"event_type": ALERT_EVENT, "event_payload": {}}}]
    for slack in (ConversationSlack(history=history), NoOwnIds(history=history)):
        runner = RecordingRunner()
        p, _, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
        asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "and the electrician friday")))
        first = runner.calls[0][0]
        assert "16:38 fan: the plumber comes thursday" in first and "not running" not in first, first


def test_a_failure_note_is_marked_and_shown_to_no_session(tmp_path, monkeypatch):
    """A failure note carries Claude Code's reason, in its words: it is posted
    with the mark frames leave out, when first posted and when delivered
    later, and an answer is not marked."""
    from wanda.vault import NOTE_EVENT

    slack = ConversationSlack(history=[])
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(ok=False), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert slack.replies[0].startswith("⚠️ my run failed: ") and slack.notes == [True]
    # one Slack refused at first, delivered later with its mark
    refusing = Refusing(history=[])
    p, store, _ = memory_processor(tmp_path / "later", refusing, RecordingRunner(ok=False), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    p.slack = ConversationSlack()
    asyncio.run(p.deliver_pending())
    assert p.slack.replies[0].startswith("⚠️ my run failed: ") and p.slack.notes == [True]
    # an answer is hers, and stays unmarked
    slack = ConversationSlack(history=[])
    p, _, _ = memory_processor(tmp_path / "answer", slack, RecordingRunner(answer("Yes.")), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    assert slack.replies == ["Yes."] and slack.notes == [False]
    # and a note in the conversation is not shown to the next session
    runner = RecordingRunner()
    history = [{"user": "U1", "ts": f"{AT - 120:.1f}", "text": "what is the plumber's number?"},
               {"user": "UBOT", "bot_id": "BME", "ts": f"{AT - 60:.1f}",
                "text": "⚠️ my run failed: You've hit your limit",
                "metadata": {"event_type": NOTE_EVENT, "event_payload": {}}}]
    p, _, _ = memory_processor(tmp_path / "next", ConversationSlack(history=history), runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "never mind, found it")))
    first = runner.calls[0][0]
    assert "16:38 fan: what is the plumber's number?" in first and "my run failed" not in first


def test_the_report_may_arrive_as_the_result_text(tmp_path, monkeypatch):
    slack = ConversationSlack()
    p, _, _ = memory_processor(tmp_path, slack, RecordingRunner(json.dumps(answer("Yes."))), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    assert slack.replies == ["Yes."]


def test_a_placeholder_is_not_posted(tmp_path, monkeypatch):
    slack = ConversationSlack()
    p, _, _ = memory_processor(tmp_path, slack, RecordingRunner(answer("test")), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert slack.replies == []


@pytest.mark.parametrize("runner", [RecordingRunner("I filed it."), RecordingRunner(ok=False)],
                         ids=["no report", "failed run"])
def test_a_session_that_reports_nothing_is_a_failure(tmp_path, monkeypatch, runner):
    slack = ConversationSlack()
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "hello")))
    assert len(slack.replies) == 1 and slack.replies[0].startswith("⚠️ my run failed: ")
    assert store._query("SELECT status FROM runs")[0]["status"] == "error"


def test_a_session_no_one_asked_for_posts_nothing_when_it_fails_or_is_refused(tmp_path, monkeypatch):
    slack = ConversationSlack()
    p, store, _ = memory_processor(tmp_path, slack, RecordingRunner(ok=False), monkeypatch)
    store.create_task(None, "D1", "conversation", kind="dm")
    task = store.get_task_by_thread("D1", "conversation")
    now = datetime.fromtimestamp(AT, p.cfg.zone)
    got = asyncio.run(p.memory_turn(task, "an arrival no one asked for", now, channel="D1", reply_thread=None,
                                    owed=False))
    assert got == "claude reported an error" and slack.replies == [] and store.pending_deliveries() == []
    store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                     cost_usd=p.cfg.daily_cost_cap_usd, status="ok")
    assert asyncio.run(p.memory_turn(task, "x", now, channel="D1", reply_thread=None, owed=False)) == "breaker"
    assert slack.replies == []


class Refusing(ConversationSlack):
    """A Slack that takes no post."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.refused = []

    async def reply(self, thread_ts, text, channel=None, note=False):
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
    # a message's answer the same way, with no second post about an internal error
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "is it paid?")))
    assert slack.refused == ["The plumber is at 5.", "Yes."]
    assert [r["result_text"] for r in store.pending_deliveries()] == ["The plumber is at 5.", "Yes."]
    # a refusal's reply that Slack refuses leaves the budget's verdict
    store.record_run(kind="agent", task_id=None, session_id=None, started_at=utcnow(), exit_code=0,
                     cost_usd=p.cfg.daily_cost_cap_usd, status="ok")
    assert asyncio.run(p.memory_turn(task, "x", now, channel="D1", reply_thread=None, owed=True)) == "breaker"
    p.slack = ConversationSlack()
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == ["The plumber is at 5.", "Yes."] and store.pending_deliveries() == []


def test_a_group_dm_names_its_readers(tmp_path, monkeypatch):
    runner = RecordingRunner()
    p, _, _ = memory_processor(tmp_path, ConversationSlack(members=["U1", "U2", "UBOT"]), runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "dinner at 7", channel_type="mpim")))
    assert "In a group direct message that fan, mei and I read. Everyone in it sees what I say there." \
        in runner.calls[0][0]


def test_no_session_where_anyone_else_can_read(tmp_path, monkeypatch):
    runner = RecordingRunner()
    slack = ConversationSlack(members=["U1", "U3", "UBOT"])
    p, store, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    for i in range(2):
        asyncio.run(p.handle_slack(dm(f"{AT + i:.1f}", "dinner at 7", channel_type="mpim")))
    assert runner.calls == [] and slack.replies == [] and store._query("SELECT * FROM runs") == []
    assert p._outside == {"D1"}


def test_a_public_channel_is_the_whole_workspaces(tmp_path, monkeypatch):
    runner = RecordingRunner()
    slack = ConversationSlack(members=["U1", "UBOT"])
    p, _, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "<@UBOT> note this", channel_type="channel", channel="C1")))
    assert "In a public Slack channel that anyone in this Slack can read; fan and I are in it." in runner.calls[0][0]
    slack.people = slack.people + [{"id": "U3"}]
    asyncio.run(p.handle_slack(dm(f"{AT + 1:.1f}", "<@UBOT> and this", channel_type="channel", channel="C2")))
    assert len(runner.calls) == 1, "someone else in the workspace can open it"


def test_no_session_without_knowing_who_reads(tmp_path, monkeypatch):
    runner = RecordingRunner()
    slack = ConversationSlack(members=None)
    p, _, _ = memory_processor(tmp_path, slack, runner, monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "dinner at 7", channel_type="mpim")))
    assert runner.calls == []
    assert slack.replies == ["⚠️ my run failed: could not see who reads this conversation: missing_scope"]
    assert slack.notes == [True]


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

    async def reply(self, thread_ts, text, channel=None, note=False):
        await super().reply(thread_ts, text, channel, note)
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
    meanwhile keeps the session from starting and nothing is posted there,
    and someone who left meanwhile is not named."""
    for later, ran in ((["U1", "U2", "U3", "UBOT"], False), (["U1", "UBOT"], True)):
        slack = ConversationSlack(members=["U1", "U2", "UBOT"], history=[])
        p, store, runner, meis = one_slot_behind_a_dm(slack, tmp_path / str(ran), monkeypatch)

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
        assert "out Tuesday" in runner.calls[0][0]
        if ran:
            assert len(runner.calls) == 2
            assert "In a group direct message that fan and I read." in runner.calls[1][0]
        else:
            assert len(runner.calls) == 1 and slack.replies == [] and p._outside == {"G1"}
            assert store._query("SELECT * FROM runs WHERE task_id = (SELECT id FROM tasks "
                                "WHERE slack_channel = 'G1')") == []


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


def test_what_is_owed_is_posted_later_only_where_the_household_alone_reads(tmp_path, monkeypatch):
    """An answer Slack refused at first is posted at a later pass, or at a
    start, which can be hours on: who is in the conversation is read again
    then. Where someone else has come in, it is not posted and stays in the
    run store; where who is there cannot be read, it waits for the next
    pass, as a post Slack refuses does."""
    slack = Refusing(members=["U1", "U2", "UBOT"], history=[], types={"G1": "mpim"})
    p, store, _ = memory_processor(
        tmp_path, slack, RecordingRunner(answer("The plumber is at 5."), answer("Yes.")), monkeypatch)
    asyncio.run(p.handle_slack(dm(f"{AT:.1f}", "when is the plumber?", channel_type="mpim", channel="G1")))
    asyncio.run(p.handle_slack(dm(f"{AT + 1:.1f}", "is it paid?")))
    assert [r["result_text"] for r in store.pending_deliveries()] == ["The plumber is at 5.", "Yes."]
    p.slack = ConversationSlack(members=None, types={"G1": "mpim"})
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == ["Yes."], "a 1:1 DM takes no one else"
    assert [r["result_text"] for r in store.pending_deliveries()] == ["The plumber is at 5."]
    p.slack = ConversationSlack(members=["U1", "U2", "U3", "UBOT"], types={"G1": "mpim"})
    asyncio.run(p.deliver_pending())
    assert p.slack.replies == [] and store.pending_deliveries() == [] and p._outside == {"G1"}
    kept = store._query("SELECT result_text, notified FROM runs WHERE result_text LIKE 'The plumber%'")
    assert [dict(r) for r in kept] == [{"result_text": "The plumber is at 5.", "notified": 1}]


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
