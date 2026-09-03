"""memory.idx — a fully derived SQLite+FTS5 cache over the vault. Nothing
durable lives here: `rm memory.idx && wanda memory reindex` is always safe.
Trust is computed here from edges and from what the harness can verify,
never read from a label a writer wrote."""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol

from wanda.memory import ledger as L
from wanda.memory.notes import Claim, Note, parse_note, parse_writespec
from wanda.memory.subjects import keys_for
from wanda.memory.vault import DIR_TO_TYPE, Vault, sha_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, type TEXT, title TEXT, mtime REAL, sha TEXT,
  due TEXT, export INTEGER NOT NULL DEFAULT 1, created TEXT, retired INTEGER NOT NULL DEFAULT 0,
  about TEXT, tier TEXT, nbytes INTEGER);
CREATE TABLE IF NOT EXISTS ids (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases (alias TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY(alias, doc));
CREATE TABLE IF NOT EXISTS subject_alias (from_subject TEXT PRIMARY KEY, to_subject TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY, doc TEXT NOT NULL, block TEXT NOT NULL, text TEXT NOT NULL, sha TEXT NOT NULL,
  facet TEXT, cls TEXT NOT NULL, tier TEXT NOT NULL, n_support INTEGER, n_causes INTEGER, n_days INTEGER,
  owner_said INTEGER, pinned INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT, until TEXT,
  folded INTEGER DEFAULT 0, status TEXT NOT NULL, score REAL NOT NULL, lineno INTEGER,
  UNIQUE(doc, block));
CREATE INDEX IF NOT EXISTS ix_claims_live ON claims(doc, folded, status, score DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(text, content='claims', content_rowid='id');
CREATE TABLE IF NOT EXISTS edges (src_doc TEXT, src_block TEXT, rel TEXT, dst_doc TEXT, dst_block TEXT, value TEXT);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst_doc, dst_block);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src_doc, src_block);
CREATE TABLE IF NOT EXISTS obs (ulid TEXT PRIMARY KEY, day TEXT, ts TEXT, subject TEXT, facet TEXT, text TEXT,
  norm TEXT, src TEXT, op TEXT, cause TEXT, ref TEXT, due TEXT, until TEXT, tier TEXT, path TEXT, lineno INTEGER);
