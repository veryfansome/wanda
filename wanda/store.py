from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
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

-- Memory: durable state that is NOT derivable from the vault lives here,
-- never in the disposable memory.idx.
CREATE TABLE IF NOT EXISTS memory_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Hash of every claim line wanda wrote, for drift (hand-edit) detection.
CREATE TABLE IF NOT EXISTS memory_shas (
  path  TEXT NOT NULL,
  block TEXT NOT NULL,
  sha   TEXT NOT NULL,
  PRIMARY KEY (path, block)
);
-- Whether an owner-tier ledger line's Slack message checked out.
CREATE TABLE IF NOT EXISTS memory_owner_checks (
  cause      TEXT PRIMARY KEY,
  verified   INTEGER NOT NULL,
  checked_at TEXT NOT NULL,
  detail     TEXT
);
-- Templated rule offers the digest made (`reply rule k4`).
CREATE TABLE IF NOT EXISTS memory_offers (
  ref        TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  subject    TEXT NOT NULL,
  action     TEXT,
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  taken_at   TEXT
);
-- Agent-run windows: which task kind was running when. Provenance of a
-- ledger line written from a shell is decided against these, not against
-- anything the writer says about itself.
CREATE TABLE IF NOT EXISTS memory_run_windows (
  session_id TEXT PRIMARY KEY,
  task_id    INTEGER,
  kind       TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at   TEXT
);
-- Digest lines waiting for the daily post.
CREATE TABLE IF NOT EXISTS memory_digest (
  id         INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  kind       TEXT NOT NULL,
  text       TEXT NOT NULL,
  posted_at  TEXT
);
"""

# Indexes on pre-existing tables: applied after migrations, tolerantly, so a
# database that predates a column never fails to open.
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_addr)",
    "CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at)",
)

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
        for stmt in INDEXES:
            try:
                self._db.execute(stmt)
            except sqlite3.OperationalError:
                pass

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
        defaults to the same value. A DM is no exception: every top-level
        DM message roots its own thread, exactly like a channel mention."""
        now = utcnow()
        cur = self._exec(
            "INSERT OR IGNORE INTO tasks(message_pk, slack_channel, thread_ts, status, kind, "
            "reply_thread, created_at, updated_at) VALUES(?,?,?,'open',?,?,?,?)",
            (message_pk, channel, thread_ts, kind,
             thread_ts if reply_thread is None else reply_thread, now, now),
        )
        if cur.rowcount:
            return cur.lastrowid
        return self.get_task_by_thread(channel, thread_ts)["id"]

    def get_task_by_thread(self, channel: str, thread_ts: str) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT * FROM tasks WHERE slack_channel=? AND thread_ts=?", (channel, thread_ts)
        )
        return rows[0] if rows else None

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM tasks WHERE id=?", (task_id,))
        return rows[0] if rows else None

    def recent_answers(self, channel: str, limit: int = 3) -> list[sqlite3.Row]:
        """wanda's latest delivered answers in a conversation, newest last.
        conversations.history omits thread replies, so a new DM's seed would
        otherwise show the asker's questions and none of wanda's answers."""
        rows = self._query(
            "SELECT r.result_text, r.started_at FROM runs r JOIN tasks t ON t.id=r.task_id "
            "WHERE t.slack_channel=? AND r.kind='agent' AND r.status='ok' AND r.result_text IS NOT NULL "
            "ORDER BY r.id DESC LIMIT ?", (channel, limit),
        )
        return list(reversed(rows))

    # --- memory: agent-run windows ---

    def open_run_window(self, session_id: str, task_id: int | None, kind: str) -> dict:
        started = utcnow()
        self._exec(
            "INSERT INTO memory_run_windows(session_id, task_id, kind, started_at, ended_at) VALUES(?,?,?,?,NULL) "
            "ON CONFLICT(session_id) DO UPDATE SET started_at=excluded.started_at, ended_at=NULL, kind=excluded.kind",
            (session_id, task_id, kind, started),
        )
        return {"session_id": session_id, "task_id": task_id, "kind": kind, "started_at": started, "ended_at": None}

    def close_run_window(self, session_id: str) -> str:
        ended = utcnow()
        self._exec("UPDATE memory_run_windows SET ended_at=? WHERE session_id=? AND ended_at IS NULL", (ended, session_id))
        return ended

    def close_orphan_windows(self) -> int:
        """At startup: a window still open belongs to a run the previous
        daemon never finished. Left open it would make every later line
        look like it was written during that run."""
        cur = self._exec("UPDATE memory_run_windows SET ended_at=? WHERE ended_at IS NULL", (utcnow(),))
        return cur.rowcount

    def all_windows(self, since_days: int = 400) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat(timespec="seconds")
        return [dict(r) for r in self._query("SELECT * FROM memory_run_windows WHERE started_at >= ?", (since,))]

    def open_windows(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM memory_run_windows WHERE ended_at IS NULL")

    def windows_at(self, when_iso: str, slack_s: int = 300) -> list[dict]:
        return windows_covering(self.all_windows(), when_iso, slack_s)

    def task_had_run_near(self, task_id: int, when_iso: str, slack_s: int = 300) -> bool:
        return any(r["task_id"] == task_id for r in self.windows_at(when_iso, slack_s))


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

    def bump_delivery_attempt(self, run_id: int) -> int:
        """Delivery cannot retry forever: an answer for a channel wanda was
        removed from would block every later delivery behind it."""
        with self._lock:
            self._db.execute(
                "UPDATE runs SET deliver_attempts = deliver_attempts + 1 WHERE id=?", (run_id,)
            )
            row = self._db.execute(
                "SELECT deliver_attempts FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            self._db.commit()
        return row["deliver_attempts"] if row else 0

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

    def get_digest_by_thread(self, channel: str, thread_ts: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM digests WHERE channel=? AND thread_ts=?", (channel, thread_ts))
        return rows[0] if rows else None

    # --- memory: durable state the vault cannot hold ---

    def memory_get(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM memory_meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def memory_set(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO memory_meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def shas_for(self, path: str) -> dict[str, str]:
        return {r["block"]: r["sha"] for r in self._query("SELECT block, sha FROM memory_shas WHERE path=?", (path,))}

    def set_shas(self, path: str, shas: dict[str, str]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM memory_shas WHERE path=?", (path,))
            self._db.executemany("INSERT INTO memory_shas(path, block, sha) VALUES(?,?,?)",
                                 [(path, b, h) for b, h in shas.items()])
            self._db.commit()

    def move_shas(self, old: str, new: str) -> None:
        self._exec("UPDATE OR REPLACE memory_shas SET path=? WHERE path=?", (new, old))

    def owner_check(self, cause: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM memory_owner_checks WHERE cause=?", (cause,))
        return rows[0] if rows else None

    def set_owner_check(self, cause: str, verified: bool, detail: str = "") -> None:
        self._exec(
            "INSERT INTO memory_owner_checks(cause, verified, checked_at, detail) VALUES(?,?,?,?) "
            "ON CONFLICT(cause) DO UPDATE SET verified=excluded.verified, checked_at=excluded.checked_at, detail=excluded.detail",
            (cause, 1 if verified else 0, utcnow(), detail[:300]),
        )

    def add_offer(self, kind: str, subject: str, action: str | None, text: str) -> str:
        with self._lock:
            # From the highest ref, not the row count: a deleted offer must
            # not hand its ref to a new one (ref is the PRIMARY KEY).
            n = self._db.execute(
                "SELECT COALESCE(MAX(CAST(SUBSTR(ref, 2) AS INTEGER)), 0) AS n FROM memory_offers WHERE ref GLOB 'k*'"
            ).fetchone()["n"]
            ref = f"k{n + 1}"
            self._db.execute(
                "INSERT INTO memory_offers(ref, kind, subject, action, text, created_at) VALUES(?,?,?,?,?,?)",
                (ref, kind, subject, action, text, utcnow()),
            )
            self._db.commit()
        return ref

    def get_offer(self, ref: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM memory_offers WHERE ref=?", (ref,))
        return rows[0] if rows else None

    def find_offer(self, subject: str, text: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM memory_offers WHERE subject=? AND text=?", (subject, text))
        return rows[0] if rows else None

    def take_offer(self, ref: str) -> None:
        self._exec("UPDATE memory_offers SET taken_at=? WHERE ref=?", (utcnow(), ref))

    def digest_add(self, kind: str, text: str) -> None:
        self._exec("INSERT INTO memory_digest(created_at, kind, text) VALUES(?,?,?)", (utcnow(), kind, text[:1500]))

    def digest_pending(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM memory_digest WHERE posted_at IS NULL ORDER BY id")

    def digest_mark_posted(self, ids: list[int]) -> None:
        if not ids:
            return
        q = ",".join("?" * len(ids))
        self._exec(f"UPDATE memory_digest SET posted_at=? WHERE id IN ({q})", (utcnow(), *ids))

    def sender_stats(self, addr: str, since_iso: str = "") -> dict:
        """Verdict history for one address, from the messages table; with
        since_iso, only from that timestamp on. from_addr is the raw From
        header, so rows are prefiltered by substring and then confirmed by
        parsing that header: a bare `%addr%` counted enews@x for news@x, a
        spoofed priya@x.example.evil.com for priya@x.example, and any address a
        sender put inside their own display name. A header no parser can split
        (`a@b.example>`, `a@b.example (N) <c@d.example>`) now counts for
        nobody — the fail-closed direction for a count, and the reason this
        does not mirror triage.addresses_in's regex fallback, which would let
        exactly those attacker-shaped headers pool under any address they
        name. Callers pass one argument (recall.StatsFn); the window is
        make_offers' (passes.py:1587)."""
        addr = addr.lower()
        window = " AND created_at >= ?" if since_iso else ""
        params = (addr, since_iso) if since_iso else (addr,)
        rows = self._query(
            "SELECT from_addr, applied_action, COUNT(*) AS n, MAX(created_at) AS last FROM messages "
            f"WHERE instr(lower(from_addr), ?) > 0{window} GROUP BY from_addr, applied_action", params,
        )
        out = {"seen": 0, "ignored": 0, "trashed": 0, "attention": 0, "last": ""}
        for r in rows:
            if addr not in [a.lower() for _, a in getaddresses([r["from_addr"] or ""]) if "@" in a]:
                continue
            out["seen"] += r["n"]
            a = r["applied_action"] or ""
            if a == "ignore":
                out["ignored"] += r["n"]
            elif a in ("trash", "shadow_trash"):
                out["trashed"] += r["n"]
            elif a == "attention":
                out["attention"] += r["n"]
            if r["last"] and r["last"] > out["last"]:
                out["last"] = r["last"]
        out["last"] = out["last"][:10]
        return out

    def senders_since(self, since_iso: str) -> list[sqlite3.Row]:
        """Raw From headers seen since `since`; callers aggregate by address,
        since one address arrives under several display names."""
        return self._query(
            "SELECT from_addr, COUNT(*) AS n, MAX(created_at) AS last FROM messages WHERE created_at >= ? "
            "GROUP BY from_addr ORDER BY n DESC LIMIT 2000", (since_iso,),
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



def windows_covering(windows: list[dict], when_iso: str, slack_s: int = 300) -> list[dict]:
    """The agent runs in flight at `when`. Ledger times are minute-truncated,
    so both ends are compared at minute precision, with a small grace after
    the end (a session's last CLI call can land just after its envelope)."""
    try:
        w = datetime.fromisoformat(when_iso).replace(second=0, microsecond=0)
    except (TypeError, ValueError):
        return []
    out = []
    for r in windows:
        try:
            start = datetime.fromisoformat(r["started_at"]).replace(second=0, microsecond=0)
            end = datetime.fromisoformat(r["ended_at"]) if r["ended_at"] else None
        except (TypeError, ValueError):
            continue
        if start <= w and (end is None or w <= (end + timedelta(seconds=slack_s)).replace(second=0, microsecond=0)):
            out.append(r)
    return out

