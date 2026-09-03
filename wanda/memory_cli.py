"""`wanda memory <verb>` — used by agent sessions (via Bash) and by hand.
Owner-only verbs (rule, attest) are deliberately absent: those are Slack
messages, so their authorship can be verified."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from wanda.config import Config
from wanda.memory import commands, index as ix, passes, recall
from wanda.memory.ledger import Observation, append as ledger_append
from wanda.memory.notes import new_note, parse_note
from wanda.memory.render import TIER_TAG
from wanda.memory.subjects import parse_subject, resolve, subject_from_address, subject_from_slack
from wanda.memory.vault import DIR_TO_TYPE, Vault, clean_text, slugify, write_atomic
from wanda.store import Store

ENV_TASK = "WANDA_TASK_ID"
SLACK_ID_RE = re.compile(r"^[uw][a-z0-9_]{2,}$", re.I)


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("memory", help="look things up in and write to wanda's memory")
    verbs = p.add_subparsers(dest="verb", required=True)

    v = verbs.add_parser("who", help="a person or org by email address or Slack user id")
    v.add_argument("ident", help="an email address, or a Slack user id like U0123456789")
    v = verbs.add_parser("recall", help="free-text recall: notes, claims, recent observations")
    v.add_argument("text")
    v.add_argument("--budget", type=int, default=3000, help="max bytes of output (default 3000)")
    v = verbs.add_parser("walk", help="a note plus the filing guides above it")
    v.add_argument("path", nargs="+", help="vault-relative note path, e.g. people/robin-vale.md")
    v = verbs.add_parser("search", help="full-text search over claims")
    v.add_argument("text")
    v.add_argument("--limit", type=int, default=10)
    v = verbs.add_parser("show", help="one note, claims first")
    v.add_argument("path", help="vault-relative note path")
    verbs.add_parser("rules", help="every standing rule from the owner")
    v = verbs.add_parser("note", help="record one fact")
    v.add_argument("text")
    v.add_argument("--about", required=True, help="subject key, e.g. person/robin-vale or org/acme.example")
    v.add_argument("--facet", default="", help="short slug grouping the fact, e.g. role, mail-pattern")
    v.add_argument("--until", default="", help="YYYY-MM-DD when this stops being true")
    v = verbs.add_parser("open", help="record a commitment with a date")
    v.add_argument("title")
    v.add_argument("--check-by", required=True, help="YYYY-MM-DD; the item lapses a week after this if nothing touches it")
    v.add_argument("--about", required=True, help="subject key the commitment belongs to")
    v = verbs.add_parser("pin", help="keep a claim as written: never rewritten, folded or dropped from the projection")
    v.add_argument("ref", help="claim reference like people/robin-vale#c4")
    v = verbs.add_parser("forget", help="veto a claim and the pattern behind it (owner-stated claims need Slack)")
    v.add_argument("ref", help="claim reference like people/robin-vale#c4")
    v = verbs.add_parser("retire", help="retire a note, rewriting every link (--to merges it into a successor; not from a session)")
    v.add_argument("path", help="vault-relative note path")
    v.add_argument("--to", help="successor note path, e.g. people/robin-vale.md")
    v = verbs.add_parser("unretire", help="restore a retired or lapsed note")
    v.add_argument("path", help="path under retired/, e.g. people/x.md or open/2026/2026-09-01-x.md")
    verbs.add_parser("reindex", help="rebuild the derived index from the vault")
    verbs.add_parser("fsck", help="dangling links, duplicate ids, oversize notes, stray temps")
    verbs.add_parser("hourly", help="run the hourly pass now")
    v = verbs.add_parser("import-cowork", help="one-time import of a .cowork-style vault (not while a session is running)")
    v.add_argument("dir", help="the old vault directory")
    v = verbs.add_parser("digest", help="show pending digest lines")
    v.add_argument("--all", action="store_true", help="include lines already posted")
    verbs.add_parser("status", help="paths, counts, last passes")


def _store(cfg: Config) -> Store:
    return Store(cfg.db_path)


def _conn(cfg: Config, create: bool = False):
    conn = ix.open_readonly(cfg.memory_index_path)
    if conn is None and create:
        svc = passes.Services(cfg, _store(cfg), Vault(cfg.memory_vault))
        conn = passes.open_conn(svc)
        ix.rebuild(svc.vault, conn, passes.StoreTrust(svc.store))
    return conn


def _provenance() -> tuple[str, str, str]:
    """(src, cause, pg) for a write from this process. The process group is
    the claude subprocess the harness started (its own group leader), which
    a session cannot change to another session's; the index decides the
    tier from the run window that group belongs to — never from the task id
    in the environment, which is informational."""
    task = os.environ.get(ENV_TASK, "").strip()
    pg = str(os.getpgrp())
    if task.isdigit():
        return "agent", f"task:{task}", pg
    return "harness", f"cli:{os.getpid()}", pg


def _line_tier(store: Store, pg: str) -> str:
    """What the index will decide for a line written right now."""
    from datetime import datetime as _dt
    return passes.StoreTrust(store).line_tier(pg, _dt.now(timezone.utc))


def _in_session() -> bool:
    return bool(os.environ.get(ENV_TASK, "").strip())


def _append(cfg: Config, store: Store, vault: Vault, o: Observation) -> None:
    """Ledger line first (the truth), then the index rows (zero-lag recall).
    A busy or missing index is fine; the hourly pass reconciles."""
    ledger_append(vault, o)
    if not cfg.memory_index_path.exists():
        return
    try:
        conn = ix.open_index(cfg.memory_index_path)
        try:
            ix.insert_observation(conn, o, ix.tier_for_obs(o, passes.StoreTrust(store)))
        finally:
            conn.close()
    except Exception as e:  # never fail a write because the cache was busy
        print(f"(index not updated now: {e}; the hourly pass will)", file=sys.stderr)


def _iso_date(value: str, flag: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        sys.exit(f"{flag} must be a date like 2026-09-15, got {value!r}")


def run(cfg: Config, args: argparse.Namespace) -> int:
    vault = Vault(cfg.memory_vault)
    verb = args.verb
    today = datetime.now(timezone.utc).date().isoformat()

    if verb == "who":
        conn = _conn(cfg)
        if conn is None:
            return _no_index()
        ident = args.ident.strip().lower().strip("<>").removeprefix("mailto:").split("|")[-1]
        is_slack = "@" not in ident and SLACK_ID_RE.match(ident) is not None
        key = f"slack:{ident}" if is_slack else f"mailto:{ident}"
        doc = ix.doc_for_id(conn, key) or (None if is_slack else ix.doc_for_id(conn, f"dom:{ident.rsplit('@', 1)[-1]}"))
        if doc:
            print(recall.walk(vault, conn, [doc]), end="")
        subj = subject_from_slack(args.ident) if is_slack else subject_from_address(ident)
        obs = ix.subject_observations(conn, ix.canonical_subject(conn, subj), limit=10) if subj else []
        if obs:
            print(f"Recent observations on {subj}:")
            for o in obs:
                print(f"- {o['day']} {TIER_TAG.get(o['tier'], '')} {o['text']}")
        if not doc and not obs:
            print(f"nothing known about {args.ident}")
        return 0

    if verb == "recall":
        conn = _conn(cfg)
        if conn is None:
            return _no_index()
        out = recall.for_agent(vault, conn, recall.AgentContext(text=args.text), today, cap_b=args.budget)
        print(out or "nothing matched; try `wanda memory search` or a subject key")
        return 0

    if verb == "walk":
        conn = _conn(cfg)
        paths = []
        for p in args.path:
            rel = p if p.endswith(".md") else p + ".md"
            try:
                vault.inside(rel)
            except ValueError as e:
                sys.exit(str(e))
            paths.append(rel)
        print(recall.walk(vault, conn, paths, cap_b=12000), end="")
        return 0

    if verb == "search":
        conn = _conn(cfg)
        if conn is None:
            return _no_index()
        for r in ix.fts(conn, args.text, limit=args.limit):
            print(f"- {TIER_TAG.get(r['tier'], '')} {r['text']}  ({r['doc']}#^{r['block']}, {r['status']})")
        return 0

    if verb == "show":
        try:
            p = vault.inside(args.path)
        except ValueError as e:
            sys.exit(str(e))
        if not p.exists():
            sys.exit(f"no such note: {args.path}")
        note = parse_note(p)
        print(f"# {note.title}  ({args.path})")
        for c in note.live():
            print(f"- {TIER_TAG.get(c.value('tier'), '')} {c.text}  ^{c.block}")
        if note.post.strip():
            print("\n" + note.post.strip())
        return 0

    if verb == "rules":
        conn = _conn(cfg)
        if conn is None:
            return _no_index()
        for r in ix.standing_rules(conn, limit=500):
            print(f"- {r['text']}  ({r['doc']}#^{r['block']})")
        return 0

    if verb == "note":
        store = _store(cfg)
        conn = _conn(cfg)
        subj, how, nearest = _resolve_subject(args.about, conn)
        if subj is None:
            print(f"unknown subject {args.about!r}; expected <type>/<slug> with type in person|org|topic|pref", file=sys.stderr)
            if nearest:
                print("nearest: " + ", ".join(nearest), file=sys.stderr)
            return 2
        until = _iso_date(args.until, "--until") if args.until else ""
        src, cause, pg = _provenance()
        o = Observation(subject=subj, facet=slugify(args.facet, 32) if args.facet else "", text=clean_text(args.text),
                        src=src, cause=cause, until=until, pg=pg)
        try:
            _append(cfg, store, vault, o)
        except ValueError as e:
            sys.exit(f"not recorded: {e}")
        if how == "near":
            print(f"filed under existing subject {subj} (close to {args.about})")
        elif how == "miss":
            store.digest_add("mint", f"new subject {subj} (from `wanda memory note`)")
            print(f"noted on new subject {subj} (reported in the next digest)")
        else:
            print(f"noted on {subj}")
        return 0

    if verb == "open":
        store = _store(cfg)
        conn = _conn(cfg)
        subj, how, nearest = _resolve_subject(args.about, conn)
        if subj is None:
            print(f"unknown subject {args.about!r}", file=sys.stderr)
            return 2
        check_by = _iso_date(args.check_by, "--check-by")
        src, cause, pg = _provenance()
        tier = _line_tier(store, pg)  # derived the same way the index derives it, never defaulted
        slug = slugify(args.title, 40) or "item"
        path = vault.root / "open" / f"{check_by}-{slug}.md"
        if path.exists():
            print(f"already open: {vault.rel(path)}")
            return 0
        n = new_note(path, "open", clean_text(args.title, 160), created=today)
        n.meta.update({"check_by": check_by, "about": subj})
        write_atomic(path, n.render())
        _append(cfg, store, vault, Observation(subject=subj, facet="commitment", text=clean_text(args.title, 240), src=src,
                                               cause=cause, op="open", due=check_by, ref=vault.rel(path), pg=pg))
        print(f"opened {vault.rel(path)}" + (" (from an email task: stays off the always-loaded list)" if tier == "email" else ""))
        return 0

    if verb in ("pin", "forget"):
        store = _store(cfg)
        conn = _conn(cfg)
        ref = commands.normalize_ref(args.ref)
        if not ref or conn is None:
            sys.exit("expected a claim reference like people/robin-vale#c4" + ("" if ref else "") + (" (no index yet)" if ref and conn is None else ""))
        doc, _, block = ref.partition("#^")
        row = conn.execute("SELECT * FROM claims WHERE doc=? AND block=?", (doc, block)).fetchone()
        if row is None:
            sys.exit(f"no claim at {ref}")
        subj = ix.subject_for_doc(doc) or passes.GENERAL_PREF_SUBJECT
        src, cause, pg = _provenance()
        if verb == "pin":
            _append(cfg, store, vault, Observation(subject=subj, facet="pin", text=f"Pinned: {row['text']}", src=src, cause=cause, op="pin", ref=ref, pg=pg))
            print(f"pinned on the next pass: {row['text']}")
            return 0
        if row["owner_said"] or row["pinned"]:
            sys.exit("that claim is the owner's word; only the owner can forget it, from Slack: `forget " + args.ref + "`")
        for o in commands.forget_observations(conn, doc, block, row["text"], subj, src=src, cause=cause, pg=pg):
            _append(cfg, store, vault, o)
        print(f"forgotten on the next pass: {row['text']}")
        return 0

    if verb in ("retire", "unretire", "reindex", "fsck", "hourly", "import-cowork", "digest", "status"):
        store = _store(cfg)
        svc = passes.Services(cfg, store, vault)
        if verb == "retire":
            if args.to and _in_session():
                sys.exit("merging notes (--to) is an identity decision for the owner, not for a session; retire without --to, or ask")
            try:
                with passes.memory_lock(cfg.memory_lock_path):
                    r = passes.retire(svc, args.path, args.to)
            except (ValueError, FileNotFoundError) as e:
                sys.exit(str(e))
            except passes.Busy:
                sys.exit("a memory pass is running; try again in a minute")
            print(json.dumps(r, indent=1))
            return 0
        if verb == "unretire":
            ok = passes.unretire(svc, args.path)
            print("restored" if ok else "nothing to restore")
            return 0 if ok else 1
        if verb == "reindex":
            try:
                with passes.memory_lock(cfg.memory_lock_path):
                    conn = passes.open_conn(svc)
                    rep = ix.rebuild(vault, conn, passes.StoreTrust(store))
            except passes.Busy:
                sys.exit("a memory pass is running; it rebuilds the index itself")
            print(f"indexed {rep.docs} notes, {rep.claims} claims, {rep.obs} observations; {len(rep.rejected)} rejected lines, {len(rep.flags)} flags")
            return 0
        if verb == "fsck":
            conn = _conn(cfg, create=True)
            issues = passes.fsck(vault, conn)
            for i in issues:
                print(f"- {i}")
            print("ok" if not issues else f"{len(issues)} issue(s)")
            return 0 if not issues else 1
        if verb == "hourly":
            try:
                with passes.memory_lock(cfg.memory_lock_path):
                    conn = passes.open_conn(svc)
                    rep = passes.hourly(svc, conn, cfg.workspace_dir)
            except passes.Busy:
                sys.exit("a memory pass is already running")
            print(rep.summary())
            return 0
        if verb == "import-cowork":
            if store.open_windows():
                sys.exit("an agent session is running; import when wanda is idle so the imported lines are not mistaken for its work")
            if _in_session():
                sys.exit("import is for the owner at a terminal, not for a session")
            try:
                with passes.memory_lock(cfg.memory_lock_path):
                    rep = passes.import_cowork(svc, Path(args.dir).expanduser())
            except passes.Busy:
                sys.exit("a memory pass is running; try again in a minute")
            print(json.dumps(rep, indent=1))
            return 0
        if verb == "digest":
            rows = store._query("SELECT * FROM memory_digest ORDER BY id DESC LIMIT 100") if args.all else store.digest_pending()
            for r in rows:
                print(f"- [{r['kind']}] {r['text']}" + ("" if r["posted_at"] else "  (pending)"))
            if not rows:
                print("nothing pending")
            return 0
        if verb == "status":
            print(f"vault:   {vault.root}")
            print(f"index:   {cfg.memory_index_path} ({'present' if cfg.memory_index_path.exists() else 'missing'})")
            print(f"export:  {cfg.memory_export_dir}")
            print(f"hourly:  {store.memory_get('hourly_at') or 'never'}")
            print(f"nightly: {store.memory_get('nightly_date') or 'never'}")
            conn = _conn(cfg)
            if conn is not None:
                for t in ("docs", "claims", "obs", "subjects", "vetoes"):
                    print(f"{t:8} {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
            return 0
    return 1


def _resolve_subject(about: str, conn):
    key = about.strip().lower()
    if key.endswith(".md"):
        key = key[:-3]
    d, _, slug = key.partition("/")
    if d in DIR_TO_TYPE:
        key = f"{DIR_TO_TYPE[d]}/{slug}"
    if parse_subject(key) is None:
        # A bare address is a subject too.
        if "@" in key:
            s = subject_from_address(key)
            return (ix.canonical_subject(conn, s) if (s and conn is not None) else s, "exact", []) if s else (None, "miss", [])
        return None, "miss", []
    if conn is None:
        return key, "exact", []
    r = resolve(key, ix.all_subjects(conn), ix.subject_aliases(conn))
    return ix.canonical_subject(conn, r.key), r.how, [k for k, _ in r.nearest]


def _no_index() -> int:
    print("memory index not built yet; run `wanda memory reindex` (the daemon does this hourly)", file=sys.stderr)
    return 1
