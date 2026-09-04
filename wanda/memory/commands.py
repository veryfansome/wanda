"""Owner commands from Slack: the one path by which anything becomes
owner-tier memory. Parsed by the harness before any session runs, minted
in-process with `cause=slack:<channel>:<ts>`, and re-verified against Slack
by the hourly pass — a session can post as the bot, never as the owner.

Parsing is strict on purpose: "forget it, thanks" or "rule of thumb: …"
must never be swallowed as a command."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from wanda.memory import index as ix
from wanda.memory.ledger import Observation
from wanda.memory.subjects import parse_subject, registrable_domain, resolve, subject_from_address
from wanda.memory.vault import DIR_TO_TYPE, clean_text

def _subjects_for_target(token: str, conn) -> set[str]:
    """Every subject key a target may legitimately have resolved to, now or
    when it was minted: the address-derived key, the note it belongs to now,
    and whatever aliases join them. The verifier accepts any of these, so a
    rule minted before the sender got a note is not quarantined after."""
    tok = _clean_token(token).lower()
    out: set[str] = set()
    if "@" in tok and addresses_in(tok):
        addr = addresses_in(tok)[0]
        out.add(subject_from_address(addr) or f"person/{addr}")
    elif "/" not in tok and DOMAIN_RE.match(tok):
        out.add(f"org/{registrable_domain(tok)}")
    subj, _, _ = _target_to_subject(token, conn)
    if subj:
        out.add(subj)
    if conn is not None:
        for s in list(out):
            out.add(ix.canonical_subject(conn, s))
    return out
from wanda.triage import addresses_in

VERBS = ("rule", "attest", "forget", "pin")
ACTIONS = ("trash", "ignore", "attention")
MENTION_PREFIX = r"^\s*(?:<@[A-Z0-9]+(?:\|[^>]*)?>\s*)?"
COMMAND_RE = re.compile(MENTION_PREFIX + r"(rule|attest|forget|pin)\b(.*)$", re.I | re.S)
OFFER_ONLY_RE = re.compile(MENTION_PREFIX + r"(k\d+)\s*$", re.I)
REF_RE = re.compile(r"^(?P<doc>(people|orgs|topics|prefs|open)/[^#\s/]+?)(?:\.md)?#\^?(?P<block>[a-z0-9]{1,24})$")
OFFER_RE = re.compile(r"^k\d+$", re.I)
DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
DISPOSITION_FACET = "mail-disposition"
PREFERENCE_FACET = "preference"


@dataclass
class Parsed:
    verb: str
    args: list[str]


def _clean_token(tok: str) -> str:
    """Slack wraps addresses as <mailto:a@b|a@b> and links as <http://x|x>."""
    t = tok.strip().strip("`").strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1]
        if "|" in t:
            t = t.split("|", 1)[-1]
        t = t.removeprefix("mailto:")
    return t


def target_like(tok: str) -> bool:
    """Does this token name something a rule can be about: an address, a
    domain, a subject key, or a note path?"""
    t = _clean_token(tok).lower()
    if "@" in t:
        return bool(addresses_in(t))
    if "/" in t:
        d, _, slug = t.partition("/")
        if d in DIR_TO_TYPE:
            t = f"{DIR_TO_TYPE[d]}/{slug[:-3] if slug.endswith('.md') else slug}"
        return parse_subject(t) is not None
    return bool(DOMAIN_RE.match(t))


def parse_command(text: str) -> Parsed | None:
    """Return a Parsed command only for a message that has the shape of one.
    Ordinary prose that happens to start with a verb word returns None and
    goes to a session like any other message."""
    m = OFFER_ONLY_RE.match(text or "")
    if m:
        return Parsed("rule", [m.group(1).lower()])
    m = COMMAND_RE.match(text or "")
    if not m:
        return None
    verb = m.group(1).lower()
    args = m.group(2).split()
    if verb == "rule":
        if not args:
            return None
        if OFFER_RE.match(args[0]) and len(args) == 1:
            return Parsed(verb, [args[0].lower()])
        if args[0].lower() in ACTIONS:
            return Parsed(verb, args)  # parsed so `handle` can say a rule must name its target
        if len(args) >= 2 and target_like(args[0]):
            return Parsed(verb, args)
        return None
    if verb in ("attest", "forget", "pin"):
        return Parsed(verb, args) if len(args) == 1 and normalize_ref(args[0]) else None
    return None


