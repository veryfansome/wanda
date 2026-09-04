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
from wanda.memory.ledger import line_fingerprint
from wanda.memory.notes import Claim, Note, parse_note, parse_writespec
from wanda.memory.subjects import keys_for, parse_subject, registrable_domain
from wanda.memory.vault import (
    DIR_TO_TYPE, GRADUATE_CAUSES, GRADUATE_DAYS, LIVE_SQL, OBS_OPS_SQL, TYPE_TO_DIR, Vault, sha_text, slugify,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, subject TEXT, type TEXT, title TEXT, mtime REAL, sha TEXT,
  due TEXT, export INTEGER NOT NULL DEFAULT 1, created TEXT, retired INTEGER NOT NULL DEFAULT 0,
  about TEXT, tier TEXT, nbytes INTEGER);
CREATE INDEX IF NOT EXISTS ix_docs_subject ON docs(subject);
CREATE TABLE IF NOT EXISTS ids (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases (alias TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY(alias, doc));
CREATE TABLE IF NOT EXISTS subject_alias (from_subject TEXT PRIMARY KEY, to_subject TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY, doc TEXT NOT NULL, block TEXT NOT NULL, text TEXT NOT NULL, sha TEXT NOT NULL,
  cls TEXT NOT NULL, tier TEXT NOT NULL, n_support INTEGER, n_causes INTEGER, n_days INTEGER,
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
  first_seen TEXT, last_seen TEXT, untrusted INTEGER);
CREATE TABLE IF NOT EXISTS rules (subject TEXT, facet TEXT, target TEXT, action TEXT, text TEXT, ledger_ref TEXT,
  created TEXT, doc TEXT, block TEXT);
CREATE TABLE IF NOT EXISTS writespecs (path TEXT PRIMARY KEY, prose TEXT, sha TEXT);
CREATE TABLE IF NOT EXISTS flags (path TEXT, block TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

TIERS = ("email", "session", "owner")
TIER_RANK = {"email": 0, "session": 1, "owner": 2}
CONVERSATION_KINDS = ("mention", "mention_guest", "dm")
STUB_KINDS = ("redirect", "tombstone")
DISPOSITION_RE = re.compile(r"^(trash|ignore|attention) mail from (\S+?)(:|$)", re.I)  # the writer is lowercase (commands.rule_text); the reader must not silently drop a cased line


class TrustOracle(Protocol):
    """What the harness can verify about a ledger line's origin. Backed by
    wanda.db, the daemon's memory and Slack in the daemon; a dict in tests."""

    def owner_verified(self, cause: str) -> bool: ...
    def line_checked(self, ulid: str, fp: str) -> bool: ...
    def window_tier(self, when: datetime) -> str: ...
    def line_authored(self, ulid: str, fp: str) -> bool: ...


def tier_for_obs(o: L.Observation, trust: TrustOracle) -> str:
    """Provenance is assigned from what can be checked, not from `src=`.

    - owner: the line must point at a Slack message the owner really wrote
      AND have passed the per-line check against that message; otherwise it
      is treated like any shell-written line.
    - triage: email, always.
    - anything written from a shell (agent, harness, import): the tier is the
      kind of the agent run whose recorded window covers the line's timestamp
      (trust.window_tier), never anything in the line itself; a line the
      daemon authored during its own pass is trusted via line_authored. A
      line covered by an email-task window is email tier, since it could have
      been that session."""
    fp = line_fingerprint(o)
    if o.src == "owner":
        if o.cause.startswith("slack:") and trust.owner_verified(o.cause) and trust.line_checked(o.ulid, fp):
            return "owner"
        return trust.window_tier(o.when)
    if o.src == "triage":
        return "email"
    if getattr(trust, "line_authored", lambda u, f: False)(o.ulid, fp):
        return "session"  # the daemon wrote this line itself during a pass
    # Any other shell-written line (agent, harness, import): attributed by the
    # agent-run windows the HARNESS recorded, never by anything in the line —
    # a session cannot forge which run was in flight when it wrote.
    return trust.window_tier(o.when)


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


def doc_for_subject(conn, subject: str) -> str | None:
    """The live note for a subject, hand-named files included (`docs.subject`
    is derived from the path), following aliases."""
    subject = canonical_subject(conn, subject)
    r = conn.execute("SELECT path FROM docs WHERE subject=? AND retired=0 ORDER BY path", (subject,)).fetchone()
    return r["path"] if r else None


def effective_status(c: dict, today: str) -> str:
    """`c` carries the claim's own flags plus `inbound_supersedes` and
    `inbound_contradicts`, resolved after every claim is loaded. The owner's
    word cannot be disputed by anything less than the owner's word: a
    contradiction only marks non-owner claims. A claim carrying its own
    `contradicts` edge is disputed by that edge alone, so marking a loser
    disputed leaves both sides disputed: both stay, both rank last."""
    if c.get("retired"):
        return "retired"
    if c.get("superseded_by") or c.get("inbound_supersedes"):
        return "superseded"
    if c.get("until") and c["until"] < today:
        return "expired"
    if c.get("owner_said"):
        return "owner-stated"
    if c.get("contradicts") or c.get("inbound_contradicts"):
        return "disputed"
    if (c.get("n_causes") or 0) >= GRADUATE_CAUSES and (c.get("n_days") or 0) >= GRADUATE_DAYS:
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
    snapshot or the new one. Measured warm on an Apple-silicon laptop at
    ~25 ms for 100 notes and ~370 ms for 1,000 (5 claims and 3 observations
    per note); there is deliberately no incremental path below ~2,000 notes."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    rep = RebuildReport()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for t in ("docs", "ids", "aliases", "subject_alias", "claims", "edges", "obs", "rkeys",
                  "vetoes", "subjects", "writespecs", "rules", "flags"):
            conn.execute(f"DELETE FROM {t}")
        _load_ledger(vault, conn, trust, rep)
        obs_by_ulid = {r["ulid"]: r for r in conn.execute("SELECT * FROM obs")}
        _load_notes(vault, conn, trust, rep, obs_by_ulid, today)
        _finish_status(conn, today)
        _load_writespecs(vault, conn)
        _derive_owner_rules(conn)
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


_OBS_INSERT = ("INSERT OR REPLACE INTO obs(ulid, day, ts, subject, facet, text, norm, src, op, cause, ref, due, until, tier, path, lineno) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _load_ledger(vault: Vault, conn, trust, rep: RebuildReport) -> None:
    seen_ulids: set[str] = set()
    for rec in L.iter_observations(vault):
        if isinstance(rec, L.Rejected):
            rep.rejected.append(rec)
            continue
        if rec.ulid in seen_ulids:
            # A ULID is unique by construction; a second line reusing one is a
            # forgery or corruption. Keep the first, reject the rest — never
            # let a later line overwrite an earlier one's obs row.
            rep.rejected.append(L.Rejected(rec.path, rec.lineno, L.format_line(rec), f"duplicate block id ^{rec.ulid}"))
            continue
        seen_ulids.add(rec.ulid)
        _insert_obs(conn, rec, tier_for_obs(rec, trust))
        rep.obs += 1


def _insert_obs(conn, o: L.Observation, tier: str) -> None:
    conn.execute(_OBS_INSERT, (o.ulid, o.day, o.when.isoformat(timespec="minutes"), o.subject, o.facet, o.text,
                               norm_text(o.text), o.src, o.op, o.cause, o.ref, o.due, o.until, tier, o.path, o.lineno))
    conn.execute("DELETE FROM rkeys WHERE ulid=?", (o.ulid,))
    for k in keys_for(o.subject, o.facet):
        conn.execute("INSERT INTO rkeys(ulid, key) VALUES(?,?)", (o.ulid, k))
    if o.op == "veto" and tier != "email":
        # A veto suppresses the CAUSE: every recurrence key named in ref,
        # comma-separated, for a year from the line's own day - never longer,
        # and never shorter than a suppression already standing on that key.
        # A line's own `until` cannot reach past that cap, so back-dating a
        # forged veto line no longer buys a fresh year.
        cap = _plus_days(o.day, 365)
        until = min(o.until, cap) if o.until else cap
        for key in [k for k in o.ref.split(",") if k]:
            conn.execute("INSERT INTO vetoes(key, until, ulid) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
                         "ulid=CASE WHEN excluded.until > vetoes.until THEN excluded.ulid ELSE vetoes.ulid END, "
                         "until=MAX(vetoes.until, excluded.until)", (key, until, o.ulid))


def insert_observation(conn: sqlite3.Connection, o: L.Observation, tier: str) -> None:
    """Zero-lag path for a line just appended: `wanda memory note/open/pin/
    forget` and the owner ops `apply_now` applies. The line is retrievable at
    once, and a `forget`'s veto starts suppressing its recurrence keys at once
    (unless an email-task window covers the line, which makes it email tier
    and installs nothing) - not at the next rebuild. Everything else waits for
    the hourly rebuild."""
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


def _load_notes(vault: Vault, conn, trust, rep, obs_by_ulid, today: str) -> None:
    seen_ids: dict[str, str] = {}
    seen_subjects: dict[str, str] = {}
    open_lines = {r["ref"]: r for r in conn.execute("SELECT ref, tier FROM obs WHERE op='open'")}
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
        n_claims, n_flags = rep.claims, len(rep.flags)
        conn.execute("SAVEPOINT note")
        try:
            _index_note(conn, trust, rep, note, rel, obs_by_ulid, today, seen_ids, seen_subjects, open_lines)
        except Exception as e:
            # One note's rows, never the whole index: the note is skipped and
            # named, exactly as a parse failure above is.
            conn.execute("ROLLBACK TO note")
            rep.claims = n_claims
            del rep.flags[n_flags:]
            rep.broken_notes.append((rel, f"indexing failed: {str(e)[:180]}"))
            continue
        finally:
            conn.execute("RELEASE note")
        rep.docs += 1
    # Retired tombstones: subject renames and retired flags.
    if vault.retired_dir.is_dir():
        for path in sorted(vault.retired_dir.rglob("*.md")):
            if path.relative_to(vault.retired_dir).parts[:1] == ("history",):
                continue
            try:
                doc = parse_note(path)
                mt = path.stat().st_mtime
            except Exception as e:
                rep.broken_notes.append((vault.rel(path), str(e)[:200]))
                continue
            frm, to = doc.meta.get("subject"), doc.meta.get("superseded_by")
            if frm and to:
                conn.execute("INSERT OR REPLACE INTO subject_alias(from_subject, to_subject) VALUES(?,?)", (str(frm), str(to)))
            conn.execute(
                "INSERT OR REPLACE INTO docs(path, type, title, mtime, sha, retired, export, nbytes) VALUES(?,?,?,?,?,1,0,?)",
                (vault.rel(path), "retired", doc.title, mt, sha_text(doc.raw), len(doc.raw.encode())),
            )


def _index_note(conn, trust, rep, note: Note, rel: str, obs_by_ulid, today: str, seen_ids: dict[str, str],
                seen_subjects: dict[str, str], open_lines) -> None:
    d = rel.split("/", 1)[0]
    ntype = str(note.meta.get("type") or DIR_TO_TYPE.get(d) or d)
    ex = note.meta.get("export")
    # A note the owner meant to withhold. Only bare false/no arrive as a bool
    # (vault._scalar); off arrives as text, 0 as an int, and every quoted form
    # as text - all of which used to export anyway. An absent key exports.
    export = 0 if ex in (False, 0) or (isinstance(ex, str) and ex.strip().lower() in ("false", "no", "off", "0")) else 1
    due = str(note.meta.get("check_by") or note.meta.get("due") or "") or None
    about = str(note.meta.get("about") or "") or None
    mtime = note.path.stat().st_mtime
    when = datetime.fromtimestamp(mtime, tz=timezone.utc)
    subject = subject_for_doc(rel)
    if subject:
        # Two hand-named files can slugify to one subject key. The index has
        # always flagged duplicate ids; a duplicate subject is the same class
        # of collision and was silent.
        if subject in seen_subjects and seen_subjects[subject] != rel:
            rep.flags.append((rel, "", "duplicate-subject", f"{subject} also on {seen_subjects[subject]}"))
        seen_subjects[subject] = rel
    doc_tier = None
    if ntype == "open":
        # Derived, not declared: the tier of the `op=open` ledger line that
        # created it, or — for a hand-written item — what was running when
        # the file was last written.
        line = open_lines.get(rel)
        doc_tier = line["tier"] if line else trust.window_tier(when)
    conn.execute(
        "INSERT OR REPLACE INTO docs(path, subject, type, title, mtime, sha, due, export, created, about, tier, nbytes) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (rel, subject or None, ntype, note.title, mtime, sha_text(note.raw), due, export,
         str(note.meta.get("created") or "") or None, about, doc_tier, len(note.raw.encode())),
    )
    for i in note.meta.get("ids") or []:
        ident = str(i).strip().lower()
        if ident in seen_ids and seen_ids[ident] != rel:
            rep.flags.append((rel, "", "duplicate-id", f"{ident} also on {seen_ids[ident]}"))
        seen_ids[ident] = rel
        conn.execute("INSERT OR REPLACE INTO ids(id, doc) VALUES(?,?)", (ident, rel))
        # An identifier on a note is also the address-keyed subject the belt
        # files under: person/<addr> → this note's subject.
        if subject:
            kind, _, value = ident.partition(":")
            if kind == "mailto" and value and value != subject.partition("/")[2]:
                conn.execute("INSERT OR IGNORE INTO subject_alias(from_subject, to_subject) VALUES(?,?)", (f"person/{value}", subject))
            elif kind in ("dom", "list") and value:
                conn.execute("INSERT OR IGNORE INTO subject_alias(from_subject, to_subject) VALUES(?,?)",
                             (f"org/{registrable_domain(value)}", subject))
    for a in note.meta.get("aliases") or []:
        alias = str(a).strip().lower()
        conn.execute("INSERT OR IGNORE INTO aliases(alias, doc) VALUES(?,?)", (alias, rel))
        # `aliases: [RLA]` on orgs/riverside-language-academy also makes the
        # subject key org/rla resolve there.
        t = subject.partition("/")[0]
        if t and slugify(alias) and f"{t}/{slugify(alias)}" != subject:
            conn.execute("INSERT OR IGNORE INTO subject_alias(from_subject, to_subject) VALUES(?,?)",
                         (f"{t}/{slugify(alias)}", subject))
    conn.execute("INSERT OR IGNORE INTO aliases(alias, doc) VALUES(?,?)", (note.title.lower(), rel))
    seen_blocks: set[str] = set()
    for c in note.claims:
        if c.block in seen_blocks:
            # A repeated ^block id would violate UNIQUE(doc, block) and abort
            # the whole rebuild. Index the first line, flag the rest.
            rep.flags.append((rel, c.block, "duplicate-block", "repeated block id; only the first line is indexed"))
            continue
        seen_blocks.add(c.block)
        _index_claim(conn, trust, rep, c, rel, obs_by_ulid, today, ntype, when)


def _index_claim(conn, trust, rep, c: Claim, doc: str, obs_by_ulid, today: str, ntype: str, file_when: datetime) -> None:
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
    hand_written = n_support == 0 and not owner_said
    if c.has("owner-edited") or hand_written:
        # The owner's own words in their own vault — unless an email-task
        # session was running when the file was written, in which case a
        # shell could have typed them.
        tiers.append(TIER_RANK[trust.window_tier(file_when)])
    tier = TIERS[max(tiers)] if tiers else "session"
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
        "INSERT INTO claims(doc, block, text, sha, cls, tier, n_support, n_causes, n_days, owner_said, pinned, "
        "first_seen, last_seen, until, folded, status, score, lineno) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc, c.block, c.text, c.sha, cls, tier, n_support, len(causes), len(days), owner_said,
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
        for o in conn.execute(f"SELECT src, cause, day, ulid FROM obs WHERE subject=? AND facet=? AND norm=? AND op IN {OBS_OPS_SQL}",
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
    for e in conn.execute("SELECT rel, dst_doc, dst_block FROM edges WHERE rel IN ('supersedes','contradicts') AND dst_block IS NOT NULL AND dst_doc IS NOT NULL"):
        # dst_doc is NULL for a wikilink with no page ([[ #^c1]]): it names no
        # claim, and dereferencing it on the next line aborts the rebuild.
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


def _derive_owner_rules(conn) -> None:
    """The live owner rule set, computed from owner-tier `op=rule` ledger
    lines directly — newest per (facet, target) wins. Triage reads this, not
    the prefs note, so editing the note's claims or edges cannot enable or
    disable a rule; only a verified owner Slack message can."""
    # A disposition for an address supersedes an older one for the same
    # address (you want one verdict per sender). A preference does not
    # supersede a different preference about the same subject — distinct
    # preferences each keep a row; only an identical re-statement collapses.
    seen: dict[tuple, sqlite3.Row] = {}
    for o in conn.execute("SELECT * FROM obs WHERE op='rule' AND tier='owner' ORDER BY ts ASC, ulid ASC"):
        # ts is minute-precision, so same-minute lines tie; the ULID breaks
        # the tie - mint-ordered to the millisecond, deterministic below it.
        m = DISPOSITION_RE.match(o["text"])
        target = m.group(2).lower() if m else o["subject"]
        key = (o["facet"], target) if (m and o["facet"] == "mail-disposition") else (o["facet"], target, o["norm"])  # a non-matching disposition falls back to the subject; two of them are not one rule
        seen[key] = o  # later line wins (same address for a disposition, same text for a preference)
    for key, o in seen.items():
        facet, target = key[0], key[1]
        m = DISPOSITION_RE.match(o["text"])
        action = m.group(1).lower() if m else None
        # The prefs claim that renders this rule, for reference/attest.
        cl = conn.execute(
                     # claims.id is the rowid alias, so this is insertion order -
                     # what a full scan returns today. Named so a future index on
                     # claims(text) cannot silently move the provenance line.
                     "SELECT doc, block FROM claims WHERE owner_said=1 AND text=? ORDER BY id LIMIT 1", (o["text"],)).fetchone()
        conn.execute("INSERT INTO rules(subject, facet, target, action, text, ledger_ref, created, doc, block) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (o["subject"], facet, target, action, o["text"], f"belt/ledger/{o['day']}#^{o['ulid']}", o["day"],
                      cl["doc"] if cl else None, cl["block"] if cl else None))


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
    rows = conn.execute(f"SELECT src, cause, day, ulid, tier FROM obs WHERE subject=? AND op IN {OBS_OPS_SQL}", (subject,)).fetchall()
    if not rows:
        conn.execute("DELETE FROM subjects WHERE key=?", (subject,))
        return
    causes = {cause_key(o["src"], o["cause"], o["day"], o["ulid"]) for o in rows}
    days = {o["day"] for o in rows}
    trusted = any(o["tier"] != "email" for o in rows)
    doc = doc_for_subject(conn, subject)
    conn.execute(
        "INSERT OR REPLACE INTO subjects(key, doc, n_obs, n_causes, n_days, first_seen, last_seen, untrusted) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (subject, doc, len(rows), len(causes), len(days), min(days), max(days), 0 if trusted else 1),
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
    """The live owner rules, derived from owner-tier ledger lines (see
    `_derive_owner_rules`) — immune to any edit of the prefs note. Oldest
    first, so the most fundamental rules are never the first dropped."""
    return conn.execute("SELECT * FROM rules ORDER BY created ASC, target ASC LIMIT ?", (limit,)).fetchall()


def dispositions_for(conn, addrs: list[str], domains: list[str]) -> list[tuple[sqlite3.Row, str]]:
    """Every live owner disposition that names one of these addresses or
    registrable domains. From the derived rules table, not the note."""
    out = []
    for r in conn.execute("SELECT * FROM rules WHERE facet='mail-disposition' ORDER BY created ASC, target ASC"):
        target = r["target"]
        if target in addrs or target in domains:
            out.append((r, target))
    return out


def due_soon(conn, today: str, limit: int = 5, horizon_days: int = 14, overdue_days: int = 30) -> list[sqlite3.Row]:
    t = date.fromisoformat(today)
    hi = (t + timedelta(days=horizon_days)).isoformat()
    lo = (t - timedelta(days=overdue_days)).isoformat()
    return conn.execute(
        "SELECT path, title, due, about FROM docs WHERE type='open' AND retired=0 AND due IS NOT NULL "
        "AND due <= ? AND due >= ? AND COALESCE(tier,'session') <> 'email' ORDER BY due ASC LIMIT ?", (hi, lo, limit)).fetchall()


def roster(conn, today: str, limit: int = 12, window_days: int = 60) -> list[sqlite3.Row]:
    """Entities recently in play: ranked by observations on their subject
    (aliases included) in the window, then by best claim score, then title.
    Titles come from closed sources (the note's title)."""
    since = (date.fromisoformat(today) - timedelta(days=window_days)).isoformat()
    aliases: dict[str, set[str]] = {}
    for r in conn.execute("SELECT from_subject, to_subject FROM subject_alias"):
        aliases.setdefault(r["to_subject"], set()).add(r["from_subject"])
    docs = conn.execute(
        "SELECT d.path, d.subject, d.title, d.type, "
        " (SELECT MAX(score) FROM claims c WHERE c.doc=d.path AND c.folded=0 AND c.tier <> 'email') AS best "
        "FROM docs d WHERE d.type IN ('person','org','topic') AND d.retired=0 AND d.export=1").fetchall()
    scored = []
    for d in docs:
        keys = [d["subject"]] + sorted(aliases.get(d["subject"], ())) if d["subject"] else []
        recent = 0
        if keys:
            q = ",".join("?" * len(keys))
            recent = conn.execute(f"SELECT COUNT(*) FROM obs WHERE subject IN ({q}) AND day >= ? AND op IN {OBS_OPS_SQL}",
                                  (*keys, since)).fetchone()[0]
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
    keys = [subject] + [r["from_subject"] for r in conn.execute("SELECT from_subject FROM subject_alias WHERE to_subject=?", (subject,))]
    q = ",".join("?" * len(keys))
    return conn.execute(
        f"SELECT * FROM obs WHERE subject IN ({q}) AND op IN {OBS_OPS_SQL} AND day >= ? ORDER BY ts DESC LIMIT ?",
        (*keys, since_day, limit)).fetchall()


def all_subjects(conn) -> set[str]:
    """Keys that exist: notes and belt subjects, minus keys that now alias
    to another (retired, merged, or an address that belongs to a note)."""
    aliased = {r["from_subject"] for r in conn.execute("SELECT from_subject FROM subject_alias")}
    out = {r["key"] for r in conn.execute("SELECT key FROM subjects")}
    for r in conn.execute("SELECT subject FROM docs WHERE retired=0 AND subject IS NOT NULL"):
        out.add(r["subject"])
    return out - aliased


def subject_aliases(conn) -> dict[str, str]:
    return {r["from_subject"]: r["to_subject"] for r in conn.execute("SELECT * FROM subject_alias")}


def canonical_subject(conn, subject: str) -> str:
    """Follow aliases (bounded), so a memo about a retired key or a bare
    address files under the note it belongs to."""
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
