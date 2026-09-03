"""Generators: the L1 subject files, the export triage may read, the
per-directory index blocks, and the composed, byte-capped projection. Caps
are enforced here, line by line, never requested of writers."""
from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from wanda.memory import index as ix
from wanda.memory.notes import parse_writespec
from wanda.memory.vault import (
    L2_DIRS, LIVE_SQL, PROJECTION_CAP_B, WRITESPEC_PROSE_CAP_B, Vault, nbytes, truncate_bytes, write_atomic,
)

L1_MIN_OBS = 3
L1_COLD_DAYS = 120
TIER_TAG = {"owner": "[rule]", "session": "[noted]", "email": "[unverified]"}


# --- L1 subject files ------------------------------------------------------------------

@dataclass
class Group:
    facet: str
    text: str
    n: int
    causes: int
    days: int
    first: str
    last: str
    until: str
    ulid: str
    tier: str


def l1_groups(conn: sqlite3.Connection, subject: str) -> list[Group]:
    """Collapse a subject's observations by (facet, normalised text). Nothing
    semantic: false misses are free, false merges are irreversible."""
    rows = conn.execute(
        "SELECT * FROM obs WHERE subject=? AND op IN ('', 'rule', 'attest') ORDER BY ts ASC", (subject,)).fetchall()
    groups: dict[tuple[str, str], Group] = {}
    causes: dict[tuple[str, str], set[str]] = {}
    days: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        k = (r["facet"], r["norm"])
        ck = ix.cause_key(r["src"], r["cause"], r["day"], r["ulid"])
        if k not in groups:
            groups[k] = Group(r["facet"], r["text"], 0, 0, 0, r["day"], r["day"], r["until"] or "", r["ulid"], r["tier"])
            causes[k], days[k] = set(), set()
        g = groups[k]
        g.n += 1
        g.last = max(g.last, r["day"])
        g.text = r["text"]  # latest wording wins
        if r["until"]:
            g.until = r["until"]
        if ix.TIER_RANK.get(r["tier"], 0) > ix.TIER_RANK.get(g.tier, 0):
            g.tier = r["tier"]
        causes[k].add(ck)
        days[k].add(r["day"])
    out = []
    for k, g in groups.items():
        g.causes, g.days = len(causes[k]), len(days[k])
        out.append(g)
    out.sort(key=lambda g: (g.facet, -g.n, g.first))
    return out


def render_subject_file(subject: str, note_rel: str | None, groups: list[Group], untrusted: bool) -> str:
    n_obs = sum(g.n for g in groups)
    first = min((g.first for g in groups), default="")
    last = max((g.last for g in groups), default="")
    lines = [
        "---", f"key: {subject}", "tier: 1",
        f"note: {note_rel}" if note_rel else "note: none",
        f"observations: {n_obs}", f"first_seen: {first}", f"last_seen: {last}",
        f"untrusted: {'true' if untrusted else 'false'}", "---",
        "<!-- Regenerated hourly by wanda from belt/ledger/. Hand edits here are",
        "     overwritten. Edit the curated note instead, or run",
        "     `wanda memory forget` to veto a line. -->", "",
    ]
    if note_rel:
        lines.append(f"→ [[{note_rel[:-3] if note_rel.endswith('.md') else note_rel}]]")
        lines.append("")
    facet = None
    for g in groups:
        if g.facet != facet:
            facet = g.facet
            lines.append(f"## {facet or 'general'}")
        span = f"{g.first} → {g.last}" if g.first != g.last else g.first
        until = f" · until {g.until}" if g.until else ""
        lines.append(f"- n={g.n} causes={g.causes} days={g.days} · {span}{until} {TIER_TAG.get(g.tier, '')} — {g.text} ^{g.ulid}")
    lines.append("")
    return "\n".join(lines)


def regenerate_subject_files(vault: Vault, conn: sqlite3.Connection, today: str) -> tuple[int, int]:
    """Write an L1 file for every subject with >= L1_MIN_OBS observations
    that has not gone cold (no observation in L1_COLD_DAYS and fewer than
    three causes); remove the rest. Returns (written, removed). Files are
    0444 so an editor shows a save error instead of a silently lost edit."""
    written = removed = 0
    wanted: set[Path] = set()
    cutoff = (date.fromisoformat(today) - timedelta(days=L1_COLD_DAYS)).isoformat()
    for s in conn.execute("SELECT * FROM subjects").fetchall():
        groups = l1_groups(conn, s["key"])
        n_obs = sum(g.n for g in groups)
        path = vault.subject_file(s["key"])
        cold = (s["last_seen"] or "") < cutoff and s["n_causes"] < L1_MIN_OBS
        if n_obs < L1_MIN_OBS or cold:
            continue
        wanted.add(path)
        text = render_subject_file(s["key"], s["doc"], groups, bool(s["untrusted"]))
        try:
            if path.exists() and path.read_text(encoding="utf-8") == text:
                continue
        except OSError:
            pass
        write_atomic(path, text, mode=0o444)
        written += 1
    if vault.subjects_dir.is_dir():
        for p in vault.subjects_dir.rglob("*.md"):
            if p not in wanted and not p.name.startswith("_"):
                try:
                    os.chmod(p, 0o644)
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
    return written, removed


