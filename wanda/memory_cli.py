"""`wanda memory <verb>` — used by agent sessions (via Bash) and by hand.
Owner-only verbs (rule, attest) are deliberately absent: those are Slack
messages, so their authorship can be verified."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from wanda.config import Config
from wanda.memory import index as ix
from wanda.memory import passes, recall, render
from wanda.memory.ledger import Observation, append as ledger_append
from wanda.memory.notes import new_note, parse_note
from wanda.memory.render import TIER_TAG
from wanda.memory.subjects import parse_subject, resolve, subject_from_address, subject_from_slack
from wanda.memory.vault import Vault, clean_text, slugify, write_atomic
from wanda.store import Store

ENV_TASK = "WANDA_TASK_ID"
ENV_SESSION = "WANDA_SESSION_ID"


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("memory", help="look things up in and write to wanda's memory")
    verbs = p.add_subparsers(dest="verb", required=True)

    v = verbs.add_parser("who", help="a person or org by email address or Slack user id")
    v.add_argument("ident")
    v = verbs.add_parser("recall", help="free-text recall: notes, claims, recent observations")
    v.add_argument("text")
    v.add_argument("--budget", type=int, default=3000)
    v = verbs.add_parser("walk", help="a note plus the filing guides above it")
    v.add_argument("path", nargs="+")
    v = verbs.add_parser("search", help="full-text search over claims")
    v.add_argument("text")
    v.add_argument("--limit", type=int, default=10)
    v = verbs.add_parser("show", help="one note, claims first")
    v.add_argument("path")
    verbs.add_parser("rules", help="every standing rule from the owner")
    v = verbs.add_parser("note", help="record one fact")
    v.add_argument("text")
    v.add_argument("--about", required=True, help="subject key, e.g. person/robin-vale or org/acme.example")
    v.add_argument("--facet", default="", help="short slug grouping the fact, e.g. role, mail-pattern")
    v.add_argument("--until", default="", help="YYYY-MM-DD when this stops being true")
    v = verbs.add_parser("open", help="record a commitment with a date")
    v.add_argument("title")
    v.add_argument("--check-by", required=True)
    v.add_argument("--about", required=True)
    v = verbs.add_parser("forget", help="veto a claim and the pattern behind it (owner-stated claims need Slack)")
    v.add_argument("ref")
    v = verbs.add_parser("retire", help="retire a note, rewriting every link (optionally into a successor)")
    v.add_argument("path")
    v.add_argument("--to")
    v = verbs.add_parser("unretire", help="restore a retired note")
    v.add_argument("path")
    v = verbs.add_parser("reindex", help="rebuild the derived index from the vault")
    v.add_argument("--full", action="store_true")
    verbs.add_parser("fsck", help="dangling links, duplicate ids, oversize notes, stray temps")
    verbs.add_parser("hourly", help="run the hourly pass now")
    v = verbs.add_parser("import-cowork", help="one-time import of a .cowork-style vault")
    v.add_argument("dir")
    v = verbs.add_parser("digest", help="show pending digest lines")
    v.add_argument("--all", action="store_true")
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


def _provenance(store: Store) -> tuple[str, str]:
    """src and cause for a write from this process. A session's identity is
    what the harness put in its environment; the index later checks the task
    kind and run window, so a session cannot launder email into session-tier
    by editing these."""
    task = os.environ.get(ENV_TASK, "")
    if task:
        return "agent", f"task:{task}"
    return "harness", f"cli:{os.getpid()}"


def run(cfg: Config, args: argparse.Namespace) -> int:
    vault = Vault(cfg.memory_vault)
    verb = args.verb
    today = datetime.now(timezone.utc).date().isoformat()

    if verb == "who":
        conn = _conn(cfg)
        if conn is None:
            return _no_index()
        ident = args.ident.strip().lower()
        key = f"slack:{ident}" if ident.startswith("u") and "@" not in ident else f"mailto:{ident}"
        doc = ix.doc_for_id(conn, key) or ix.doc_for_id(conn, f"dom:{ident.rsplit('@', 1)[-1]}")
        if doc:
            print(recall.walk(vault, conn, [doc]), end="")
        subj = subject_from_slack(args.ident) if key.startswith("slack:") else subject_from_address(ident)
        if subj:
            obs = ix.subject_observations(conn, subj, limit=10)
            if obs:
                print(f"Recent observations on {subj}:")
                for o in obs:
                    print(f"- {o['day']} {TIER_TAG.get(o['tier'], '')} {o['text']}")
        if not doc and not (subj and obs):
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
        print(recall.walk(vault, conn, [p if p.endswith(".md") else p + ".md" for p in args.path], cap_b=12000), end="")
        return 0

    if verb == "search":
        conn = _conn(cfg)
        if conn is None:
            return _no_index()
        for r in ix.fts(conn, args.text, limit=args.limit):
            print(f"- {TIER_TAG.get(r['tier'], '')} {r['text']}  ({r['doc']}#^{r['block']}, {r['status']})")
        return 0

    if verb == "show":
        p = vault.root / args.path
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
        for r in ix.standing_rules(conn, limit=200):
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
        src, cause = _provenance(store)
        facet = slugify(args.facet, 32) if args.facet else ""
        o = Observation(subject=subj, facet=facet, text=clean_text(args.text), src=src, cause=cause, until=args.until)
        ledger_append(vault, o)
        if how == "near":
            print(f"filed under existing subject {subj} (close to {args.about})")
        elif how == "miss":
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
        src, cause = _provenance(store)
        tier = "session"
        task = os.environ.get(ENV_TASK, "")
        if task:
            t = store.get_task(int(task)) if task.isdigit() else None
            tier = "session" if (t and t["kind"] in ix.CONVERSATION_KINDS) else "email"
        slug = slugify(args.title, 40)
        path = vault.root / "open" / f"{args.check_by}-{slug}.md"
        if path.exists():
            print(f"already open: {vault.rel(path)}")
            return 0
        n = new_note(path, "open", clean_text(args.title, 160), created=today)
        n.meta.update({"check_by": args.check_by, "about": subj, "tier": tier})
        write_atomic(path, n.render())
        ledger_append(vault, Observation(subject=subj, facet="commitment", text=clean_text(args.title, 240), src=src,
                                         cause=cause, op="open", due=args.check_by, ref=vault.rel(path)))
        print(f"opened {vault.rel(path)}" + (" (from an email task: stays off the always-loaded list)" if tier == "email" else ""))
        return 0

    if verb == "forget":
        from wanda.memory.commands import normalize_ref
        store = _store(cfg)
        conn = _conn(cfg)
        ref = normalize_ref(args.ref)
        if not ref or conn is None:
            sys.exit("expected a claim reference like people/robin-vale#c4")
        doc, _, block = ref.partition("#^")
        row = conn.execute("SELECT * FROM claims WHERE doc=? AND block=?", (doc, block)).fetchone()
        if row is None:
            sys.exit(f"no claim at {ref}")
        if row["owner_said"]:
            sys.exit("that is an owner-stated claim; only the owner can forget it, from Slack: `forget " + args.ref + "`")
        keys = [r["key"] for r in conn.execute(
            "SELECT DISTINCT k.key FROM edges e JOIN rkeys k ON k.ulid=e.dst_block WHERE e.src_doc=? AND e.src_block=? AND e.rel='derived-from'", (doc, block))]
        subj = ix.subject_for_doc(doc) or "pref/general"
        src, cause = _provenance(store)
        ledger_append(vault, Observation(subject=subj, facet="retire", text=f"Forgotten: {row['text']}", src=src, cause=cause, op="retire", ref=ref))
        ledger_append(vault, Observation(subject=subj, facet="veto", text="Vetoed the pattern behind a forgotten claim", src=src, cause=cause,
                                         op="veto", ref=",".join(sorted(set(keys or [f'key:{subj}|'])))))
        print(f"forgotten on the next pass: {row['text']}")
        return 0

    if verb in ("retire", "unretire", "reindex", "fsck", "hourly", "import-cowork", "digest", "status"):
        store = _store(cfg)
        svc = passes.Services(cfg, store, vault)
        if verb == "retire":
            with passes.memory_lock(cfg.memory_lock_path):
                r = passes.retire(svc, args.path, args.to)
            print(json.dumps(r, indent=1))
            return 0
        if verb == "unretire":
            ok = passes.unretire(svc, args.path)
            print("restored" if ok else "nothing to restore")
            return 0 if ok else 1
        if verb == "reindex":
            conn = passes.open_conn(svc)
            rep = ix.rebuild(vault, conn, passes.StoreTrust(store))
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
            with passes.memory_lock(cfg.memory_lock_path):
                conn = passes.open_conn(svc)
                rep = passes.hourly(svc, conn, cfg.workspace_dir)
            print(json.dumps(rep.__dict__, indent=1, default=str))
            return 0
        if verb == "import-cowork":
            with passes.memory_lock(cfg.memory_lock_path):
                rep = passes.import_cowork(svc, Path(args.dir).expanduser())
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
    from wanda.memory.vault import DIR_TO_TYPE
    d, _, slug = key.partition("/")
    if d in DIR_TO_TYPE:
        key = f"{DIR_TO_TYPE[d]}/{slug}"
    if parse_subject(key) is None:
        # A bare address or Slack id is a subject too.
        if "@" in key:
            s = subject_from_address(key)
            return (s, "exact", []) if s else (None, "miss", [])
        return None, "miss", []
    if conn is None:
        return key, "exact", []
    r = resolve(key, ix.all_subjects(conn), ix.subject_aliases(conn))
    return r.key, r.how, [k for k, _ in r.nearest]


def _no_index() -> int:
    print("memory index not built yet; run `wanda memory reindex` (the daemon does this hourly)", file=sys.stderr)
    return 1
