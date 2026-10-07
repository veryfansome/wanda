import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from wanda.store import Settled, Store

LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def ingest(store, key="k1", uid=1):
    return store.ingest_message(
        dedupe_key=key, message_id=f"<{key}@x>", folder="INBOX", uidvalidity=7, uid=uid,
        from_addr="a@example.com", subject="hi", date_hdr="today", snippet="body",
    )


def test_ingest_dedupes(store):
    assert ingest(store) is True
    assert ingest(store) is False
    assert len(store.fetch_by_status("new")) == 1


def test_state_transitions(store):
    ingest(store)
    store.set_triaged("k1", {"action": "trash", "guard_note": ""}, "trash")
    row = store.fetch_by_status("triaged")[0]
    assert row["applied_action"] == "trash"
    assert "trash" in row["verdict_json"]
    store.set_message_status("k1", "acting")
    store.set_message_status("k1", "done")
    assert store.fetch_by_status("new") == []
    assert store.get_message_by_key("k1")["status"] == "done"


def test_cursor_roundtrip(store):
    assert store.get_cursor("INBOX") is None
    store.set_cursor("INBOX", 7, 100)
    store.set_cursor("INBOX", 7, 105)
    assert store.get_cursor("INBOX") == (7, 105)


def test_tasks(store):
    ingest(store)
    pk = store.get_message_by_key("k1")["id"]
    t1 = store.create_task(pk, "C1", "111.222")
    t2 = store.create_task(pk, "C1", "111.222")  # idempotent
    assert t1 == t2
    task = store.get_task_by_thread("C1", "111.222")
    assert task["claude_session_id"] is None
    store.set_task_session(task["id"], "sess-1")
    assert store.get_task_by_thread("C1", "111.222")["claude_session_id"] == "sess-1"


def test_slack_event_dedupe(store):
    assert store.first_time("ev1") is True
    assert store.first_time("ev1") is False


def message(ts, channel="D1", task_key="conversation"):
    """A member's message as the watcher hands it on."""
    return {"kind": "dm", "channel": channel, "channel_type": "im", "task_key": task_key, "reply_thread": None,
            "in_thread": False, "user": "U1", "text": f"line {ts}", "files": [], "ts": ts}


def test_a_message_to_her_is_kept_as_it_is_seen(store):
    """In the transaction that records it as seen, before its conversation
    has a task; a redelivery keeps nothing again, and one that is not to
    her keeps nothing."""
    assert store.first_time("D1:1.1", message("1.1"))
    assert store.get_task_by_thread("D1", "conversation") is None
    assert not store.first_time("D1:1.1", message("1.1"))
    assert store.first_time("C1:2.2")
    [kept] = store.kept()
    assert (kept["channel"], kept["ts"], kept["task_key"], kept["state"], kept["tries"], kept["session"]) == (
        "D1", "1.1", "conversation", "due", 0, None)
    assert json.loads(kept["payload"]) == message("1.1")
    # one that cannot be kept is not marked seen either, so that Slack's
    # sending it again is taken
    store._db.execute("DROP TABLE unanswered")
    with pytest.raises(sqlite3.OperationalError):
        store.first_time("D1:3.3", message("3.3"))
    store.set_meta("next", "write")
    assert store._query("SELECT 1 FROM slack_events WHERE event_id='D1:3.3'") == []