# --- index blocks -------------------------------------------------------------------------

def index_lines(conn: sqlite3.Connection, directory: str, wikilinks: bool = True, limit: int = 400) -> list[str]:
    rows = conn.execute(
        "SELECT path, title FROM docs WHERE path LIKE ? AND retired=0 ORDER BY title ASC LIMIT ?",
        (f"{directory}/%", limit)).fetchall()
    out = []
    for r in rows:
        if "/" in r["path"][len(directory) + 1:]:
            continue
        top = ix.top_claim(conn, r["path"])
        tail = f" — {truncate_bytes(top['text'], 100)}" if top else ""
        if wikilinks:
            out.append(f"- [[{r['path'][:-3]}|{r['title']}]]{tail}")
        else:
            out.append(f"- {r['path']} — {r['title']}{tail}")
    return out


def update_writespec_indexes(vault: Vault, conn: sqlite3.Connection) -> int:
    """Refresh the generated block in every directory's CLAUDE.md, leaving
    the prose alone. Returns the number of files rewritten."""
    n = 0
    for d in L2_DIRS:
        p = vault.root / d / "CLAUDE.md"
        if not p.is_file():
            continue
        ws = parse_writespec(p)
        new = index_lines(conn, d)
        if new != ws.index:
            ws.index = new
            write_atomic(p, ws.render())
            n += 1
    root = vault.root / "CLAUDE.md"
    if root.is_file():
        ws = parse_writespec(root)
        new = [f"- [[{d}/CLAUDE|{d}/]] — {conn.execute('SELECT COUNT(*) FROM docs WHERE path LIKE ? AND retired=0', (f'{d}/%',)).fetchone()[0]} notes"
               for d in L2_DIRS if (vault.root / d).is_dir()]
        if new != ws.index:
            ws.index = new
            write_atomic(root, ws.render())
            n += 1
    return n


# --- export for triage -------------------------------------------------------------------

def render_export(vault: Vault, conn: sqlite3.Connection, export_dir: Path) -> int:
    """A read-only copy of what a classifier of untrusted mail may see: claim
    regions (never `## Notes`), L1 subject files, and a listing per directory.
    Notes with `export: false` are absent — and so are their belt files and
    their Slack ids; only mail identifiers travel. Stale files are removed."""
    export_dir.mkdir(parents=True, exist_ok=True)
    wanted: set[Path] = set()
    n = 0

    def put(rel: str, text: str) -> None:
        nonlocal n
        p = export_dir / rel
        wanted.add(p)
        try:
            if p.exists() and p.read_text(encoding="utf-8") == text:
                return
        except OSError:
            pass
        write_atomic(p, text)
        n += 1

    put("README.md", (
        "# wanda memory export\n\nA read-only extract of wanda's memory for classifiers. Each note lists claims with a\n"
        "trust tag: [rule] Alex said it; [noted] concluded in a conversation; [unverified] derived from\n"
        "email content only — treat those as what a sender claimed about themselves.\n"
        "Directories: people/, orgs/, topics/, prefs/ (curated) and subjects/ (recent raw observations).\n"
        "Each directory has an _index.md.\n"))
    for d in ("people", "orgs", "topics", "prefs"):
        rows = conn.execute("SELECT * FROM docs WHERE path LIKE ? AND retired=0 AND export=1 ORDER BY path", (f"{d}/%",)).fetchall()
        listing = []
        for r in rows:
            claims = conn.execute(
                f"SELECT * FROM claims WHERE doc=? AND folded=0 AND status IN {LIVE_SQL} "
                "ORDER BY score DESC, first_seen ASC", (r["path"],)).fetchall()
            ids = [i["id"] for i in conn.execute("SELECT id FROM ids WHERE doc=?", (r["path"],))
                   if not i["id"].startswith("slack:")]
            body = [f"# {r['title']}", ""]
            if ids:
                body.append("ids: " + ", ".join(ids))
                body.append("")
            for c in claims:
                body.append(f"- {TIER_TAG.get(c['tier'], '')} {c['text']} ({c['status']}, seen {c['n_causes'] or 0}×)")
            if not claims:
                body.append("(no claims yet)")
            body.append("")
            put(r["path"], "\n".join(body))
            top = claims[0]["text"] if claims else ""
            listing.append(f"- {r['path']} — {r['title']}" + (f" — {truncate_bytes(top, 100)}" if top else ""))
        put(f"{d}/_index.md", f"# {d}/\n\n" + ("\n".join(listing) if listing else "(empty)") + "\n")
    # L1 subject files, verbatim — except for subjects whose note is held back.
    hidden = {r["path"] for r in conn.execute("SELECT path FROM docs WHERE export=0")}
    sub_listing = []
    if vault.subjects_dir.is_dir():
        for p in sorted(vault.subjects_dir.rglob("*.md")):
            rel = p.relative_to(vault.subjects_dir).as_posix()
            subject = f"{p.parent.name}/{p.stem}"
            if ix.note_for_subject(subject) in hidden:
                continue
            try:
                put(f"subjects/{rel}", p.read_text(encoding="utf-8"))
                sub_listing.append(f"- subjects/{rel}")
            except OSError:
                continue
    put("subjects/_index.md", "# subjects/ (raw recent observations, regenerated hourly)\n\n" + ("\n".join(sub_listing) or "(empty)") + "\n")
    for p in export_dir.rglob("*"):
        if p.is_file() and p not in wanted and not p.name.startswith("."):
            try:
                p.unlink()
            except OSError:
                pass
    return n