def is_command(text: str) -> bool:
    return parse_command(text) is not None


def normalize_ref(ref: str) -> str | None:
    """`people/x#c4`, `people/x.md#^c4` -> `people/x.md#^c4`. Rejects
    anything that could leave the vault."""
    m = REF_RE.match(_clean_token(ref))
    if not m or ".." in m.group("doc"):
        return None
    return f"{m.group('doc')}.md#^{m.group('block')}"


class CannotRecompute(Exception):
    """The line a message may have minted cannot be recomputed now (the claim
    it quotes has left the index). Not a verdict about the line."""


# Duplicated from passes.GENERAL_PREF_SUBJECT on purpose: `passes` imports
# `commands`, so a shared constant here would be an import cycle.
GENERAL_PREF = "pref/general"
# A disposition offer's text is exactly `<action> mail from <address>`. The
# `\S+` target is what rejects a row whose text smuggles anything past the
# address; the rule_text() equality below is what pins the action.
OFFER_TEXT_RE = re.compile(r"^\S+ mail from (?P<target>\S+)$")


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

    @property
    def ok(self) -> bool:
        return bool(self.observations)


@dataclass
class Context:
    channel: str
    ts: str
    user: str
    text: str
    when: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def cause(self) -> str:
        return f"slack:{self.channel}:{self.ts}"


def _target_to_subject(token: str, conn) -> tuple[str | None, str, list[str]]:
    """Resolve a rule's target: an address, a domain, a subject key or a note
    path. Returns (subject, display, nearest-existing-keys-if-new)."""
    tok = _clean_token(token).lower()
    if "@" in tok and addresses_in(tok):
        addr = addresses_in(tok)[0]
        doc = ix.doc_for_id(conn, f"mailto:{addr}") if conn is not None else None
        subj = ix.subject_for_doc(doc) if doc else subject_from_address(addr)
        return subj, addr, []
    if "/" not in tok and DOMAIN_RE.match(tok):
        dom = registrable_domain(tok)
        doc = ix.doc_for_id(conn, f"dom:{dom}") if conn is not None else None
        return (ix.subject_for_doc(doc) if doc else f"org/{dom}"), dom, []
    key = tok[:-3] if tok.endswith(".md") else tok
    d, _, slug = key.partition("/")
    if d in DIR_TO_TYPE:
        key = f"{DIR_TO_TYPE[d]}/{slug}"
    if parse_subject(key) is None:
        return None, tok, []
    if conn is None:
        return key, key, []
    r = resolve(key, ix.all_subjects(conn), ix.subject_aliases(conn))
    # An owner utterance is one of the ways a subject may come into being,
    # so a well-formed but unknown key mints; the reply names near misses.
    return r.key, r.key, [k for k, _ in r.nearest] if r.how == "miss" else []


def _claim_for_ref(ref: str, conn) -> tuple[str, str, str, str] | None:
    """(doc, block, claim text, subject) for a normalized ref, or None when
    the index has no such claim. `handle` mints from this and
    `expected_for_message` recomputes from it, so the two cannot drift."""
    doc, _, block = ref.partition("#^")
    row = conn.execute("SELECT text FROM claims WHERE doc=? AND block=?",
                       (doc, block)).fetchone() if conn is not None else None
    if row is None:
        return None
    return doc, block, row["text"], ix.subject_for_doc(doc) or GENERAL_PREF


