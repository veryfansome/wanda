from datetime import datetime, timedelta, timezone

import pytest

from wanda.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def ingest(store, key="k1", uid=1, from_addr="a@example.com"):
    return store.ingest_message(
        dedupe_key=key, message_id=f"<{key}@x>", folder="INBOX", uidvalidity=7, uid=uid,
        from_addr=from_addr, subject="hi", date_hdr="today",
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


def test_offer_refs_are_never_reused(store):
    """ref is the PRIMARY KEY, so deriving the next one from the row count
    hands a deleted offer's ref to a new one and the insert raises."""
    for _ in range(4):
        store.add_offer("disposition", "org/x.example", "ignore", "ignore mail from x.example")
    store._exec("DELETE FROM memory_offers WHERE ref='k2'")
    assert store.add_offer("disposition", "org/y.example", "ignore", "ignore mail from y.example") == "k5"


def test_meta_and_digest(store):
    assert store.get_meta("x") is None
    store.set_meta("x", "1")
    store.set_meta("x", "2")
    assert store.get_meta("x") == "2"
    assert store.get_digest("2026-08-31") is None
    store.set_digest("2026-08-31", "C1", "9.9")
    assert store.get_digest("2026-08-31")["thread_ts"] == "9.9"


def test_sender_stats_counts_only_this_address(store):
    """from_addr is the raw From header. Counting by substring pooled a
    lookalike local part, a lookalike domain, and any address a sender wrote
    into their own display name under the address being asked about."""
    rows = [
        ("news@fabrikam.com", "trash"),
        ("News <news@fabrikam.com>", "trash"),
        ("Enews <enews@fabrikam.com>", "ignore"),
        ('"see news@fabrikam.com now" <evil@attacker.example>', "ignore"),
        ("Spoof <news@fabrikam.com.evil.example>", "ignore"),
        ("legacy@z.example (Legacy Form)", "ignore"),
        ("first@x.example, news@fabrikam.com", "trash"),
        ("Group: g@x.example;", "ignore"),
        ("news@fabrikam.com>", "trash"),          # no parser can split this: it counts for nobody
        ("Ax <axb@x.example>", "attention"),
    ]
    for i, (from_addr, action) in enumerate(rows):
        ingest(store, key=f"k{i}", uid=i, from_addr=from_addr)
        store.set_triaged(f"k{i}", {}, action)
    st = store.sender_stats("news@fabrikam.com")
    assert (st["seen"], st["trashed"], st["ignored"]) == (3, 3, 0)
    # Forms a `= ? OR LIKE '%<addr>%'` narrowing would miss.
    assert store.sender_stats("legacy@z.example")["ignored"] == 1
    assert store.sender_stats("g@x.example")["ignored"] == 1
    assert store.sender_stats("first@x.example")["trashed"] == 1
    assert store.sender_stats("a_b@x.example")["seen"] == 0, "`_` is a LIKE wildcard; instr carries none"
    assert store.sender_stats("news@fabrikam.com", since_iso="2099-01-01")["seen"] == 0


def test_body_cache_round_trips_and_is_not_persisted(store):
    ingest(store, key="k1")
    store.stash_body("k1", "the body")
    # Not a column: nothing about the body is in the row.
    row = store.get_message_by_key("k1")
    assert "snippet" not in row.keys()
    # take pops it; a second take is a miss (caller re-fetches from IMAP).
    assert store.take_body("k1") == "the body"
    assert store.take_body("k1") is None


def test_body_cache_evicts_oldest_over_cap(store):
    cap = Store.BODY_CACHE_CAP
    for i in range(cap + 5):
        store.stash_body(f"k{i}", f"b{i}")
    # The five oldest were evicted; they miss and fall back to a re-fetch.
    assert store.take_body("k0") is None
    assert store.take_body("k4") is None
    assert store.take_body(f"k{cap}") == f"b{cap}"


def test_messages_table_has_no_snippet_column(store):
    cols = {r["name"] for r in store._db.execute("PRAGMA table_info(messages)")}
    assert "snippet" not in cols
