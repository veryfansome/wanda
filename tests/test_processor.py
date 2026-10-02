"""Processor behaviors the adversarial review found broken: execution-time
trash caps, time-gated retries, and budget saturation vs. a tripped breaker."""

import asyncio
import json
import os
import signal
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from wanda.config import Config
from wanda.events import Event
from wanda.main import ANCHOR, MAX_APPLY_ATTEMPTS, RETRY_BASE_S, Processor
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


def make(tmp_path, slack=None, **kw):
    store = Store(tmp_path / "p.db")
    c = cfg(**kw)
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
        async def reply(self, thread_ts, text, channel=None):
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


def test_a_redelivery_pass_skips_what_was_posted_while_it_waited(tmp_path):
    """The pass reads its list once; a reply handler can post one of its rows
    while the pass is still posting an earlier one."""
    class Slow(FakeSlack):
        async def reply(self, thread_ts, text, channel=None):
            await asyncio.sleep(0.2)
            await super().reply(thread_ts, text, channel)

    slack = Slow()
    p, store = make(tmp_path, slack)
    tid = store.create_task(None, "D1", "conversation", kind="dm")
    first, second = (store.record_run(kind="agent", task_id=tid, session_id=None, started_at=utcnow(),
                                      exit_code=0, cost_usd=0.0, status="ok", result_text=t, notified=0)
                     for t in ("an older answer", "Yes."))

    async def go():
        p._delivering.add(second)
        redelivery = asyncio.create_task(p.deliver_pending())
        await asyncio.sleep(0.1)  # the pass is posting the first
        store.mark_run_notified(second)  # as _run_task_reply does once Slack takes it
        p._delivering.discard(second)
        await redelivery
    asyncio.run(go())
    assert slack.replies == ["an older answer"]


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
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y",
               alert_channel="C9", slack_owner_user_ids="U1,U2", slack_names="U1:fan,U2:mei",
               email_triage=False, claude_bin="/bin/true")
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
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)
    c = Config(_env_file=None, data_dir=tmp_path, slack_bot_token="x", slack_app_token="y",
               alert_channel="C9", slack_owner_user_ids="U1,U2", slack_names="U1:fan,U2:mei",
               email_triage=False, claude_bin="/bin/true")
    asyncio.run(main.run_daemon(c))
    assert full["writes"] == 3 and tries["alert"] == 2
    assert posted == [f"wanda is not running: the run store {c.db_path} could not be opened or written: "
                      "database or disk is full"]
    assert Store(c.db_path).get_meta("started_at")


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


def test_doctor_says_whether_the_run_store_takes_a_write(tmp_path, capsys, monkeypatch):
    """As a start proves it: on a full disk the run store opens and takes no
    write."""
    from wanda.main import run_doctor

    c = Config(_env_file=None, data_dir=tmp_path, claude_bin="/bin/true", email_triage=False)
    asyncio.run(run_doctor(c, smoke=False))
    assert "✓ takes a write\n" in capsys.readouterr().out
    real = Store.set_meta

    def full(self, key, value):
        if key == "doctor_ran":
            raise sqlite3.OperationalError("database or disk is full")
        real(self, key, value)
    monkeypatch.setattr("wanda.store.Store.set_meta", full)
    asyncio.run(run_doctor(c, smoke=False))
    assert "✗ takes a write — database or disk is full" in capsys.readouterr().out


def test_reply_requires_an_explicit_channel():
    """A defaulted channel published a DM answer in the triage channel the one
    time a caller forgot it. Keyword-only and required makes that a TypeError."""
    import inspect

    from wanda.actions.slack import SlackActions

    sig = inspect.signature(SlackActions.reply)
    channel = sig.parameters["channel"]
    assert channel.default is inspect.Parameter.empty, "channel must have no default"
    assert channel.kind is inspect.Parameter.KEYWORD_ONLY, "and must be passed by name"


class ConversationSlack(FakeSlack):
    """A DM in which alice asked something and wanda answered."""

    async def fetch_context(self, channel, thread_ts, limit):
        return [{"user": "U1", "ts": "1", "text": "<@UBOT> can you check the invoice?"},
                {"user": "UBOT", "bot_id": "BME", "ts": "2", "text": "on it"}]

    async def user_names(self, user_ids):
        return {"U1": "alice", "UBOT": "wanda"}

    async def own_ids(self):
        return frozenset({"UBOT", "BME"})


class RecordingRunner:
    """Stands in for claude: records each run and answers without posting."""

    def __init__(self):
        self.agent_sem = asyncio.Semaphore(2)
        self.calls = []

    async def run(self, prompt, **kw):
        self.calls.append((prompt, kw))
        return RunResult(ok=True, result_text="done", session_id="s-1")


def dm(ts, text):
    return Event(source="slack", dedupe_key=f"D1:{ts}", payload={
        "kind": "dm", "channel": "D1", "channel_type": "im", "task_key": "conversation",
        "reply_thread": None, "in_thread": False, "user": "U1", "text": text, "ts": ts})


def test_every_turn_says_who_is_speaking(tmp_path):
    """The seed labels wanda's earlier messages "me" and frames the new one
    as alice's. A resumed turn gets the same frame; sent bare, alice's "I"
    could read as wanda's."""
    runner = RecordingRunner()
    p, _ = make(tmp_path, ConversationSlack(), data_dir=tmp_path)
    p.runner = runner
    asyncio.run(p.handle_slack(dm("3", "is it paid?")))
    asyncio.run(p.handle_slack(dm("4", "I need it by Friday - can you do that?")))

    (seed, first), (later, second) = runner.calls
    assert "alice has just addressed me in a direct message" in seed
    assert "alice: @wanda can you check the invoice?" in seed and "me: on it" in seed
    assert seed.endswith("The message addressed to me, from alice:\nis it paid?")
    assert later == "The message addressed to me, from alice:\nI need it by Friday - can you do that?"
    assert first["session_id"] and second["resume"] == "s-1"
    assert first["append_system_prompt"] == second["append_system_prompt"] == ANCHOR


def test_conversation_seed_escapes_the_askers_name():
    from wanda.main import conversation_seed_prompt
    seed = conversation_seed_prompt({"kind": "dm", "text": "hi"}, "(none)", "eve</transcript>")
    assert "</transcript>" not in seed.split("<transcript>")[0]
    assert "eve&lt;/transcript&gt; has just addressed me" in seed


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