def test_a_run_and_what_it_answers_are_written_together_while_the_watcher_writes(store, monkeypatch):
    """The watcher's thread commits on the same connection: the lock is held
    across a run and its kept messages, so a commit of the watcher's never
    keeps half of them, and one that fails leaves none."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.first_time("D1:1.1", message("1.1"))
    settle, watcher, seen = store._settle, [], threading.Event()

    def interrupted(settled, run_id):
        settle(settled, run_id)
        watcher.append(threading.Thread(target=lambda: store.first_time("D1:2.2", message("2.2")) and seen.set()))
        watcher[0].start()
        assert not seen.wait(0.3), "the watcher's write waits for the run's"
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(store, "_settle", interrupted)
    with pytest.raises(sqlite3.OperationalError):
        store.record_run(kind="agent", task_id=task, session_id="s", started_at=now, exit_code=0, cost_usd=0.1,
                         status="ok", result_text="Noted.", notified=0, settled=Settled(answered=(("D1", "1.1"),)))
    watcher[0].join(5)
    assert seen.is_set() and store._query("SELECT id FROM runs") == []
    assert [(r["ts"], r["state"], r["run"]) for r in store.kept()] == [("1.1", "due", None), ("2.2", "due", None)]


def test_what_a_run_answers_goes_when_it_is_posted(store):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    task = store.create_task(None, "D1", "conversation", kind="dm")
    for ts in ("1.1", "2.2", "3.3", "4.4"):
        store.first_time(f"D1:{ts}", message(ts))
    store.took("s1", [("D1", "1.1"), ("D1", "2.2"), ("D1", "3.3")], {("D1", "1.1"), ("D1", "3.3")})
    run = store.record_run(kind="agent", task_id=task, session_id="s1", started_at=now, exit_code=0, cost_usd=0.1,
                           status="ok", result_text="Noted.", notified=0, settled=Settled(
                               answered=(("D1", "1.1"),), gone=(("D1", "2.2"),),
                               again=((("D1", "3.3"), message("3.3") | {"again": "s1"}),)))
    assert [(r["ts"], r["state"], r["run"], r["tries"], r["session"]) for r in store.kept()] == [
        ("1.1", "answered", run, 1, "s1"), ("3.3", "due", None, 0, "s1"), ("4.4", "due", None, 0, None)]
    assert json.loads(store.kept()[1]["payload"])["again"] == "s1"
    # a deletion takes only what no run answers
    store.forget([("D1", "1.1"), ("D1", "4.4")])
    assert [r["ts"] for r in store.kept()] == ["1.1", "3.3"]
    store.mark_run_notified(run)
    assert [r["ts"] for r in store.kept()] == ["3.3"]


def test_a_deletion_marks_an_answered_message_and_still_forgets_a_due_one(store):
    """The answered one stays for its run, marked, which a post reads; one
    forgotten for any other reason is not marked."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    task = store.create_task(None, "D1", "conversation", kind="dm")
    for ts in ("1.1", "2.2", "3.3"):
        store.first_time(f"D1:{ts}", message(ts))
    run = store.record_run(kind="agent", task_id=task, session_id="s1", started_at=now, exit_code=0, cost_usd=0.1,
                           status="ok", result_text="Noted.", notified=0,
                           settled=Settled(answered=(("D1", "1.1"), ("D1", "3.3"))))
    assert store.forget([("D1", "3.3")]) == []
    assert store.forget([("D1", "1.1"), ("D1", "2.2")], deleted=True) == [("D1", "2.2")]
    assert [(r["ts"], r["state"], r["deleted"]) for r in store.kept()] == [("1.1", "answered", 1),
                                                                           ("3.3", "answered", 0)]
    assert [r["ts"] for r in store.answering(run)] == ["1.1", "3.3"]
    store.mark_run_notified(run)
    assert store.kept() == []


def test_runs_accounting(store):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.record_run(kind="triage", task_id=None, session_id=None, started_at=now,
                     exit_code=0, cost_usd=0.02, status="ok")
    store.record_run(kind="agent", task_id=None, session_id="s", started_at=now,
                     exit_code=0, cost_usd=0.5, status="ok")
    n, cost = store.runs_today(LA)
    assert n == 2
    assert cost == pytest.approx(0.52)


def test_the_days_runs_count_from_midnight_where_the_household_is(store, monkeypatch):
    """At 17:10 in Los Angeles, past UTC midnight: a run at 23:30 there the
    day before is not counted, those at 00:10 and at 16:50, before UTC
    midnight, are; a refusal is counted apart."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, 0, 10, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr("wanda.store.datetime", Clock)
    for (day, hh, mm), status in (((30, 23, 30), "ok"), ((1, 0, 10), "ok"), ((1, 16, 50), "ok"),
                                  ((1, 16, 55), "refused")):
        at = datetime(2026, 9 if day == 30 else 10, day, hh, mm, tzinfo=LA).astimezone(timezone.utc)
        store.record_run(kind="agent", task_id=None, session_id=None, started_at=at.isoformat(timespec="seconds"),
                         exit_code=0, cost_usd=0.1, status=status)
    assert store.runs_today(LA) == (2, pytest.approx(0.2))
    assert store.refused_today(LA) == 1


def test_her_notes_and_sessions_claude_code_refused_are_not_counted(store):
    """A note runs nothing, and a session Claude Code refused ran nothing:
    neither counts toward the daily cap."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.record_run_and_note("Sorry.", kind="agent", task_id=task, session_id="s", started_at=now, exit_code=1,
                              cost_usd=0.5, status="error", error="x")
    store.record_run(kind="agent", task_id=task, session_id="t", started_at=now, exit_code=1, cost_usd=0.0,
                     status="refused", error="You've hit your limit")
    assert store.runs_today(LA) == (1, pytest.approx(0.5))


def test_a_note_is_written_with_its_run_or_not_at_all(store, monkeypatch):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    task = store.create_task(None, "D1", "conversation", kind="dm")
    run, note = store.record_run_and_note("Sorry.", kind="agent", task_id=task, session_id="s", started_at=now,
                                          exit_code=1, cost_usd=0.1, status="error", error="x")
    assert [dict(r) for r in store._query("SELECT id, kind, session_id, result_text, notified FROM runs")] == [
        {"id": run, "kind": "agent", "session_id": "s", "result_text": None, "notified": 1},
        {"id": note, "kind": "note", "session_id": "s", "result_text": "Sorry.", "notified": 0}]
    insert = store._insert_run

    def fails_second(*a, **kw):
        if a and a[0] == "note":
            raise OSError("disk full")
        return insert(*a, **kw)
    monkeypatch.setattr(store, "_insert_run", fails_second)
    with pytest.raises(OSError):
        store.record_run_and_note("Sorry.", kind="agent", task_id=task, session_id="t", started_at=now,
                                  exit_code=1, cost_usd=0.1, status="error", error="x")
    store.set_meta("next", "write")  # a later write commits nothing of it
    assert len(store._query("SELECT id FROM runs")) == 2


