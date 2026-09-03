"""Owner commands from Slack: the one path by which anything becomes
owner-tier memory. Parsed by the harness before any session runs, minted
in-process with `cause=slack:<channel>:<ts>`, and re-verified against Slack
by the hourly pass — a session can post as the bot, never as the owner."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from wanda.memory import index as ix
from wanda.memory.ledger import Observation
from wanda.memory.subjects import parse_subject, registrable_domain, resolve, subject_from_address
from wanda.memory.vault import DIR_TO_TYPE, TYPE_TO_DIR, clean_text
from wanda.triage import addresses_in

VERBS = ("rule", "attest", "forget", "pin", "unretire")
ACTIONS = ("trash", "ignore", "attention")
COMMAND_RE = re.compile(r"^\s*(?:<@[A-Z0-9]+(?:\|[^>]*)?>\s*)?(rule|attest|forget|pin|unretire)\b(.*)$", re.I | re.S)
REF_RE = re.compile(r"^(?P<doc>[a-z]+/[^#\s]+?)(?:\.md)?#\^?(?P<block>[a-z0-9]{1,24})$")
OFFER_RE = re.compile(r"^k\d+$")
DISPOSITION_FACET = "mail-disposition"
PREFERENCE_FACET = "preference"


@dataclass
class Parsed:
    verb: str
    args: list[str]
    rest: str


def parse_command(text: str) -> Parsed | None:
    m = COMMAND_RE.match(text or "")
    if not m:
        return None
    rest = m.group(2).strip()
    return Parsed(m.group(1).lower(), rest.split(), rest)


def is_command(text: str) -> bool:
    return parse_command(text) is not None


def normalize_ref(ref: str) -> str | None:
    """`people/x#c4`, `people/x.md#^c4` -> `people/x.md#^c4`."""
    m = REF_RE.match(ref.strip())
    if not m:
        return None
    return f"{m.group('doc')}.md#^{m.group('block')}"


def rule_text(action: str, target: str, note: str = "") -> str:
    """Closed vocabulary, harness-built. This exact string becomes the claim,
    so an owner rule never carries model prose."""
    t = f"{action} mail from {target}"
    if note:
        t += f": {clean_text(note, 160)}"
    return t


@dataclass
class Minted:
    observations: list[Observation] = field(default_factory=list)
    reply: str = ""
    ok: bool = True


@dataclass
class Context:
    channel: str
    ts: str
    user: str
    text: str
    # For a command typed inside an email task thread: the email's sender.
    task_sender: str = ""
    when: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def cause(self) -> str:
        return f"slack:{self.channel}:{self.ts}"


def _key_to_note(key: str) -> str:
    t, _, slug = key.partition("/")
    return f"{TYPE_TO_DIR.get(t, t)}/{slug}.md"


def _target_to_subject(token: str, conn, store) -> tuple[str | None, str, list[str]]:
    """Resolve the second token of `rule`: an offer ref, an address, a domain,
    a subject key or a note path. Returns (subject, display, nearest)."""
    tok = token.strip().strip("<>").lower()
    if "|" in tok:  # Slack mailto markup <mailto:a@b|a@b>
        tok = tok.split("|", 1)[-1]
    tok = tok.removeprefix("mailto:")
    if "@" in tok and addresses_in(tok):
        addr = addresses_in(tok)[0]
        doc = ix.doc_for_id(conn, f"mailto:{addr}") if conn is not None else None
        subj = ix.subject_for_doc(doc) if doc else subject_from_address(addr)
        return subj, addr, []
    if "." in tok and "/" not in tok and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", tok):
        dom = registrable_domain(tok)
        doc = ix.doc_for_id(conn, f"dom:{dom}") if conn is not None else None
        return (ix.subject_for_doc(doc) if doc else f"org/{dom}"), dom, []
    key = tok[:-3] if tok.endswith(".md") else tok
    d, _, slug = key.partition("/")
    if d in DIR_TO_TYPE:
        key = f"{DIR_TO_TYPE[d]}/{slug}"
    if parse_subject(key) is None:
        return None, tok, []
    existing = ix.all_subjects(conn) if conn is not None else set()
    aliases = ix.subject_aliases(conn) if conn is not None else {}
    r = resolve(key, existing, aliases)
    # An owner utterance is one of the ways a subject may come into being,
    # so a well-formed but unknown key mints; the reply names near misses.
    return r.key, r.key, [k for k, _ in r.nearest] if r.how == "miss" else []


def expected_for_message(text: str, conn, store, task_sender: str = "") -> list[tuple[str, str, str, str]]:
    """What ledger lines a given owner message may legitimately have minted:
    (op, subject, facet, text-or-ref). The verifier recomputes this from the
    fetched Slack message and requires the ledger line to match one of them,
    so a forged line cannot borrow a real owner message it did not come from."""
    p = parse_command(text)
    if p is None:
        return []
    out: list[tuple[str, str, str, str]] = []
    if p.verb == "rule":
        if not p.args:
            return []
        if OFFER_RE.match(p.args[0]) and store is not None:
            offer = store.get_offer(p.args[0])
            if offer:
                facet = DISPOSITION_FACET if offer["kind"] == "disposition" else PREFERENCE_FACET
                out.append(("rule", offer["subject"], facet, offer["text"]))
            return out
        args = list(p.args)
        if args[0].lower() in ACTIONS and task_sender:
            args = [task_sender, *args]
        if len(args) < 2:
            return []
        subj, display, _ = _target_to_subject(args[0], conn, store)
        if subj is None:
            return []
        if args[1].lower() in ACTIONS:
            out.append(("rule", subj, DISPOSITION_FACET, rule_text(args[1].lower(), display, " ".join(args[2:]))))
        else:
            out.append(("rule", subj, PREFERENCE_FACET, clean_text(" ".join(args[1:]), 300)))
    elif p.verb in ("attest", "forget", "pin"):
        if p.args:
            ref = normalize_ref(p.args[0])
            if ref:
                op = {"attest": "attest", "forget": "retire", "pin": "pin"}[p.verb]
                out.append((op, "", "", ref))
                if p.verb == "forget":
                    out.append(("veto", "", "", ref))
    elif p.verb == "unretire" and p.args:
        out.append(("unretire", "", "", p.args[0]))
    return out


def handle(ctx: Context, conn, store, owner_ids: list[str]) -> Minted:
    """Turn an owner command into ledger lines and a reply. Never opens a
    session, never costs a run."""
    if not owner_ids:
        return Minted(reply="Memory rules are off: set WANDA_MEMORY_OWNER_USER_IDS to the people whose word should count.", ok=False)
    if ctx.user not in owner_ids:
        return Minted(reply="Only the configured owners can set memory rules.", ok=False)
    p = parse_command(ctx.text)
    if p is None:
        return Minted(reply="", ok=False)
    verb = p.verb
    base = dict(src="owner", cause=ctx.cause, when=ctx.when)
    if verb == "rule":
        if not p.args:
            return Minted(reply="Usage: `rule <address|domain|subject> trash|ignore|attention [note]`, `rule <subject> <preference>`, or `rule k4` to accept an offer.", ok=False)
        if OFFER_RE.match(p.args[0]):
            offer = store.get_offer(p.args[0])
            if not offer:
                return Minted(reply=f"No offer {p.args[0]}.", ok=False)
            facet = DISPOSITION_FACET if offer["kind"] == "disposition" else PREFERENCE_FACET
            o = Observation(subject=offer["subject"], facet=facet, text=offer["text"], op="rule", **base)
            store.take_offer(p.args[0])
            return Minted([o], f"Rule recorded: _{offer['text']}_")
        args = list(p.args)
        if args[0].lower() in ACTIONS and ctx.task_sender:
            args = [ctx.task_sender, *args]
        if len(args) < 2:
            return Minted(reply="Say what the rule is: `rule <who> trash|ignore|attention` or `rule <who> <preference>`.", ok=False)
        subj, display, nearest = _target_to_subject(args[0], conn, store)
        if subj is None:
            hint = ("Nearest: " + ", ".join(nearest[:10])) if nearest else "Use an email address, a domain, or an existing subject like `person/robin-vale`."
            return Minted(reply=f"I don't know `{args[0]}`. {hint}", ok=False)
        near = f" (note: `{subj}` is new; nearest existing: {', '.join(nearest[:3])})" if nearest else ""
        if args[1].lower() in ACTIONS:
            text = rule_text(args[1].lower(), display, " ".join(args[2:]))
            o = Observation(subject=subj, facet=DISPOSITION_FACET, text=text, op="rule", **base)
            return Minted([o], f"Rule recorded: _{text}_ — applies to triage from the next batch.{near}")
        text = clean_text(" ".join(args[1:]), 300)
        o = Observation(subject=subj, facet=PREFERENCE_FACET, text=text, op="rule", **base)
        return Minted([o], f"Preference recorded for `{subj}`: _{text}_{near}")
    if verb in ("attest", "forget", "pin"):
        if not p.args:
            return Minted(reply=f"Usage: `{verb} people/<note>#c4`", ok=False)
        ref = normalize_ref(p.args[0])
        if not ref:
            return Minted(reply=f"`{p.args[0]}` is not a claim reference (expected `people/<note>#c4`).", ok=False)
        doc, _, block = ref.partition("#^")
        row = conn.execute("SELECT * FROM claims WHERE doc=? AND block=?", (doc, block)).fetchone() if conn is not None else None
        if row is None:
            return Minted(reply=f"No claim at `{ref}`.", ok=False)
        subj = ix.subject_for_doc(doc) or "pref/general"
        if verb == "attest":
            o = Observation(subject=subj, facet="attest", text=f"Alex confirmed: {row['text']}", op="attest", ref=ref, **base)
            return Minted([o], f"Confirmed as your word: _{row['text']}_")
        if verb == "pin":
            o = Observation(subject=subj, facet="pin", text=f"Pinned: {row['text']}", op="pin", ref=ref, **base)
            return Minted([o], f"Pinned: _{row['text']}_ — wanda will not rewrite or fold it.")
        # forget: retire the claim AND veto every recurrence key that produced it.
        keys = [r["key"] for r in conn.execute(
            "SELECT DISTINCT k.key FROM edges e JOIN rkeys k ON k.ulid=e.dst_block "
            "WHERE e.src_doc=? AND e.src_block=? AND e.rel='derived-from'", (doc, block))]
        keys = keys or [f"key:{subj}|"]
        retire = Observation(subject=subj, facet="retire", text=f"Forgotten: {row['text']}", op="retire", ref=ref, **base)
        veto = Observation(subject=subj, facet="veto", text="Vetoed the pattern behind a forgotten claim", op="veto",
                           ref=",".join(sorted(set(keys))), **base)
        return Minted([retire, veto], f"Forgotten, and the pattern behind it is suppressed for a year: _{row['text']}_")
    if verb == "unretire":
        if not p.args:
            return Minted(reply="Usage: `unretire people/<note>.md`", ok=False)
        path = p.args[0].strip()
        o = Observation(subject="pref/general", facet="unretire", text=f"Restore {path}", op="unretire", ref=path, **base)
        return Minted([o], f"Restoring `{path}` on the next pass.")
    return Minted(reply="", ok=False)
