from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS imap_cursor (
  folder        TEXT PRIMARY KEY,
  uidvalidity   INTEGER NOT NULL,
  last_seen_uid INTEGER NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id             INTEGER PRIMARY KEY,
  dedupe_key     TEXT NOT NULL UNIQUE,
  message_id     TEXT,
  folder         TEXT NOT NULL,
  uidvalidity    INTEGER NOT NULL,
  uid            INTEGER NOT NULL,
  from_addr      TEXT,
  subject        TEXT,
  date_hdr       TEXT,
  snippet        TEXT,
  status         TEXT NOT NULL DEFAULT 'new',
  verdict_json   TEXT,
  applied_action TEXT,
  error          TEXT,
  attempts       INTEGER NOT NULL DEFAULT 0,
  moved_at       TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);

CREATE TABLE IF NOT EXISTS tasks (
  id                INTEGER PRIMARY KEY,
  -- NULL for mention/DM tasks: those have no email behind them.
  message_pk        INTEGER REFERENCES messages(id),
  slack_channel     TEXT NOT NULL,
  thread_ts         TEXT NOT NULL,
  claude_session_id TEXT,
  status            TEXT NOT NULL DEFAULT 'open',
  kind              TEXT NOT NULL DEFAULT 'email',
  reply_thread      TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE (slack_channel, thread_ts)
);

CREATE TABLE IF NOT EXISTS runs (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  task_id    INTEGER REFERENCES tasks(id),
  session_id TEXT,
  started_at TEXT NOT NULL,
  ended_at   TEXT,
  exit_code  INTEGER,
  cost_usd   REAL,
  status     TEXT,
  error      TEXT,
  result_text TEXT
);

CREATE TABLE IF NOT EXISTS slack_events (
  event_id    TEXT PRIMARY KEY,
  received_at TEXT NOT NULL
);

-- A member's message to her, kept from when it is seen until its answer is
-- posted: `due` until a turn records what it came to, then `answered` by
-- `run`, the run that posts her answer or note. Slack never sends a message
-- again once it is acknowledged, so this is what a stop or a crash leaves to
-- run again. `session` is the last session that took it, and `tries` how many
-- turns a session began for it and did not finish.
CREATE TABLE IF NOT EXISTS unanswered (
  channel  TEXT NOT NULL,
  ts       TEXT NOT NULL,
  task_key TEXT NOT NULL,
  payload  TEXT NOT NULL,
  state    TEXT NOT NULL DEFAULT 'due',
  tries    INTEGER NOT NULL DEFAULT 0,
  session  TEXT,
  run      INTEGER REFERENCES runs(id),
  PRIMARY KEY (channel, ts)
);

