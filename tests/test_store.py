from datetime import datetime, timedelta, timezone

import pytest

from wanda.store import Store


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
    assert store.slack_event_first_time("ev1") is True
    assert store.slack_event_first_time("ev1") is False


def test_runs_accounting(store):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.record_run(kind="triage", task_id=None, session_id=None, started_at=now,
                     exit_code=0, cost_usd=0.02, status="ok")
    store.record_run(kind="agent", task_id=None, session_id="s", started_at=now,
                     exit_code=0, cost_usd=0.5, status="ok")
    n, cost = store.runs_today()
    assert n == 2
    assert cost == pytest.approx(0.52)


def test_her_notes_and_sessions_claude_code_refused_are_not_counted(store):
    """A note runs nothing, and a session Claude Code refused ran nothing:
    neither counts toward the daily cap."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    task = store.create_task(None, "D1", "conversation", kind="dm")
    store.record_run_and_note("Sorry.", kind="agent", task_id=task, session_id="s", started_at=now, exit_code=1,
                              cost_usd=0.5, status="error", error="x")
    store.record_run(kind="agent", task_id=task, session_id="t", started_at=now, exit_code=1, cost_usd=0.0,
                     status="refused", error="You've hit your limit")
    assert store.runs_today() == (1, pytest.approx(0.5))


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