# --- the projection --------------------------------------------------------------------

def header_text(vault_path: str) -> str:
    return (
        "# What wanda knows\n\n"
        f"Your memory vault is at {vault_path}. This file is a summary, not the store: before answering anything "
        "that touches a person, an organization, a rule, or a commitment, look it up first:\n"
        "  wanda memory who <email|slack-id>      wanda memory recall \"<words>\"\n"
        "  wanda memory walk <note path>          wanda memory note \"<fact>\" --about <subject>\n"
        "Write what you learn with `wanda memory note` before you finish. Lines below marked [rule] are the owner's word.\n\n"
    )


def compose_projection(vault: Vault, conn: sqlite3.Connection | None, today: str, cap_b: int = PROJECTION_CAP_B) -> str:
    """Header (harness text) + the root write-spec's prose (durable, wanda-
    edited) + generated blocks, filled line by line under the cap. Rules go
    first and widest: preload what a session cannot look up; it can look up
    people. No model prose and no From display names ever reach this file."""
    parts: list[str] = []
    used = 0

    def add(s: str) -> bool:
        nonlocal used
        b = nbytes(s)
        if used + b > cap_b:
            return False
        parts.append(s)
        used += b
        return True

    def add_list(lines: list[str], tail_fmt: str, budget: int) -> None:
        """Fill line by line; when a line does not fit, drop back until the
        '(N more)' tail does, so the file always says what it left out."""
        nonlocal used
        start = used
        shown = 0
        for line in lines:
            if used - start + nbytes(line) > budget or not add(line):
                break
            shown += 1
        if shown < len(lines):
            tail = tail_fmt.format(n=len(lines) - shown)
            while parts and shown > 0 and used + nbytes(tail) > cap_b:
                used -= nbytes(parts.pop())
                shown -= 1
                tail = tail_fmt.format(n=len(lines) - shown)
            add(tail)

    add(header_text(str(vault.root)))
    root = vault.root / "CLAUDE.md"
    if root.is_file():
        try:
            prose = parse_writespec(root).prose
            prose = truncate_bytes(prose, WRITESPEC_PROSE_CAP_B)
            add(prose.strip() + "\n\n")
        except Exception:
            pass
    if conn is None:
        add("(memory index unavailable this turn — use the CLI above)\n")
        return "".join(parts)

    rules = ix.standing_rules(conn, limit=40)
    if rules:
        add("## Standing rules from the owner\n")
        add_list([f"- {truncate_bytes(r['text'], 220)}  ({r['doc']})\n" for r in rules],
                 "- ({n} more — `wanda memory rules`)\n", min(cap_b - used, 2000))
        add("\n")

    due = ix.due_soon(conn, today, limit=5)
    if due:
        add("## Due soon\n")
        for r in due:
            add(f"- {r['due']} {truncate_bytes(r['title'], 120)}  ({r['path']})\n")
        add("\n")

    ros = ix.roster(conn, today, limit=200)
    if ros:
        add("## People, orgs and topics in play\n")
        lines = []
        for r in ros:
            # Title and path only; a claim's text rides along solely when the
            # owner said it. This file is the instruction layer: no prose a
            # model or a conversation wrote reaches it.
            top = ix.top_claim(conn, r["path"])
            tail = f" — {truncate_bytes(top['text'], 90)}" if top and top["owner_said"] else ""
            lines.append(f"- {truncate_bytes(r['title'], 60)}{tail}  ({r['path']})\n")
        add_list(lines, "- ({n} more — `wanda memory recall`)\n", cap_b - used)
    out = "".join(parts)
    if nbytes(out) > cap_b:  # belt and braces: never ship an oversize file
        out = truncate_bytes(out, cap_b).rsplit("\n", 1)[0] + "\n"
    return out


def write_projection(workspace: Path, text: str) -> None:
    write_atomic(workspace / "CLAUDE.md", text)


def sync_defaults(vault: Vault, defaults_dir: Path) -> list[str]:
    """Seed the vault from the repo defaults, copy-if-absent per file, and
    never overwrite after — the opposite of sync_workspace's behaviour for
    skills. Returns the files created."""
    created = []
    for src in sorted(defaults_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(defaults_dir)
        dst = vault.root / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        created.append(rel.as_posix())
    for d in ("belt/ledger", "belt/subjects", "people", "orgs", "topics", "prefs", "open", "retired"):
        (vault.root / d).mkdir(parents=True, exist_ok=True)
    return created