CREATE TABLE IF NOT EXISTS digests (
  local_date TEXT PRIMARY KEY,
  channel    TEXT NOT NULL,
  thread_ts  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# Columns added after the first schema; CREATE TABLE IF NOT EXISTS won't add
# them to a database that already exists, so they are applied explicitly.
MIGRATIONS = (
    ("messages", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "moved_at", "TEXT"),
    ("runs", "result_text", "TEXT"),
    # DEFAULT 1: ALTER TABLE backfills existing rows with this, and every run
    # already in the table was delivered before this column existed.
    ("runs", "notified", "INTEGER NOT NULL DEFAULT 1"),
    ("messages", "deferred_until", "TEXT"),
    # 'email' | 'mention' | 'mention_guest' | 'dm' — where the task came from.
    ("tasks", "kind", "TEXT NOT NULL DEFAULT 'email'"),
    # Where replies are posted. Distinct from thread_ts, which is the task KEY
    # and for a DM holds a sentinel that is not a Slack timestamp.
    ("tasks", "reply_thread", "TEXT"),
    ("runs", "deliver_attempts", "INTEGER NOT NULL DEFAULT 0"),
)

# How many of the intervals she was not running are kept, the newest; a
# restart loop leaves one (Store.came_up).
DOWN_KEPT = 5


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Settled(NamedTuple):
    """What a recorded run does to the kept messages of its turn, each by
    (channel, ts): answered by the run that posts the turn's answer or note
    (the note's, when there is one), gone after a silence, or kept due with
    its payload, to run again as the conversation's next turn; or, written
    with no run, kept as they are with a payload naming the turn's first
    try, which a stop left with no retry."""
    answered: tuple = ()
    gone: tuple = ()
    again: tuple = ()  # of ((channel, ts), payload)
    first_try: tuple = ()  # of ((channel, ts), payload)


class Store:
    """SQLite is the source of truth; Slack is the UI. Single process writes,
    from both the asyncio loop and the IMAP watcher thread, hence the lock."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        self._relax_task_message_fk()
        for table, column, decl in MIGRATIONS:
            existing = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        # Repair databases migrated by the build that backfilled notified=0,
        # which would replay every historical answer into Slack on startup.
        marked = self._db.execute(
            "SELECT value FROM meta WHERE key='notified_backfilled'"
        ).fetchone()
        if not marked:
            self._db.execute("UPDATE runs SET notified=1")
            self._db.execute("INSERT INTO meta(key, value) VALUES('notified_backfilled','1')")
        # Pre-existing tasks reply in their own thread; without this they post
        # at channel top level. Meta-guarded rather than tied to the ADD COLUMN,
        # because an earlier build already added the column full of NULLs.
        # DM rows keep NULL — their key is a sentinel, not a thread id.
        if not self._db.execute(
            "SELECT value FROM meta WHERE key='reply_thread_backfilled'"
        ).fetchone():
            self._db.execute(
                "UPDATE tasks SET reply_thread = thread_ts "
                "WHERE reply_thread IS NULL AND kind <> 'dm'"
            )
            self._db.execute("INSERT INTO meta(key, value) VALUES('reply_thread_backfilled','1')")

    def _relax_task_message_fk(self) -> None:
        """A task used to require an email row. Mention- and DM-driven tasks
        have no email behind them, so message_pk must become nullable — which
        SQLite can only do by rebuilding the table."""
        cols = list(self._db.execute("PRAGMA table_info(tasks)"))
        if not cols or not any(c["name"] == "message_pk" and c["notnull"] for c in cols):
            return
        has_kind = any(c["name"] == "kind" for c in cols)
        kind_sel = "kind" if has_kind else "'email'"
        # executescript() would COMMIT before each statement, leaving durable
        # half-states: a crash between DROP and RENAME loses every task row,
        # because SCHEMA then recreates tasks empty on the next start. Run the
        # rebuild inside one explicit transaction instead. The PRAGMA must be
        # outside it — SQLite ignores foreign_keys changes within one.
        self._db.execute("PRAGMA foreign_keys=OFF")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for stmt in self._rebuild_statements(kind_sel):
                self._db.execute(stmt)
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        finally:
            self._db.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _rebuild_statements(kind_sel: str) -> tuple[str, ...]:
        return (
            """
            CREATE TABLE tasks_new (
              id                INTEGER PRIMARY KEY,
              message_pk        INTEGER REFERENCES messages(id),
              slack_channel     TEXT NOT NULL,
              thread_ts         TEXT NOT NULL,
              claude_session_id TEXT,
              status            TEXT NOT NULL DEFAULT 'open',
              kind              TEXT NOT NULL DEFAULT 'email',
              created_at        TEXT NOT NULL,
              updated_at        TEXT NOT NULL,
              UNIQUE (slack_channel, thread_ts)
            )
            """,
            f"""
            INSERT INTO tasks_new (id, message_pk, slack_channel, thread_ts,
                                   claude_session_id, status, kind, created_at, updated_at)
              SELECT id, message_pk, slack_channel, thread_ts,
                     claude_session_id, status, {kind_sel}, created_at, updated_at FROM tasks
            """,
            "DROP TABLE tasks",
            "ALTER TABLE tasks_new RENAME TO tasks",
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    @contextlib.contextmanager
    def _transaction(self):
        """Statements written together or not at all. The lock is held across
        them, since the watcher's thread commits on this same connection, and
        a commit among them would keep the half before it; one that raises
        is rolled back, or the next write's commit would keep it."""
        with self._lock:
            try:
                yield self._db
            except BaseException:
                self._db.rollback()
                raise
            self._db.commit()

    # --- imap cursor ---

    def get_cursor(self, folder: str) -> tuple[int, int] | None:
        rows = self._query(
            "SELECT uidvalidity, last_seen_uid FROM imap_cursor WHERE folder=?", (folder,)
        )
        return (rows[0]["uidvalidity"], rows[0]["last_seen_uid"]) if rows else None

    def set_cursor(self, folder: str, uidvalidity: int, last_seen_uid: int) -> None:
        self._exec(
            "INSERT INTO imap_cursor(folder, uidvalidity, last_seen_uid, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(folder) DO UPDATE SET uidvalidity=excluded.uidvalidity, "
            "last_seen_uid=excluded.last_seen_uid, updated_at=excluded.updated_at",
            (folder, uidvalidity, last_seen_uid, utcnow()),
        )

    # --- messages ---

    def ingest_message(
        self,
        *,
        dedupe_key: str,
        message_id: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        from_addr: str,
        subject: str,
        date_hdr: str,
        snippet: str,
    ) -> bool:
        """Returns True if this is a new message (inserted), False if seen before."""
        now = utcnow()
        cur = self._exec(
            "INSERT OR IGNORE INTO messages(dedupe_key, message_id, folder, uidvalidity, uid, "
            "from_addr, subject, date_hdr, snippet, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'new',?,?)",
            (dedupe_key, message_id, folder, uidvalidity, uid, from_addr, subject, date_hdr, snippet, now, now),
        )
        return cur.rowcount > 0

    def fetch_by_status(self, status: str, limit: int = 50) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM messages WHERE status=? ORDER BY id LIMIT ?", (status, limit)
        )

    def fetch_retryable(self, before: str, limit: int = 50) -> list[sqlite3.Row]:
        """'acting' rows whose backoff window has elapsed. Retries must be
        time-gated, not pass-gated: a burst of processor passes would otherwise
        exhaust the attempt budget during a short outage. Ordered by updated_at
        so a large backlog of not-yet-due rows can't crowd out due ones."""
        return self._query(
            "SELECT * FROM messages WHERE status='acting' AND updated_at <= ? "
            "ORDER BY updated_at LIMIT ?",
            (before, limit),
        )

    def defer_message(self, dedupe_key: str, until: str) -> None:
        """A rate-capped trash isn't a verdict change, it's 'not yet' — park the
        row until the window reopens instead of retiring it."""
        self._exec(
            "UPDATE messages SET status='deferred', deferred_until=?, updated_at=? WHERE dedupe_key=?",
            (until, utcnow(), dedupe_key),
        )

    def fetch_due_deferred(self, now: str, limit: int = 50) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM messages WHERE status='deferred' AND deferred_until <= ? "
            "ORDER BY deferred_until LIMIT ?",
            (now, limit),
        )

    def count_by_status(self, status: str) -> int:
        return self._query("SELECT COUNT(*) AS n FROM messages WHERE status=?", (status,))[0]["n"]

    def requeue_errors(self) -> int:
        """Return abandoned rows to the pipeline (wanda requeue)."""
        cur = self._exec(
            "UPDATE messages SET status='acting', attempts=0, error=NULL, updated_at=? "
            "WHERE status='error'",
            (utcnow(),),
        )
        return cur.rowcount

    def get_message(self, pk: int) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM messages WHERE id=?", (pk,))
        return rows[0] if rows else None

    def get_message_by_key(self, dedupe_key: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM messages WHERE dedupe_key=?", (dedupe_key,))
        return rows[0] if rows else None

    def set_triaged(self, dedupe_key: str, verdict: dict[str, Any], applied_action: str) -> None:
        self._exec(
            "UPDATE messages SET status='triaged', verdict_json=?, applied_action=?, updated_at=? "
            "WHERE dedupe_key=?",
            (json.dumps(verdict), applied_action, utcnow(), dedupe_key),
        )

    def set_message_status(self, dedupe_key: str, status: str, error: str | None = None) -> None:
        self._exec(
            "UPDATE messages SET status=?, error=?, updated_at=? WHERE dedupe_key=?",
            (status, error, utcnow(), dedupe_key),
        )

    def bump_attempts(self, dedupe_key: str) -> int:
        with self._lock:
            self._db.execute(
                "UPDATE messages SET attempts = attempts + 1, updated_at=? WHERE dedupe_key=?",
                (utcnow(), dedupe_key),
            )
            row = self._db.execute(
                "SELECT attempts FROM messages WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            self._db.commit()
        return row["attempts"] if row else 0

    def mark_moved(self, dedupe_key: str) -> None:
        """Stamped only when an IMAP move actually happened — this, not the
        verdict, is what the trash rate caps count."""
        self._exec(
            "UPDATE messages SET moved_at=?, updated_at=? WHERE dedupe_key=?",
            (utcnow(), utcnow(), dedupe_key),
        )

    def trash_count_since(self, since: datetime) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM messages WHERE moved_at IS NOT NULL AND moved_at >= ?",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        )
        return rows[0]["n"]

    # --- tasks ---

    def create_task(self, message_pk: int | None, channel: str, thread_ts: str,
                    kind: str = "email", reply_thread: str | None = None) -> int:
        """thread_ts identifies the task; reply_thread is where answers go and
        defaults to the same value (an email or channel thread)."""
        now = utcnow()
        cur = self._exec(
            "INSERT OR IGNORE INTO tasks(message_pk, slack_channel, thread_ts, status, kind, "
            "reply_thread, created_at, updated_at) VALUES(?,?,?,'open',?,?,?,?)",
            (message_pk, channel, thread_ts, kind,
             thread_ts if reply_thread is None and kind != "dm" else reply_thread, now, now),
        )
        if cur.rowcount:
            return cur.lastrowid
        return self.get_task_by_thread(channel, thread_ts)["id"]

    def get_task_by_thread(self, channel: str, thread_ts: str) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT * FROM tasks WHERE slack_channel=? AND thread_ts=?", (channel, thread_ts)
        )
        return rows[0] if rows else None

    def set_task_session(self, task_id: int, session_id: str) -> None:
        self._exec(
            "UPDATE tasks SET claude_session_id=?, status='working', updated_at=? WHERE id=?",
            (session_id, utcnow(), task_id),
        )

    # --- runs / cost accounting ---

    def record_run(
        self,
        *,
        kind: str,
        task_id: int | None,
        session_id: str | None,
        started_at: str,
        exit_code: int | None,
        cost_usd: float | None,
        status: str,
        error: str | None = None,
        result_text: str | None = None,
        notified: int = 1,
        settled: Settled | None = None,
    ) -> int:
        """notified=0 marks a run whose outcome still owes the owner a Slack
        message, so a restart can deliver it. `settled` is what it does to
        its turn's kept messages, written with it: a restart finds the run
        and the messages it answers, or neither."""
        with self._transaction():
            run_id = self._insert_run(kind, task_id, session_id, started_at, exit_code, cost_usd, status, error,
                                      result_text, notified)
            self._settle(settled, run_id)
        return run_id

    def record_run_and_note(self, note: str, settled: Settled | None = None, **run) -> tuple[int, int]:
        """A run, and her note in its conversation after it, a run of kind
        `note` owed to the same task under the same session, written in one
        transaction with what they do to the turn's kept messages: a restart
        finds all of it or none, so a failure is never left with nothing said
        for it. Returns both ids."""
        with self._transaction():
            run_id = self._insert_run(**run)
            note_id = self._insert_run("note", run["task_id"], run["session_id"], run["started_at"], None, 0.0,
                                       "ok", None, note, 0)
            self._settle(settled, note_id)
        return run_id, note_id

    def settle(self, settled: Settled) -> None:
        """What a turn that recorded no run does to its kept messages."""
        with self._transaction():
            self._settle(settled, None)

    def _settle(self, settled: Settled | None, run_id: int | None) -> None:
        if settled is None:
            return
        self._db.executemany("UPDATE unanswered SET state='answered', run=? WHERE channel=? AND ts=?",
                             [(run_id, *k) for k in settled.answered])
        self._db.executemany("DELETE FROM unanswered WHERE channel=? AND ts=? AND state <> 'answered'",
                             list(settled.gone))
        # run again as a fresh turn, so the try of the turn that gave it back
        # is not counted against it
        self._db.executemany("UPDATE unanswered SET state='due', tries=0, payload=? WHERE channel=? AND ts=?",
                             [(json.dumps(p), *k) for k, p in settled.again])
        # the turn that takes them next is the first try's retry, its try
        # still counted
        self._db.executemany("UPDATE unanswered SET payload=? WHERE channel=? AND ts=? AND state <> 'answered'",
                             [(json.dumps(p), *k) for k, p in settled.first_try])

    def _insert_run(self, kind: str, task_id: int | None, session_id: str | None, started_at: str,
                    exit_code: int | None, cost_usd: float | None, status: str, error: str | None = None,
                    result_text: str | None = None, notified: int = 1) -> int:
        return self._db.execute(
            "INSERT INTO runs(kind, task_id, session_id, started_at, ended_at, exit_code, cost_usd, "
            "status, error, result_text, notified) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (kind, task_id, session_id, started_at, utcnow(), exit_code, cost_usd, status, error,
             result_text, notified),
        ).lastrowid

    def newest_run(self, task_id: int) -> int:
        """The id of the newest run recorded for this task, 0 for none."""
        rows = self._query("SELECT COALESCE(MAX(id), 0) AS id FROM runs WHERE task_id=?", (task_id,))
        return rows[0]["id"]

    def run_after(self, task_id: int, run_id: int) -> sqlite3.Row | None:
        """The first session's run recorded for this task after the run
        `run_id`: a note is not one."""
        rows = self._query("SELECT * FROM runs WHERE task_id=? AND id > ? AND kind <> 'note' ORDER BY id LIMIT 1",
                           (task_id, run_id))
        return rows[0] if rows else None

    def session_run(self, session_id: str) -> sqlite3.Row | None:
        """The first run recorded under this session, whatever its task: a
        session the harness names itself records under that name alone."""
        rows = self._query("SELECT * FROM runs WHERE session_id=? ORDER BY id LIMIT 1", (session_id,))
        return rows[0] if rows else None

    def run(self, run_id: int) -> sqlite3.Row | None:
        """The run recorded under this id."""
        rows = self._query("SELECT * FROM runs WHERE id=?", (run_id,))
        return rows[0] if rows else None

    def runs_since(self, since: datetime) -> tuple[int, float]:
        """The runs counted against the daily cap: not her notes, which run
        nothing, nor a session Claude Code refused, which ran nothing."""
        rows = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost FROM runs WHERE started_at >= ? "
            "AND kind <> 'note' AND COALESCE(status, '') <> 'refused'",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        )
        return rows[0]["n"], rows[0]["cost"]

    def pending_deliveries(self, task_id: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
        """Agent outcomes the owner never received: killed by a restart, or
        answered successfully but undeliverable at the time; only `task_id`'s
        when it is given."""
        return self._query(
            "SELECT r.*, t.reply_thread, t.slack_channel, t.kind AS task_kind FROM runs r "
            "JOIN tasks t ON t.id = r.task_id WHERE r.notified=0 AND (? IS NULL OR r.task_id = ?) "
            "ORDER BY r.id LIMIT ?",
            (task_id, task_id, limit),
        )

    def mark_run_notified(self, run_id: int) -> list[tuple[str, str]]:
        """A run posted: the messages it answers are no longer kept. Returns
        them, by (channel, ts)."""
        with self._transaction():
            self._db.execute("UPDATE runs SET notified=1 WHERE id=?", (run_id,))
            return [tuple(r) for r in self._db.execute(
                "DELETE FROM unanswered WHERE run=? AND state='answered' RETURNING channel, ts", (run_id,))]

    def owed_before(self, run_id: int) -> bool:
        """Whether a run recorded before this one in its task still owes its
        conversation a post."""
        return bool(self._query(
            "SELECT 1 FROM runs r JOIN runs o ON o.task_id = r.task_id AND o.id < r.id AND o.notified = 0 "
            "WHERE r.id = ? LIMIT 1", (run_id,)))

    def run_notified(self, run_id: int) -> bool:
        rows = self._query("SELECT notified FROM runs WHERE id=?", (run_id,))
        return bool(rows and rows[0]["notified"])

    def given_up_runs(self, attempts: int, limit: int = 20) -> list[sqlite3.Row]:
        """Answers delivery gave up on, newest first, with where each was due."""
        return self._query(
            "SELECT r.id, r.started_at, t.slack_channel, t.reply_thread FROM runs r "
            "JOIN tasks t ON t.id = r.task_id WHERE r.deliver_attempts >= ? ORDER BY r.id DESC LIMIT ?",
            (attempts, limit),
        )

    def first_refusal(self, run_id: int) -> bool:
        """Whether Slack has refused this run's post for the first time, which
        is then kept in its `deliver_attempts`: a run refused at every pass
        for two hours is logged once."""
        return self._exec("UPDATE runs SET deliver_attempts = 1 WHERE id=? AND deliver_attempts = 0",
                          (run_id,)).rowcount > 0

    def _notes_after(self, run_id: int) -> list[int]:
        # her notes owed after the run in its task under its session
        return [r["id"] for r in self._db.execute(
            "SELECT n.id FROM runs r JOIN runs n ON n.task_id = r.task_id AND n.session_id = r.session_id "
            "AND n.id > r.id WHERE r.id = ? AND n.kind = 'note' AND n.notified = 0", (run_id,))]

    def answering(self, run_id: int) -> list[sqlite3.Row]:
        """The kept messages a run answers, and those of the notes giving it
        up would drop with it, oldest first."""
        with self._lock:
            runs = [run_id, *self._notes_after(run_id)]
            return self._db.execute(
                f"SELECT * FROM unanswered WHERE state='answered' AND run IN ({','.join('?' * len(runs))}) "
                "ORDER BY CAST(ts AS REAL)", runs).fetchall()

    def give_up(self, run_id: int, attempts: int, note: str | None = None) -> tuple[list[int], int | None]:
        """Delivery gives a run up, so that it blocks nothing after it: marked
        notified with `attempts` as its count, by which a run given up is told
        from one posted. Her notes owed after it in its task under its
        session go with it, since each follows that answer and means nothing
        without it. The messages they answered are no longer kept, or, given
        a `note`, are answered by that note instead, a run of hers with no
        session, recorded after them in the same task. Returns the notes' ids
        and the new note's."""
        with self._transaction():
            self._db.execute("UPDATE runs SET notified=1, deliver_attempts=? WHERE id=?", (attempts, run_id))
            notes = self._notes_after(run_id)
            self._db.executemany("UPDATE runs SET notified=1 WHERE id=?", [(n,) for n in notes])
            runs = [run_id, *notes]
            which = f"state='answered' AND run IN ({','.join('?' * len(runs))})"
            given = None
            if note is not None:
                task = self._db.execute("SELECT task_id FROM runs WHERE id=?", (run_id,)).fetchone()["task_id"]
                given = self._insert_run("note", task, None, utcnow(), None, 0.0, "ok", None, note, 0)
                self._db.execute(f"UPDATE unanswered SET run=? WHERE {which}", (given, *runs))
            else:
                self._db.execute(f"DELETE FROM unanswered WHERE {which}", runs)
        return notes, given

    # --- the messages kept until they are answered ---

    def first_time(self, key: str, payload: dict | None = None) -> bool:
        """Whether a Slack message is seen for the first time, by its
        `channel:ts` key. A member's message to her (`payload`, as the
        watcher hands it on) is kept due in the same transaction, so that
        once it is seen nothing loses it before it is answered."""
        with self._transaction():
            first = self._db.execute("INSERT OR IGNORE INTO slack_events(event_id, received_at) VALUES(?,?)",
                                     (key, utcnow())).rowcount > 0
            if first and payload is not None:
                self._db.execute("INSERT OR IGNORE INTO unanswered(channel, ts, task_key, payload) VALUES(?,?,?,?)",
                                 (payload["channel"], payload["ts"], payload["task_key"], json.dumps(payload)))
        return first

    def kept(self, channel: str | None = None, task_key: str | None = None) -> list[sqlite3.Row]:
        """The messages kept, in one conversation when it is given, oldest
        first."""
        return self._query("SELECT * FROM unanswered WHERE ? IS NULL OR (channel=? AND task_key=?) "
                           "ORDER BY CAST(ts AS REAL)", (channel, channel, task_key))

    def took(self, sid: str, keys, counted) -> None:
        """The session `sid` has taken these kept messages, by (channel, ts):
        a try is counted for each in `counted`."""
        with self._transaction():
            self._db.executemany("UPDATE unanswered SET session=?, tries=tries+? WHERE channel=? AND ts=? "
                                 "AND state='due'", [(sid, int(k in counted), *k) for k in keys])

    def given_back(self, keys) -> None:
        """Kept messages written to a session that never took them in: the
        session and the try that write counted go."""
        with self._transaction():
            self._db.executemany("UPDATE unanswered SET session=NULL, tries=MAX(tries-1, 0) WHERE channel=? "
                                 "AND ts=? AND state='due'", list(keys))

    def forget(self, keys) -> list[tuple[str, str]]:
        """Kept messages that no run will answer: deleted, refused, or
        answered with no run. One already answered stays, for its run.
        Returns those no longer kept."""
        with self._transaction():
            return [k for k in keys if self._db.execute(
                "DELETE FROM unanswered WHERE channel=? AND ts=? AND state <> 'answered'", k).rowcount]

    # --- her running time ---

    def came_up(self, at: str) -> None:
        """A start that reached running, at `at`: she was not running from the
        last time she was known to be (`up_at`) until then. A start that got
        as far and died before a pass moved `up_at` on began from the same
        time, and this one's interval replaces its, so that a restart loop
        leaves one."""
        up = self.get_meta("up_at")
        if up is None:
            return
        down = json.loads(self.get_meta("down") or "[]")
        if down and down[-1][0] == up:
            down.pop()
        self.set_meta("down", json.dumps((down + [[up, at]])[-DOWN_KEPT:]))

    def down(self) -> list[tuple[datetime, datetime]]:
        """The intervals she was not running, oldest first, as many as are
        kept."""
        return [(datetime.fromisoformat(start), datetime.fromisoformat(end))
                for start, end in json.loads(self.get_meta("down") or "[]")]

    def ran(self, since: datetime, now: datetime) -> timedelta:
        """How long she has been running between `since` and `now`: the time
        between, less what of it falls in the intervals she was not."""
        ran = now - since
        for start, end in self.down():
            ran -= max(timedelta(0), min(end, now) - max(start, since))
        return ran

    def runs_today(self) -> tuple[int, float]:
        midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.runs_since(midnight_utc)

    # --- slack event dedupe ---

    def prune_slack_events(self, older_than_days: int = 7) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        self._exec(
            "DELETE FROM slack_events WHERE received_at < ?",
            (cutoff.isoformat(timespec="seconds"),),
        )

    # --- digests ---

    def get_digest(self, local_date: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM digests WHERE local_date=?", (local_date,))
        return rows[0] if rows else None

    def clear_digest(self, local_date: str) -> None:
        self._exec("DELETE FROM digests WHERE local_date=?", (local_date,))

    def set_digest(self, local_date: str, channel: str, thread_ts: str) -> None:
        self._exec(
            "INSERT OR IGNORE INTO digests(local_date, channel, thread_ts) VALUES(?,?,?)",
            (local_date, channel, thread_ts),
        )

    # --- meta ---

    def meta_starting(self, prefix: str) -> dict[str, str]:
        """Every meta row whose key starts with `prefix`, by key."""
        rows = self._query("SELECT key, value FROM meta WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))
        return {r["key"]: r["value"] for r in rows}

    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