def test_a_clock_wakes_run_is_found_past_a_note(store):
    """The clock finds a wake's run as the first after the newest before it,
    and a note can be written in the DM meanwhile, by a pass that does not
    take the conversation's lock."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    task = store.create_task(None, "D1", "conversation", kind="dm")
    before = store.newest_run(task)
    store.record_run(kind="note", task_id=task, session_id=None, started_at=now, exit_code=None, cost_usd=0.0,
                     status="ok", result_text="Sorry.", notified=0)
    wake = store.record_run(kind="agent", task_id=task, session_id="w", started_at=now, exit_code=0, cost_usd=0.0,
                            status="ok")
    assert store.run_after(task, before)["id"] == wake


def test_a_run_knows_whether_one_before_it_there_is_owed(store):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    here, there = (store.create_task(None, c, "conversation", kind="dm") for c in ("D1", "D2"))

    def run(task, owed):
        return store.record_run(kind="agent", task_id=task, session_id=None, started_at=now, exit_code=0,
                                cost_usd=0.0, status="ok", result_text="x", notified=0 if owed else 1)
    first = run(here, True)
    run(there, True)
    second = run(here, False)
    assert store.owed_before(second) and not store.owed_before(first)
    store.mark_run_notified(first)
    assert not store.owed_before(second)


def test_trash_count_counts_moves_not_verdicts(store):
    ingest(store, "k1", 1)
    ingest(store, "k2", 2)
    store.set_triaged("k1", {}, "trash")
    store.set_triaged("k2", {}, "trash")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    assert store.trash_count_since(cutoff) == 0  # verdicts alone don't count
    store.mark_moved("k1")
    assert store.trash_count_since(cutoff) == 1
    # A later status change must not retroactively re-date the move.
    store.set_message_status("k1", "done")
    assert store.trash_count_since(datetime.now(timezone.utc) + timedelta(seconds=1)) == 0


def test_bump_attempts(store):
    ingest(store)
    assert store.bump_attempts("k1") == 1
    assert store.bump_attempts("k1") == 2


def test_meta_and_digest(store):
    assert store.get_meta("x") is None
    store.set_meta("x", "1")
    store.set_meta("x", "2")
    assert store.get_meta("x") == "2"
    assert store.get_digest("2026-08-31") is None
    store.set_digest("2026-08-31", "C1", "9.9")
    assert store.get_digest("2026-08-31")["thread_ts"] == "9.9"


def test_the_threads_of_hers_a_session_ran_in_within_30_days_are_listed(store):
    """What a read back from Slack reads whatever the age of its first
    message: a mention's, a guest's and a DM's thread a session ran in; not
    an email task's, which is never read back, nor a DM's one conversation,
    nor a thread with no run, or whose last run is 31 days old."""
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

    def ran(channel, thread, kind, days, reply_thread=None, run_kind="agent"):
        task = store.create_task(None, channel, thread, kind=kind, reply_thread=reply_thread)
        if days is not None:
            store.record_run(kind=run_kind, task_id=task, session_id="s", started_at=(now - timedelta(days=days))
                             .isoformat(timespec="seconds"), exit_code=0, cost_usd=0.0, status="ok")
    ran("C1", "10.1", "mention", 2)
    ran("C1", "11.1", "mention_guest", 29)
    ran("D1", "12.1", "dm", 1, reply_thread="12.1")
    ran("D1", "conversation", "dm", 1)
    ran("C2", "13.1", "email", 1)
    ran("C1", "14.1", "mention", None)
    ran("C1", "15.1", "mention", 31)
    ran("C1", "16.1", "mention", 1, run_kind="note")
    assert store.threads(now) == [("C1", "10.1"), ("C1", "11.1"), ("D1", "12.1")]


def test_a_message_kept_carries_when_it_was_seen_until_the_record_is_pruned(store):
    """A message stays kept until it is answered; the record of when it was
    seen goes at seven days, and the message is listed without it."""
    p = {"channel": "D1", "ts": "1.1", "task_key": "conversation"}
    store.first_time("D1:1.1", p)
    [row] = store.kept()
    assert datetime.fromisoformat(row["received_at"]) <= datetime.now(timezone.utc)
    store._exec("UPDATE slack_events SET received_at=?",
                ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds"),))
    store.prune_slack_events()
    [row] = store.kept("D1", "conversation")
    assert row["ts"] == "1.1" and row["received_at"] is None
