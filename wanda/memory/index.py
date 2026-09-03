"""memory.idx — a fully derived SQLite+FTS5 cache over the vault. Nothing
durable lives here: `rm memory.idx && wanda memory reindex` is always safe.
Trust is computed here from edges and from what the harness can verify,
never read from a label a writer wrote."""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from wanda.memory import ledger as L
from wanda.memory.notes import Claim, Note, parse_note, parse_writespec
from wanda.memory.subjects import keys_for, parse_subject
from wanda.memory.vault import DIR_TO_TYPE, LIVE_SQL, TYPE_TO_DIR, Vault, sha_text, slugify

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
CREATE INDEX IF NOT EXISTS ix_obs_group ON obs(subject, facet, norm);
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

TIERS = ("email", "session", "owner")
TIER_RANK = {"email": 0, "session": 1, "owner": 2}
CONVERSATION_KINDS = ("mention", "mention_guest", "dm")
OBS_OPS = ("", "rule", "attest")  # lines that carry content, as opposed to bookkeeping ops
STUB_KINDS = ("redirect", "tombstone")


class TrustOracle(Protocol):
    """What the harness can verify about a ledger line's origin. Backed by
    wanda.db and Slack in the daemon; a dict in tests."""

    def owner_verified(self, cause: str) -> bool: ...
    def line_checked(self, ulid: str) -> bool: ...
    def task_tier(self, task_id: int, when: datetime) -> str: ...
    def window_tier(self, when: datetime) -> str: ...


@dataclass
class DictTrust:
    verified_causes: set[str] = field(default_factory=set)
    task_kinds: dict[int, str] = field(default_factory=dict)
    checked_lines: set[str] | None = None      # None = every line under a verified cause counts
    email_windows: list[tuple[datetime, datetime]] = field(default_factory=list)

    def owner_verified(self, cause: str) -> bool:
        return cause in self.verified_causes

    def line_checked(self, ulid: str) -> bool:
        return True if self.checked_lines is None else ulid in self.checked_lines

    def task_tier(self, task_id: int, when: datetime) -> str:
        kind = self.task_kinds.get(task_id)
        return "session" if kind in CONVERSATION_KINDS else "email"

    def window_tier(self, when: datetime) -> str:
        return "email" if any(a <= when <= b for a, b in self.email_windows) else "session"


def tier_for_obs(o: L.Observation, trust: TrustOracle) -> str:
    """Provenance is assigned from what can be checked, not from `src=`:
    an owner line must point at a Slack message the owner really wrote AND
    have passed the per-line check against that message; an agent line is
    session-tier only if its task was a conversation with a run in flight;
    a shell-written line (cli:, import:, hand:) is email-tier whenever an
    email-task session was running at the time — the writer could have been
    that session, whatever it says about itself."""
    if o.src == "owner":
        ok = o.cause.startswith("slack:") and trust.owner_verified(o.cause) and trust.line_checked(o.ulid)
        return "owner" if ok else "session"
    if o.src == "agent":
        if o.cause.startswith("task:"):
            try:
                return trust.task_tier(int(o.cause[5:]), o.when)
            except ValueError:
                return "email"
        return "email"
    if o.src == "triage":
        return "email"
    return trust.window_tier(o.when)  # harness, import


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


def cause_key(src: str, cause: str, day: str, ulid: str) -> str:
    """At most one triage memo per subject/facet/day counts as a cause, so a
    chatty sender cannot manufacture consensus in an afternoon."""
    if src == "triage":
        return f"triage-day:{day}"
    return cause or f"line:{ulid}"


def note_for_subject(subject: str) -> str:
    t, _, slug = subject.partition("/")
    d = TYPE_TO_DIR.get(t)
    return f"{d}/{slug}.md" if d else ""


def subject_for_doc(path: str) -> str:
    """`people/Robin Vale.md` -> `person/robin-vale`: a hand-named file
    still maps to a valid subject key."""
    d, _, rest = path.partition("/")
    t = DIR_TO_TYPE.get(d)
    if not t or not rest.endswith(".md") or "/" in rest:
        return ""
    slug = rest[:-3].lower()
    key = f"{t}/{slug}"
    if parse_subject(key) is None:
        key = f"{t}/{slugify(slug)}"
    return key if parse_subject(key) else ""


def effective_status(c: dict, today: str) -> str:
    """`c` carries the claim's own flags plus `inbound_supersedes` and
    `inbound_contradicts`, resolved after every claim is loaded."""
    if c.get("retired"):
        return "retired"
    if c.get("superseded_by") or c.get("inbound_supersedes"):
        return "superseded"
    if c.get("until") and c["until"] < today:
        return "expired"
    if c.get("contradicts") or c.get("inbound_contradicts"):
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
        _load_notes(vault, conn, rep, obs_by_ulid, today)
        _finish_status(conn, today)
        _load_writespecs(vault, conn)
        _aggregate_subjects(conn)
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


