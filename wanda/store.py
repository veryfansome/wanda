from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    ) -> int:
        """notified=0 marks a run whose outcome still owes the owner a Slack
        message, so a restart can deliver it."""
        cur = self._exec(
            "INSERT INTO runs(kind, task_id, session_id, started_at, ended_at, exit_code, cost_usd, "
            "status, error, result_text, notified) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (kind, task_id, session_id, started_at, utcnow(), exit_code, cost_usd, status, error,
             result_text, notified),
        )
        return cur.lastrowid

    def runs_since(self, since: datetime) -> tuple[int, float]:
        rows = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost FROM runs WHERE started_at >= ?",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        )
        return rows[0]["n"], rows[0]["cost"]

    def pending_deliveries(self, limit: int = 50) -> list[sqlite3.Row]:
        """Agent outcomes the owner never received: killed by a restart, or
        answered successfully but undeliverable at the time."""
        return self._query(
            "SELECT r.*, t.reply_thread, t.slack_channel FROM runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.notified=0 ORDER BY r.id LIMIT ?",
            (limit,),
        )

    def mark_run_notified(self, run_id: int) -> None:
        self._exec("UPDATE runs SET notified=1 WHERE id=?", (run_id,))

    def runs_today(self) -> tuple[int, float]:
        midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.runs_since(midnight_utc)

    # --- slack event dedupe ---

    def slack_event_first_time(self, event_id: str) -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO slack_events(event_id, received_at) VALUES(?,?)",
            (event_id, utcnow()),
        )
        return cur.rowcount > 0

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

    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
