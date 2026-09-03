"""Processor behaviors the adversarial review found broken: execution-time
trash caps, time-gated retries, and budget saturation vs. a tripped breaker."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from wanda.config import Config
from wanda.main import MAX_APPLY_ATTEMPTS, RETRY_BASE_S, Processor
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
    still answered; posting 'agent run failed' under it is noise."""
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


def test_reply_requires_an_explicit_channel():
    """A defaulted channel published a DM answer in the triage channel the one
    time a caller forgot it. Keyword-only and required makes that a TypeError."""
    import inspect

    from wanda.actions.slack import SlackActions

    sig = inspect.signature(SlackActions.reply)
    channel = sig.parameters["channel"]
    assert channel.default is inspect.Parameter.empty, "channel must have no default"
    assert channel.kind is inspect.Parameter.KEYWORD_ONLY, "and must be passed by name"