CREATE INDEX IF NOT EXISTS ix_obs_key ON obs(subject, facet, day);
CREATE INDEX IF NOT EXISTS ix_obs_cause ON obs(cause);
CREATE TABLE IF NOT EXISTS rkeys (ulid TEXT, key TEXT);
CREATE INDEX IF NOT EXISTS ix_rkeys ON rkeys(key, ulid);
CREATE TABLE IF NOT EXISTS vetoes (key TEXT PRIMARY KEY, until TEXT, ulid TEXT);
CREATE TABLE IF NOT EXISTS subjects (key TEXT PRIMARY KEY, doc TEXT, n_obs INTEGER, n_causes INTEGER, n_days INTEGER,
  first_seen TEXT, last_seen TEXT, untrusted INTEGER, has_file INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS writespecs (path TEXT PRIMARY KEY, prose TEXT, sha TEXT);
CREATE TABLE IF NOT EXISTS flags (path TEXT, block TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

TIER_RANK = {"email": 0, "session": 1, "owner": 2}
CONVERSATION_KINDS = ("mention", "mention_guest", "dm")


class TrustOracle(Protocol):
    """What the harness can verify about a ledger line's origin. Backed by
    wanda.db and Slack in the daemon; a dict in tests."""

    def owner_verified(self, cause: str) -> bool: ...
    def task_tier(self, task_id: int, when: datetime) -> str: ...


@dataclass
class DictTrust:
    verified_causes: set[str] = field(default_factory=set)
    task_kinds: dict[int, str] = field(default_factory=dict)

    def owner_verified(self, cause: str) -> bool:
        return cause in self.verified_causes

    def task_tier(self, task_id: int, when: datetime) -> str:
        kind = self.task_kinds.get(task_id)
        return "session" if kind in CONVERSATION_KINDS else "email"


def tier_for_obs(o: L.Observation, trust: TrustOracle) -> str:
    """Provenance is assigned from what can be checked, not from `src=`:
    an owner line must point at a Slack message the owner really wrote; an
    agent line is session-tier only if its task was a conversation (no
    email in the seed). Anything unverifiable gets the least trust."""
    if o.src == "owner":
        quarantined = getattr(trust, "line_quarantined", lambda u: False)(o.ulid)
        return "owner" if o.cause.startswith("slack:") and trust.owner_verified(o.cause) and not quarantined else "session"
    if o.src == "agent":
        if o.cause.startswith("task:"):
            try:
                return trust.task_tier(int(o.cause[5:]), o.when)
            except ValueError:
                return "email"
        return "email"
    if o.src == "triage":
        return "email"
    return "session"  # harness, import: the daemon's own writes


def open_index(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def open_readonly(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def norm_text(text: str) -> str:
    t = " ".join(text.lower().split()).rstrip(".!;, ")
    return sha_text(t)


def cause_key(o: L.Observation) -> str:
    """At most one triage memo per subject/facet/day counts as a cause, so a
    chatty sender cannot manufacture consensus in an afternoon."""
    if o.src == "triage":
        return f"triage-day:{o.day}"
    return o.cause or f"line:{o.ulid}"


def effective_status(c: dict, inbound: list[sqlite3.Row], today: str) -> str:
    if c.get("retired"):
        return "retired"
    if any(e["rel"] == "supersedes" for e in inbound):  # someone supersedes this claim
        return "superseded"
    if c.get("superseded_by"):
        return "superseded"
    if c.get("until") and c["until"] < today:
        return "expired"
    if any(e["rel"] == "contradicts" for e in inbound) or c.get("contradicts"):
        return "disputed"
    if c.get("owner_said"):
        return "owner-stated"
    if (c.get("n_causes") or 0) >= 3 and (c.get("n_days") or 0) >= 2:
        return "corroborated"
    return "provisional"


def score_for(owner_said: bool, n_causes: int, last_seen: str, status: str, today: str, tier_rank: int = 2) -> float:
    s = 3.0 * owner_said + 1.0 * math.log2(1 + max(0, n_causes)) + 0.5 * tier_rank
    if not owner_said and last_seen:
        try:
            age = (date.fromisoformat(today) - date.fromisoformat(last_seen[:10])).days
            s += 0.8 * math.exp(-max(0, age) / 45)
        except ValueError:
            pass
    if status == "disputed":
        s -= 2.0
    return round(s, 4)


@dataclass
class RebuildReport:
    docs: int = 0
    claims: int = 0
    obs: int = 0
    rejected: list[L.Rejected] = field(default_factory=list)
    flags: list[tuple[str, str, str, str]] = field(default_factory=list)
    broken_notes: list[tuple[str, str]] = field(default_factory=list)


def rebuild(vault: Vault, conn: sqlite3.Connection, trust: TrustOracle, today: str | None = None) -> RebuildReport:
    """Full rebuild in one transaction, so a concurrent reader sees the old
    snapshot or the new one. Measured 5–53 ms at 100–1,000 notes; there is
    deliberately no incremental path below ~2,000 notes."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    rep = RebuildReport()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for t in ("docs", "ids", "aliases", "subject_alias", "claims", "edges", "obs", "rkeys",
                  "vetoes", "subjects", "writespecs", "flags"):
            conn.execute(f"DELETE FROM {t}")
        _load_ledger(vault, conn, trust, rep)
        obs_by_ulid = {r["ulid"]: r for r in conn.execute("SELECT * FROM obs")}
        _load_notes(vault, conn, trust, rep, obs_by_ulid, today)
        _load_writespecs(vault, conn)
        _aggregate_subjects(conn, obs_by_ulid)
        for f in rep.flags:
            conn.execute("INSERT INTO flags(path, block, kind, detail) VALUES(?,?,?,?)", f)
        conn.execute("INSERT INTO claims_fts(claims_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO meta(k, v) VALUES('rebuilt_at', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return rep


def _load_ledger(vault: Vault, conn, trust, rep: RebuildReport) -> None:
    for rec in L.iter_observations(vault):
        if isinstance(rec, L.Rejected):
            rep.rejected.append(rec)
            continue
        o = rec
        tier = tier_for_obs(o, trust)
        conn.execute(
            "INSERT OR REPLACE INTO obs(ulid, day, ts, subject, facet, text, norm, src, op, cause, ref, due, until, tier, path, lineno) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.ulid, o.day, o.when.isoformat(timespec="minutes"), o.subject, o.facet, o.text, norm_text(o.text),
             o.src, o.op, o.cause, o.ref, o.due, o.until, tier, o.path, o.lineno),
        )
        for k in keys_for(o.subject, o.facet):
            conn.execute("INSERT INTO rkeys(ulid, key) VALUES(?,?)", (o.ulid, k))
        if o.op == "veto" and tier != "email":
            # A veto suppresses the CAUSE: every recurrence key named in ref,
            # comma-separated, for a year.
            until = o.until or _plus_days(o.day, 365)
            for key in [k for k in o.ref.split(",") if k]:
                conn.execute("INSERT OR REPLACE INTO vetoes(key, until, ulid) VALUES(?,?,?)", (key, until, o.ulid))
        rep.obs += 1


def _plus_days(day: str, n: int) -> str:
    from datetime import timedelta
    try:
        return (date.fromisoformat(day) + timedelta(days=n)).isoformat()
    except ValueError:
        return day


def _load_notes(vault: Vault, conn, trust, rep, obs_by_ulid, today: str) -> None:
    for path in vault.l2_notes():
        rel = vault.rel(path)
        try:
            note = parse_note(path)
        except Exception as e:  # one broken note must not wedge the pass
            rep.broken_notes.append((rel, str(e)[:200]))
            continue
        _index_note(vault, conn, trust, rep, note, rel, obs_by_ulid, today)
        rep.docs += 1
    # Retired tombstones: subject renames and retired flags.
    if vault.retired_dir.is_dir():
        for path in sorted(vault.retired_dir.rglob("*.md")):
            try:
                doc = parse_note(path)
            except Exception:
                continue
            frm, to = doc.meta.get("subject"), doc.meta.get("superseded_by")
            if frm and to:
                conn.execute("INSERT OR REPLACE INTO subject_alias(from_subject, to_subject) VALUES(?,?)", (str(frm), str(to)))
            conn.execute(
                "INSERT OR REPLACE INTO docs(path, type, title, mtime, sha, retired, export, nbytes) VALUES(?,?,?,?,?,1,0,?)",
                (vault.rel(path), "retired", doc.title, path.stat().st_mtime, sha_text(doc.raw), len(doc.raw.encode())),
            )


def _index_note(vault, conn, trust, rep, note: Note, rel: str, obs_by_ulid, today: str) -> None:
    d = rel.split("/", 1)[0]
    ntype = str(note.meta.get("type") or DIR_TO_TYPE.get(d) or d)
    export = 0 if note.meta.get("export") is False else 1
    due = str(note.meta.get("check_by") or note.meta.get("due") or "") or None
    about = str(note.meta.get("about") or "") or None
    doc_tier = str(note.meta.get("tier") or "") or None
    conn.execute(
        "INSERT OR REPLACE INTO docs(path, type, title, mtime, sha, due, export, created, about, tier, nbytes) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (rel, ntype, note.title, note.path.stat().st_mtime, sha_text(note.raw), due, export,
         str(note.meta.get("created") or "") or None, about, doc_tier, len(note.raw.encode())),
    )
    for i in note.meta.get("ids") or []:
        conn.execute("INSERT OR REPLACE INTO ids(id, doc) VALUES(?,?)", (str(i).strip().lower(), rel))
    for a in note.meta.get("aliases") or []:
        conn.execute("INSERT OR IGNORE INTO aliases(alias, doc) VALUES(?,?)", (str(a).strip().lower(), rel))
    conn.execute("INSERT OR IGNORE INTO aliases(alias, doc) VALUES(?,?)", (note.title.lower(), rel))
    for c in note.claims:
        _index_claim(conn, trust, rep, c, rel, obs_by_ulid, today, ntype)


def _index_claim(conn, trust, rep, c: Claim, doc: str, obs_by_ulid, today: str, ntype: str) -> None:
    # Evidence: which ledger lines back this claim, and at what tier.
    causes: set[str] = set()
    days: set[str] = set()
    tiers: list[int] = []
    n_support = 0
    owner_said = 0
    is_rule = False
    for e in c.edges:
        if e.rel not in ("derived-from", "owner-said") or not e.dst_doc.startswith("belt/ledger/"):
            continue
        o = obs_by_ulid.get(e.dst_block)
        if o is None:
            rep.flags.append((doc, c.block, "dangling-evidence", f"{e.dst_doc}#^{e.dst_block}"))
            continue
        if e.rel == "owner-said":
            # An owner-said edge counts only if the line is really owner-tier
            # AND was about this claim or this note — a forged edge pointing
            # at a real owner line is inert.
            # A rule line lands on a prefs note whose claim text IS the rule
            # text (both harness-written); an attest names the claim; anything
            # else must be about this note's own subject.
            about_this = (
                (o["ref"] == f"{doc}#^{c.block}")
                or (_note_for_subject(o["subject"]) == doc)
                or (o["op"] == "rule" and o["text"] == c.text and doc.startswith("prefs/"))
            )
            if o["tier"] == "owner" and about_this:
                owner_said = 1
                tiers.append(2)
                if o["op"] == "rule" and o["facet"] == "mail-disposition":
                    is_rule = True
            else:
                rep.flags.append((doc, c.block, "unverified-owner-edge", f"{e.dst_doc}#^{e.dst_block}"))
            continue
        n_support += 1
        causes.add(_cause_key_row(o))
        days.add(o["day"])
        tiers.append(TIER_RANK.get(o["tier"], 0))
    if c.has("owner-edited"):
        tiers.append(1)  # the owner touched it: at least session-tier, pinned
    tier = ["email", "session", "owner"][max(tiers) if tiers else (1 if c.has("owner-edited") else 0)]
    if n_support == 0 and not owner_said and not c.has("owner-edited"):
        # No evidence at all (hand-written without edges): the owner's word
        # in their own vault, session-tier, pinned by construction.
        tier = "session"
    declared = c.value("tier")
    if declared and declared != tier:
        rep.flags.append((doc, c.block, "tier-mismatch", f"declared {declared}, derived {tier}"))
    cls = "disposition" if is_rule else ("pref" if ntype == "pref" else "fact")
    row = {
        "retired": c.has("retired"), "superseded_by": c.has("superseded-by"),
        "contradicts": c.has("contradicts"), "until": c.value("until") or None,
        "owner_said": owner_said, "n_causes": len(causes), "n_days": len(days),
    }
    inbound: list[sqlite3.Row] = []  # inbound edges are resolved after all claims load; see _finish_status
    status = effective_status(row, inbound, today)
    first_seen = min((obs_by_ulid[b]["day"] for _, b in c.targets("derived-from") if b in obs_by_ulid), default=None)
    last_seen = max((obs_by_ulid[b]["day"] for _, b in c.targets("derived-from") if b in obs_by_ulid), default=None)
    score = score_for(bool(owner_said), len(causes), last_seen or "", status, today)
    conn.execute(
        "INSERT INTO claims(doc, block, text, sha, facet, cls, tier, n_support, n_causes, n_days, owner_said, pinned, "
        "first_seen, last_seen, until, folded, status, score, lineno) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc, c.block, c.text, c.sha, None, cls, tier, n_support, len(causes), len(days), owner_said,
         1 if (c.has("owner-edited") or c.minted) else 0, first_seen, last_seen, row["until"],
         1 if c.folded else 0, status, score, c.lineno),
    )
    for e in c.edges:
        conn.execute("INSERT INTO edges(src_doc, src_block, rel, dst_doc, dst_block, value) VALUES(?,?,?,?,?,?)",
                     (doc, c.block, e.rel, e.dst_doc or None, e.dst_block or None, e.value or None))
    rep.claims += 1


def _cause_key_row(o: sqlite3.Row) -> str:
    if o["src"] == "triage":
        return f"triage-day:{o['day']}"
    return o["cause"] or f"line:{o['ulid']}"


def _note_for_subject(subject: str) -> str:
    from wanda.memory.vault import TYPE_TO_DIR
    t, _, slug = subject.partition("/")
    d = TYPE_TO_DIR.get(t)
    return f"{d}/{slug}.md" if d else ""


def _load_writespecs(vault: Vault, conn) -> None:
    for p in vault.writespecs():
        try:
            ws = parse_writespec(p)
        except Exception:
            continue
        conn.execute("INSERT OR REPLACE INTO writespecs(path, prose, sha) VALUES(?,?,?)", (vault.rel(p), ws.prose, ws.sha))


def _aggregate_subjects(conn, obs_by_ulid) -> None:
    rows = conn.execute(
        "SELECT subject, COUNT(*) AS n, MIN(day) AS first_seen, MAX(day) AS last_seen, "
        "MAX(CASE WHEN tier <> 'email' THEN 1 ELSE 0 END) AS trusted FROM obs "
        "WHERE op IN ('', 'rule', 'attest') GROUP BY subject"
    ).fetchall()
    for r in rows:
        causes = {_cause_key_row(o) for o in conn.execute("SELECT * FROM obs WHERE subject=? AND op IN ('', 'rule', 'attest')", (r["subject"],))}
        days = {o["day"] for o in conn.execute("SELECT day FROM obs WHERE subject=?", (r["subject"],))}
        doc = _note_for_subject(r["subject"])
        exists = conn.execute("SELECT 1 FROM docs WHERE path=?", (doc,)).fetchone() is not None
        conn.execute(
            "INSERT OR REPLACE INTO subjects(key, doc, n_obs, n_causes, n_days, first_seen, last_seen, untrusted) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (r["subject"], doc if exists else None, r["n"], len(causes), len(days), r["first_seen"], r["last_seen"],
             0 if r["trusted"] else 1),
        )


# --- queries -------------------------------------------------------------------------

def doc_for_id(conn, ident: str) -> str | None:
    r = conn.execute("SELECT doc FROM ids WHERE id=?", (ident.lower(),)).fetchone()
    return r["doc"] if r else None


def live_claims(conn, doc: str, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM claims WHERE doc=? AND folded=0 AND status IN ('owner-stated','corroborated','provisional') "
        "ORDER BY score DESC, first_seen ASC, block ASC LIMIT ?", (doc, limit)).fetchall()


def standing_rules(conn, limit: int = 8) -> list[sqlite3.Row]:
    """Owner rules do not decay and tie-break on creation, so the oldest,
    most fundamental rules are never the first to fall out of the projection."""
    return conn.execute(
        "SELECT c.*, d.title FROM claims c JOIN docs d ON d.path=c.doc "
        "WHERE c.folded=0 AND c.owner_said=1 AND c.status='owner-stated' AND c.cls IN ('disposition','pref') "
        "ORDER BY c.pinned DESC, c.first_seen ASC, c.doc ASC, c.block ASC LIMIT ?", (limit,)).fetchall()


def due_soon(conn, today: str, limit: int = 5, horizon_days: int = 14, overdue_days: int = 30) -> list[sqlite3.Row]:
    from datetime import timedelta
    t = date.fromisoformat(today)
    hi = (t + timedelta(days=horizon_days)).isoformat()
    lo = (t - timedelta(days=overdue_days)).isoformat()
    return conn.execute(
        "SELECT path, title, due, about FROM docs WHERE type='open' AND retired=0 AND due IS NOT NULL "
        "AND due <= ? AND due >= ? AND COALESCE(tier,'session') <> 'email' ORDER BY due ASC LIMIT ?", (hi, lo, limit)).fetchall()


def roster(conn, today: str, limit: int = 12, window_days: int = 60) -> list[sqlite3.Row]:
    """Entities recently in play: one line each, ranked by recent activity
    then score. Titles come from closed sources (the note's title)."""
    from datetime import timedelta
    since = (date.fromisoformat(today) - timedelta(days=window_days)).isoformat()
    return conn.execute(
        "SELECT d.path, d.title, d.type, "
        " (SELECT COUNT(*) FROM obs o JOIN ids i ON i.doc=d.path WHERE o.day >= ? AND "
        "   (o.subject = replace(replace(d.path,'people/','person/'),'.md','') OR o.subject = replace(replace(d.path,'orgs/','org/'),'.md','') "
        "    OR o.subject = replace(replace(d.path,'topics/','topic/'),'.md',''))) AS recent, "
        " (SELECT MAX(score) FROM claims c WHERE c.doc=d.path AND c.folded=0 AND c.tier <> 'email') AS best "
        "FROM docs d WHERE d.type IN ('person','org','topic') AND d.retired=0 AND d.export=1 "
        "ORDER BY recent DESC, best DESC, d.title ASC LIMIT ?", (since, limit)).fetchall()


def top_claim(conn, doc: str, exclude_email: bool = True) -> sqlite3.Row | None:
    q = ("SELECT * FROM claims WHERE doc=? AND folded=0 AND status IN ('owner-stated','corroborated','provisional')"
         + (" AND tier <> 'email'" if exclude_email else "") + " ORDER BY score DESC, first_seen ASC, block ASC LIMIT 1")
    return conn.execute(q, (doc,)).fetchone()


FTS_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "be", "with", "at", "by",
            "from", "who", "what", "when", "where", "how", "does", "do", "did", "about", "this", "that", "it", "me", "my",
            "you", "your", "please", "can", "i", "we", "he", "she", "they"}


def fts(conn, query: str, limit: int = 10) -> list[sqlite3.Row]:
    """Any content word may match (OR), ranked by bm25 then score — a
    question rarely repeats a claim's exact words."""
    terms = [t for t in re.findall(r"[a-z0-9@.'-]+", query.lower()) if len(t) > 1 and t not in FTS_STOP]
    q = " OR ".join(f'"{t}"' for t in terms[:10])
    if not q:
        return []
    try:
        return conn.execute(
            "SELECT c.* FROM claims_fts f JOIN claims c ON c.id=f.rowid WHERE claims_fts MATCH ? AND c.folded=0 "
            "ORDER BY bm25(claims_fts), c.score DESC LIMIT ?", (q, limit)).fetchall()
    except sqlite3.OperationalError:
        return []


def subject_observations(conn, subject: str, since_day: str = "", limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM obs WHERE subject=? AND op IN ('', 'rule', 'attest') AND day >= ? ORDER BY ts DESC LIMIT ?",
        (subject, since_day, limit)).fetchall()


def all_subjects(conn) -> set[str]:
    out = {r["key"] for r in conn.execute("SELECT key FROM subjects")}
    for r in conn.execute("SELECT path FROM docs WHERE retired=0"):
        s = subject_for_doc(r["path"])
        if s:
            out.add(s)
    return out


def subject_aliases(conn) -> dict[str, str]:
    return {r["from_subject"]: r["to_subject"] for r in conn.execute("SELECT * FROM subject_alias")}


def subject_for_doc(path: str) -> str:
    d, _, rest = path.partition("/")
    t = DIR_TO_TYPE.get(d)
    if not t or not rest.endswith(".md"):
        return ""
    return f"{t}/{rest[:-3]}"


def is_vetoed(conn, keys: list[str], today: str) -> bool:
    if not keys:
        return False
    q = ",".join("?" * len(keys))
    r = conn.execute(f"SELECT 1 FROM vetoes WHERE key IN ({q}) AND until >= ? LIMIT 1", (*keys, today)).fetchone()
    return r is not None