def expected_for_message(text: str, conn, store) -> list[tuple[str, str, str, str, str]]:
    """What ledger lines a given owner message may legitimately have minted:
    the whole line, (op, subject, facet, text, ref) — one per subject a rule's
    target may resolve to, one per line a ref verb mints. The verifier
    recomputes this from the fetched
    Slack message and requires the ledger line to match one of them field for
    field, so a forged line cannot pick its own subject, facet, text or ref
    under a message the owner really wrote. A rule's fields are built from
    the message; attest, pin, retire and veto quote a claim, so their fields
    are recomputed from the index — which means a session that also rewrites
    that claim can still make a forged line match, and the note edit is what
    surfaces it, reported as a hand-edit.

    Raises CannotRecompute when the claim a ref names is no longer in the
    index: "we cannot recompute it" must never read as "a session forged it".
    """
    p = parse_command(text)
    if p is None:
        return []
    out: list[tuple[str, str, str, str, str]] = []
    if p.verb == "rule":
        if OFFER_RE.match(p.args[0]) and len(p.args) == 1:
            offer = store.get_offer(p.args[0]) if store is not None else None
            if not offer:
                return out
            if offer["kind"] == "disposition":
                # Re-derive the templated text from the offer's own fields; a
                # forged offer row cannot smuggle prose into a disposition.
                # The target is in the offer's own TEXT, not in its subject:
                # make_offers builds the text from rule_text(action, address)
                # but the subject from subject_from_address(address), which
                # for a role or list address is org/<registrable domain>.
                # Reading the slug therefore compared that domain against the
                # address and rejected every role-address offer.
                m = OFFER_TEXT_RE.match(str(offer["text"]))
                target = m.group("target") if m else ""
                if offer["action"] not in ACTIONS or not target or rule_text(offer["action"], target) != offer["text"]:
                    return out
                if not ((addresses_in(target) == [target] and subject_from_address(target) == offer["subject"])
                        or f"org/{target}" == offer["subject"]):
                    return out
                for subj in ({offer["subject"]} | ({ix.canonical_subject(conn, offer["subject"])} if conn is not None else set())):
                    out.append(("rule", subj, DISPOSITION_FACET, offer["text"], ""))
            # Anything else mints nothing. A disposition's text is rebuilt
            # above from the offer's own action and address, so a rewritten
            # row is caught; free text has nothing to rebuild it from, so
            # accepting one would take a wanda.db row as the owner's word.
            return out
        args = list(p.args)
        if args[0].lower() in ACTIONS:
            # A rule must name its target. The bare in-thread form used to
            # take it from the task's sender, which is a wanda.db row the
            # verifier re-read at check time — so a rewritten row decided
            # which address a genuine message was held to have named.
            return []
        subj, display, _ = _target_to_subject(args[0], conn)
        if subj is None:
            return []
        for s in _subjects_for_target(args[0], conn) | {subj}:
            if args[1].lower() in ACTIONS:
                out.append(("rule", s, DISPOSITION_FACET, rule_text(args[1].lower(), display, " ".join(args[2:])), ""))
            else:
                out.append(("rule", s, PREFERENCE_FACET, clean_text(" ".join(args[1:]), 300), ""))
    elif p.verb in ("attest", "forget", "pin"):
        # Recompute the whole line the way `handle` minted it. clean_text is
        # not optional: format_line stores clean_text(o.text), which rewrites
        # backticks, <>, :: and truncates at 600 bytes, while a hand-written
        # claim may contain any of them — so without it a genuine attest of
        # such a claim reads as a forgery.
        ref = normalize_ref(p.args[0])
        found = _claim_for_ref(ref, conn)
        if found is None:
            raise CannotRecompute(f"no claim at {ref}")
        doc, block, claim_text, subj = found
        if p.verb == "attest":
            out.append(("attest", subj, "attest", clean_text(f"Confirmed by the owner: {claim_text}"), ref))
        elif p.verb == "pin":
            out.append(("pin", subj, "pin", clean_text(f"Pinned: {claim_text}"), ref))
        else:
            for o in forget_observations(conn, doc, block, claim_text, subj):
                out.append((o.op, o.subject, o.facet, clean_text(o.text), o.ref))
    return out


def forget_observations(conn, doc: str, block: str, text: str, subject: str, **base) -> list[Observation]:
    """Retire a claim AND veto every recurrence key that produced it — a veto
    suppresses the cause, not just the symptom. Shared by Slack and the CLI."""
    ref = f"{doc}#^{block}"
    keys = [r["key"] for r in conn.execute(
        "SELECT DISTINCT k.key FROM edges e JOIN rkeys k ON k.ulid=e.dst_block "
        "WHERE e.src_doc=? AND e.src_block=? AND e.rel='derived-from'", (doc, block))] if conn is not None else []
    keys = keys or [f"key:{subject}|"]
    # The veto's ref is the key set the claim was derived from; verification
    # recomputes it by calling this same function, so the two cannot drift.
    retire = Observation(subject=subject, facet="retire", text=f"Forgotten: {text}", op="retire", ref=ref, **base)
    veto = Observation(subject=subject, facet="veto", text="Vetoed the pattern behind a forgotten claim", op="veto",
                       ref=",".join(sorted(set(keys))), **base)
    return [retire, veto]