def _obs_row(o: L.Observation, tier: str) -> tuple:
    return (o.ulid, o.day, o.when.isoformat(timespec="minutes"), o.subject, o.facet, o.text, norm_text(o.text),
            o.src, o.op, o.cause, o.ref, o.due, o.until, tier, o.path, o.lineno)


_OBS_INSERT = ("INSERT OR REPLACE INTO obs(ulid, day, ts, subject, facet, text, norm, src, op, cause, ref, due, until, tier, path, lineno) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _load_ledger(vault: Vault, conn, trust, rep: RebuildReport) -> None:
    for rec in L.iter_observations(vault):
        if isinstance(rec, L.Rejected):
            rep.rejected.append(rec)
            continue
        _insert_obs(conn, rec, tier_for_obs(rec, trust))
        rep.obs += 1


def _insert_obs(conn, o: L.Observation, tier: str) -> None:
    conn.execute(_OBS_INSERT, _obs_row(o, tier))
    conn.execute("DELETE FROM rkeys WHERE ulid=?", (o.ulid,))
    for k in keys_for(o.subject, o.facet):
        conn.execute("INSERT INTO rkeys(ulid, key) VALUES(?,?)", (o.ulid, k))
    if o.op == "veto" and tier != "email":
        # A veto suppresses the CAUSE: every recurrence key named in ref,
        # comma-separated, for a year.
        until = o.until or _plus_days(o.day, 365)
        for key in [k for k in o.ref.split(",") if k]:
            conn.execute("INSERT OR REPLACE INTO vetoes(key, until, ulid) VALUES(?,?,?)", (key, until, o.ulid))


def insert_observation(conn: sqlite3.Connection, o: L.Observation, tier: str) -> None:
    """Zero-lag path for `wanda memory note`: the line just appended becomes
    retrievable now; the hourly rebuild reconciles everything else."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _insert_obs(conn, o, tier)
        _aggregate_subject(conn, o.subject)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _plus_days(day: str, n: int) -> str:
    try:
        return (date.fromisoformat(day) + timedelta(days=n)).isoformat()
    except ValueError:
        return day


def _load_notes(vault: Vault, conn, rep, obs_by_ulid, today: str) -> None:
    seen_ids: dict[str, str] = {}
    for path in vault.l2_notes():
        rel = vault.rel(path)
        try:
            note = parse_note(path)
        except Exception as e:  # one broken note must not wedge the pass
            rep.broken_notes.append((rel, str(e)[:200]))
            continue
        if note.kind in STUB_KINDS:
            # A redirect left at an old path: remember where it points, index nothing.
            to = str(note.meta.get("superseded_by") or "")
            frm, dst = subject_for_doc(rel), subject_for_doc(to)
            if frm and dst:
                conn.execute("INSERT OR REPLACE INTO subject_alias(from_subject, to_subject) VALUES(?,?)", (frm, dst))
            continue
        _index_note(conn, rep, note, rel, obs_by_ulid, today, seen_ids)
        rep.docs += 1
    # Retired tombstones: subject renames and retired flags.
    if vault.retired_dir.is_dir():
        for path in sorted(vault.retired_dir.rglob("*.md")):
            if "history" in path.relative_to(vault.retired_dir).parts[:1]:
                continue
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


def _index_note(conn, rep, note: Note, rel: str, obs_by_ulid, today: str, seen_ids: dict[str, str]) -> None:
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
        ident = str(i).strip().lower()
        if ident in seen_ids and seen_ids[ident] != rel:
            rep.flags.append((rel, "", "duplicate-id", f"{ident} also on {seen_ids[ident]}"))
        seen_ids[ident] = rel
        conn.execute("INSERT OR REPLACE INTO ids(id, doc) VALUES(?,?)", (ident, rel))
    subject = subject_for_doc(rel)
    for a in note.meta.get("aliases") or []:
        alias = str(a).strip().lower()
        conn.execute("INSERT OR IGNORE INTO aliases(alias, doc) VALUES(?,?)", (alias, rel))
        # `aliases: [RLA]` on orgs/riverside-language-academy also makes the
        # subject key org/rla resolve there.
        t = subject.partition("/")[0]
        if t and slugify(alias):
            conn.execute("INSERT OR IGNORE INTO subject_alias(from_subject, to_subject) VALUES(?,?)",
                         (f"{t}/{slugify(alias)}", subject))
    conn.execute("INSERT OR IGNORE INTO aliases(alias, doc) VALUES(?,?)", (note.title.lower(), rel))
    for c in note.claims:
        _index_claim(conn, rep, c, rel, obs_by_ulid, today, ntype)


def _index_claim(conn, rep, c: Claim, doc: str, obs_by_ulid, today: str, ntype: str) -> None:
    # Evidence: which ledger lines back this claim, and at what tier.
    witness_groups: set[tuple[str, str, str]] = set()
    tiers: list[int] = []
    owner_said = 0
    is_rule = False
    owner_day = None
    for e in c.edges:
        if e.rel not in ("derived-from", "owner-said") or not e.dst_doc.startswith("belt/ledger/"):
            continue
        o = obs_by_ulid.get(e.dst_block)
        if o is None:
            rep.flags.append((doc, c.block, "dangling-evidence", f"{e.dst_doc}#^{e.dst_block}"))
            continue
        if e.rel == "owner-said":
            # An owner-said edge counts only if the line is really owner-tier
            # AND is about this claim: an attest names the claim by ref; a
            # rule's text IS the claim text on a prefs note (both harness
            # written); anything else must be the note's own subject — and
            # a rule line never confers authority on a claim it did not word.
            if o["tier"] != "owner":
                rep.flags.append((doc, c.block, "unverified-owner-edge", f"{e.dst_doc}#^{e.dst_block}"))
                continue
            if o["op"] == "rule":
                if o["text"] == c.text and doc.startswith("prefs/"):
                    owner_said = 1
                    if o["facet"] == "mail-disposition":
                        is_rule = True
                else:
                    rep.flags.append((doc, c.block, "unverified-owner-edge", f"rule text mismatch {e.dst_doc}#^{e.dst_block}"))
                    continue
            elif o["ref"] == f"{doc}#^{c.block}" or note_for_subject(o["subject"]) == doc:
                owner_said = 1
            else:
                rep.flags.append((doc, c.block, "unverified-owner-edge", f"{e.dst_doc}#^{e.dst_block}"))
                continue
            tiers.append(2)
            owner_day = min(owner_day or o["day"], o["day"])
            continue
        witness_groups.add((o["subject"], o["facet"], o["norm"]))
        tiers.append(TIER_RANK.get(o["tier"], 0))
    # Support is counted from the ledger groups the witnesses belong to, so
    # capping derived-from at three refs loses nothing.
    n_support, causes, days, first_seen, last_seen = _group_support(conn, witness_groups)
    if c.has("owner-edited"):
        tiers.append(1)  # the owner touched it: at least session-tier, pinned
    hand_written = n_support == 0 and not owner_said
    tier = TIERS[max(tiers)] if tiers else "session"
    if hand_written:
        tier = "session"  # the owner's own words in their own vault
    declared = c.value("tier")
    if declared and declared != tier:
        rep.flags.append((doc, c.block, "tier-mismatch", f"declared {declared}, derived {tier}"))
    cls = "disposition" if is_rule else ("pref" if ntype == "pref" else "fact")
    row = {
        "retired": c.has("retired"), "superseded_by": c.has("superseded-by"),
        "contradicts": c.has("contradicts"), "until": c.value("until") or None,
        "owner_said": owner_said, "n_causes": len(causes), "n_days": len(days),
    }
    status = effective_status(row, today)
    first_seen = first_seen or owner_day
    score = score_for(bool(owner_said), len(causes), last_seen or "", status, today)
    conn.execute(
        "INSERT INTO claims(doc, block, text, sha, facet, cls, tier, n_support, n_causes, n_days, owner_said, pinned, "
        "first_seen, last_seen, until, folded, status, score, lineno) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc, c.block, c.text, c.sha, None, cls, tier, n_support, len(causes), len(days), owner_said,
         1 if (c.has("owner-edited") or c.minted or hand_written) else 0, first_seen, last_seen, row["until"],
         1 if c.folded else 0, status, score, c.lineno),
    )
    for e in c.edges:
        conn.execute("INSERT INTO edges(src_doc, src_block, rel, dst_doc, dst_block, value) VALUES(?,?,?,?,?,?)",
                     (doc, c.block, e.rel, e.dst_doc or None, e.dst_block or None, e.value or None))
    rep.claims += 1


def _group_support(conn, groups: set[tuple[str, str, str]]):
    causes: set[str] = set()
    days: set[str] = set()
    n = 0
    first = last = None
    for subject, facet, norm in groups:
        for o in conn.execute("SELECT src, cause, day, ulid FROM obs WHERE subject=? AND facet=? AND norm=? AND op IN ('', 'rule', 'attest')",
                              (subject, facet, norm)):
            n += 1
            causes.add(cause_key(o["src"], o["cause"], o["day"], o["ulid"]))
            days.add(o["day"])
            first = min(first or o["day"], o["day"])
            last = max(last or o["day"], o["day"])
    return n, causes, days, first, last


def _finish_status(conn, today: str) -> None:
    """Inbound edges: a claim someone else `supersedes` or `contradicts` is
    superseded / disputed even if only the winner's side was written."""
    inbound: dict[tuple[str, str], set[str]] = {}
    for e in conn.execute("SELECT rel, dst_doc, dst_block FROM edges WHERE rel IN ('supersedes','contradicts') AND dst_block IS NOT NULL"):
        dst = e["dst_doc"] if e["dst_doc"].endswith(".md") else e["dst_doc"] + ".md"
        inbound.setdefault((dst, e["dst_block"]), set()).add(e["rel"])
    for (doc, block), rels in inbound.items():
        c = conn.execute("SELECT * FROM claims WHERE doc=? AND block=?", (doc, block)).fetchone()
        if c is None:
            continue
        row = {
            "retired": c["status"] == "retired", "superseded_by": c["status"] == "superseded",
            "contradicts": c["status"] == "disputed", "until": c["until"], "owner_said": c["owner_said"],
            "n_causes": c["n_causes"], "n_days": c["n_days"],
            "inbound_supersedes": "supersedes" in rels, "inbound_contradicts": "contradicts" in rels,
        }
        status = effective_status(row, today)
        if status != c["status"]:
            conn.execute("UPDATE claims SET status=?, score=? WHERE id=?",
                         (status, score_for(bool(c["owner_said"]), c["n_causes"] or 0, c["last_seen"] or "", status, today), c["id"]))
    # Marking the loser disputed also marks the winner disputed (both stay, both ranked last).
    for e in conn.execute("SELECT src_doc, src_block FROM edges WHERE rel='contradicts'"):
        c = conn.execute("SELECT * FROM claims WHERE doc=? AND block=?", (e["src_doc"], e["src_block"])).fetchone()
        if c is not None and c["status"] in ("provisional", "corroborated", "owner-stated"):
            conn.execute("UPDATE claims SET status='disputed', score=? WHERE id=?",
                         (score_for(bool(c["owner_said"]), c["n_causes"] or 0, c["last_seen"] or "", "disputed", today), c["id"]))


def _load_writespecs(vault: Vault, conn) -> None:
    for p in vault.writespecs():
        try:
            ws = parse_writespec(p)
        except Exception:
            continue
        conn.execute("INSERT OR REPLACE INTO writespecs(path, prose, sha) VALUES(?,?,?)", (vault.rel(p), ws.prose, ws.sha))


def _aggregate_subjects(conn) -> None:
    for r in conn.execute("SELECT DISTINCT subject FROM obs").fetchall():
        _aggregate_subject(conn, r["subject"])


def _aggregate_subject(conn, subject: str) -> None:
    rows = conn.execute("SELECT src, cause, day, ulid, tier FROM obs WHERE subject=? AND op IN ('', 'rule', 'attest')", (subject,)).fetchall()
    if not rows:
        conn.execute("DELETE FROM subjects WHERE key=?", (subject,))
        return
    causes = {cause_key(o["src"], o["cause"], o["day"], o["ulid"]) for o in rows}
    days = {o["day"] for o in rows}
    trusted = any(o["tier"] != "email" for o in rows)
    doc = note_for_subject(subject)
    exists = conn.execute("SELECT 1 FROM docs WHERE path=? AND retired=0", (doc,)).fetchone() is not None
    conn.execute(
        "INSERT OR REPLACE INTO subjects(key, doc, n_obs, n_causes, n_days, first_seen, last_seen, untrusted) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (subject, doc if exists else None, len(rows), len(causes), len(days), min(days), max(days), 0 if trusted else 1),
    )


# --- queries -------------------------------------------------------------------------

def doc_for_id(conn, ident: str) -> str | None:
    r = conn.execute("SELECT doc FROM ids WHERE id=?", (ident.lower(),)).fetchone()
    return r["doc"] if r else None


def live_claims(conn, doc: str, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM claims WHERE doc=? AND folded=0 AND status IN {LIVE_SQL} "
        "ORDER BY score DESC, first_seen ASC, lineno ASC LIMIT ?", (doc, limit)).fetchall()


def standing_rules(conn, limit: int = 8) -> list[sqlite3.Row]:
    """Owner rules do not decay and tie-break on when they were first said,
    so the oldest, most fundamental rules are never the first to fall out of
    the projection."""
    return conn.execute(
        "SELECT c.*, d.title FROM claims c JOIN docs d ON d.path=c.doc "
        "WHERE c.folded=0 AND c.owner_said=1 AND c.status='owner-stated' AND c.cls IN ('disposition','pref') "
        "ORDER BY c.pinned DESC, c.first_seen ASC, c.doc ASC, c.lineno ASC LIMIT ?", (limit,)).fetchall()


def dispositions_for(conn, addrs: list[str], domains: list[str]) -> list[sqlite3.Row]:
    """Every live owner disposition that names one of these addresses or
    registrable domains — however many rules exist."""
    out = []
    for r in conn.execute("SELECT * FROM claims WHERE cls='disposition' AND folded=0 AND status='owner-stated' ORDER BY first_seen ASC, lineno ASC"):
        m = re.match(r"^(trash|ignore|attention) mail from (\S+?)(:|$)", r["text"])
        if not m:
            continue
        target = m.group(2)
        if target in addrs or target in domains:
            out.append(r)
    return out


def due_soon(conn, today: str, limit: int = 5, horizon_days: int = 14, overdue_days: int = 30) -> list[sqlite3.Row]:
    t = date.fromisoformat(today)
    hi = (t + timedelta(days=horizon_days)).isoformat()
    lo = (t - timedelta(days=overdue_days)).isoformat()
    return conn.execute(
        "SELECT path, title, due, about FROM docs WHERE type='open' AND retired=0 AND due IS NOT NULL "
        "AND due <= ? AND due >= ? AND COALESCE(tier,'session') <> 'email' ORDER BY due ASC LIMIT ?", (hi, lo, limit)).fetchall()


def roster(conn, today: str, limit: int = 12, window_days: int = 60) -> list[sqlite3.Row]:
    """Entities recently in play: ranked by observations on their subject in
    the window, then by best claim score, then title. Titles come from closed
    sources (the note's title)."""
    since = (date.fromisoformat(today) - timedelta(days=window_days)).isoformat()
    docs = conn.execute(
        "SELECT d.path, d.title, d.type, "
        " (SELECT MAX(score) FROM claims c WHERE c.doc=d.path AND c.folded=0 AND c.tier <> 'email') AS best "
        "FROM docs d WHERE d.type IN ('person','org','topic') AND d.retired=0 AND d.export=1").fetchall()
    scored = []
    for d in docs:
        subj = subject_for_doc(d["path"])
        recent = conn.execute("SELECT COUNT(*) FROM obs WHERE subject=? AND day >= ? AND op IN ('', 'rule', 'attest')",
                              (subj, since)).fetchone()[0] if subj else 0
        scored.append((recent, d["best"] or 0.0, d["title"].lower(), d))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [x[3] for x in scored[:limit]]


def top_claim(conn, doc: str, exclude_email: bool = True) -> sqlite3.Row | None:
    q = (f"SELECT * FROM claims WHERE doc=? AND folded=0 AND status IN {LIVE_SQL}"
         + (" AND tier <> 'email'" if exclude_email else "") + " ORDER BY score DESC, first_seen ASC, lineno ASC LIMIT 1")
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
    """Keys that exist: notes and belt subjects, minus keys that now alias
    to another (retired or merged)."""
    aliased = {r["from_subject"] for r in conn.execute("SELECT from_subject FROM subject_alias")}
    out = {r["key"] for r in conn.execute("SELECT key FROM subjects")}
    for r in conn.execute("SELECT path FROM docs WHERE retired=0"):
        s = subject_for_doc(r["path"])
        if s:
            out.add(s)
    return out - aliased


def subject_aliases(conn) -> dict[str, str]:
    return {r["from_subject"]: r["to_subject"] for r in conn.execute("SELECT * FROM subject_alias")}


def canonical_subject(conn, subject: str) -> str:
    """Follow aliases (bounded), so a memo about a retired key files under
    its successor."""
    seen = set()
    while subject not in seen:
        seen.add(subject)
        r = conn.execute("SELECT to_subject FROM subject_alias WHERE from_subject=?", (subject,)).fetchone()
        if r is None:
            break
        subject = r["to_subject"]
    return subject


def is_vetoed(conn, keys: list[str], today: str) -> bool:
    if not keys:
        return False
    q = ",".join("?" * len(keys))
    r = conn.execute(f"SELECT 1 FROM vetoes WHERE key IN ({q}) AND until >= ? LIMIT 1", (*keys, today)).fetchone()
    return r is not None