def handle(ctx: Context, conn, store, owner_ids: list[str]) -> Minted:
    """Turn an owner command into ledger lines and a reply. Never opens a
    session, never costs a run."""
    if not owner_ids:
        return Minted(reply="Memory rules are off: set WANDA_MEMORY_OWNER_USER_IDS to the people whose word should count.")
    if ctx.user not in owner_ids:
        return Minted(reply="Only the configured owners can set memory rules.")
    p = parse_command(ctx.text)
    if p is None:
        return Minted(reply="")
    base = dict(src="owner", cause=ctx.cause, when=ctx.when)
    if p.verb == "rule":
        if OFFER_RE.match(p.args[0]) and len(p.args) == 1:
            offer = store.get_offer(p.args[0])
            if not offer:
                return Minted(reply=f"No offer {p.args[0]}.")
            if offer["taken_at"]:
                # An offer is single-use: minting again would just duplicate
                # the rule (_apply_rule's supersede branch skips an identical
                # text, so the second claim stays live alongside the first).
                return Minted(reply=f"Offer {p.args[0]} is already your word: _{offer['text']}_")
            if offer["kind"] != "disposition":
                # Unverifiable by construction: see expected_for_message.
                # Nothing produces these; a row that says otherwise is not
                # the owner's word.
                return Minted(reply=f"Offer {p.args[0]} is not one I can turn into a rule.")
            o = Observation(subject=offer["subject"], facet=DISPOSITION_FACET, text=offer["text"], op="rule", **base)
            store.take_offer(p.args[0])
            return Minted([o], f"Recorded as your word: _{offer['text']}_")
        args = list(p.args)
        if args[0].lower() in ACTIONS:
            return Minted(reply="Say who the rule is about: `rule <address|domain|subject> trash|ignore|attention`.")
        subj, display, nearest = _target_to_subject(args[0], conn)
        if subj is None:
            return Minted(reply=f"I don't know `{args[0]}`. Use an email address, a domain, or a subject like `person/robin-vale`.")
        near = f" (`{subj}` is new; nearest existing: {', '.join(nearest[:3])})" if nearest else ""
        if args[1].lower() in ACTIONS:
            text = rule_text(args[1].lower(), display, " ".join(args[2:]))
            o = Observation(subject=subj, facet=DISPOSITION_FACET, text=text, op="rule", **base)
            return Minted([o], f"Rule recorded: _{text}_ — applies to triage from the next batch.{near}")
        text = clean_text(" ".join(args[1:]), 300)
        o = Observation(subject=subj, facet=PREFERENCE_FACET, text=text, op="rule", **base)
        return Minted([o], f"Preference recorded for `{subj}`: _{text}_{near}")
    if p.verb in ("attest", "forget", "pin"):
        ref = normalize_ref(p.args[0])
        found = _claim_for_ref(ref, conn)
        if found is None:
            return Minted(reply=f"No claim at `{ref}`.")
        doc, block, claim_text, subj = found
        if p.verb == "attest":
            o = Observation(subject=subj, facet="attest", text=f"Confirmed by the owner: {claim_text}", op="attest", ref=ref, **base)
            return Minted([o], f"Confirmed as your word: _{claim_text}_")
        if p.verb == "pin":
            o = Observation(subject=subj, facet="pin", text=f"Pinned: {claim_text}", op="pin", ref=ref, **base)
            return Minted([o], f"Pinned: _{claim_text}_ — wanda will not rewrite or fold it.")
        obs = forget_observations(conn, doc, block, claim_text, subj, **base)
        return Minted(obs, f"Forgotten, and the pattern behind it is suppressed for a year: _{claim_text}_")
    return Minted(reply="")
