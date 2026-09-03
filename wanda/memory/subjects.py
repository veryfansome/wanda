from __future__ import annotations

import re
from dataclasses import dataclass

from wanda.memory.vault import SUBJECT_TYPES, slugify
from wanda.triage import addresses_in

# Local parts that mean "the organisation is the sender", not a person.
ROLE_LOCALS = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|info|announce(ments)?|newsletters?|"
    r"notifications?|notify|mailer-daemon|postmaster|support|hello|hi|team|news|updates?|alerts?|"
    r"marketing|bounces?|mail|email|noreply.*|.*-noreply|no-reply.*|digest|billing|receipts?|"
    r"orders?|service|customerservice|contact|admin|office|communications?|community|events?)$",
    re.I,
)
# Two-label suffixes under which the registrable domain is three labels.
SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac", "ne", "or", "go"}

NEAR_TRIGRAM = 0.6
NEAR_TRIGRAM_STRICT = 0.75  # people and orgs: names must almost match


def parse_subject(s: str) -> tuple[str, str] | None:
    if not s or "/" not in s:
        return None
    t, _, slug = s.partition("/")
    if t not in SUBJECT_TYPES or not slug:
        return None
    if t == "person" and "@" in slug:
        if not re.fullmatch(r"[a-z0-9._+-]+@[a-z0-9.-]+\.[a-z]{2,}", slug):
            return None
    elif not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,63}", slug):
        return None
    return t, slug


def registrable_domain(domain: str) -> str:
    labels = [x for x in domain.lower().strip(".").split(".") if x]
    if len(labels) <= 2:
        return ".".join(labels)
    if labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_role_address(addr: str) -> bool:
    local = addr.split("@", 1)[0]
    return bool(ROLE_LOCALS.match(local))


def subject_from_address(from_addr: str, list_id: str = "") -> str | None:
    """Deterministic minting from an email header, no model, no judgment.
    A person keys on the FULL lowercased address, so a spoofer's
    mei.delgado@evil.example can never land on the family's mei.delgado@icloud.com.
    Role addresses and list mail key on the registrable domain as an org."""
    addrs = addresses_in(from_addr or "")
    if not addrs:
        return None
    addr = addrs[0]
    domain = addr.rsplit("@", 1)[-1]
    if list_id or is_role_address(addr):
        return f"org/{registrable_domain(domain)}"
    return f"person/{addr}"


def subject_from_slack(user_id: str) -> str:
    return f"person/slack-{user_id.lower()}"


# --- nearest match --------------------------------------------------------------

def _trigrams(s: str) -> set[str]:
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _tokens(slug: str) -> set[str]:
    return {t for t in re.split(r"[-._@]+", slug) if len(t) > 1}


@dataclass
class Resolution:
    key: str
    how: str                 # exact | alias | near | miss
    nearest: list[tuple[str, float]]


def resolve(candidate: str, existing: set[str], aliases: dict[str, str]) -> Resolution:
    """Exact and alias hits resolve verbatim. A new slug is first compared
    with what exists; a near miss returns the existing key and says so, so a
    session cannot mint `topic/hoa-election` beside `topic/hoa-board-election`.
    Only a real miss mints — and the caller reports every mint in the digest."""
    # Aliases first: a retired or merged key may still appear in `existing`
    # (the ledger remembers it), and must resolve to its successor.
    if candidate in aliases:
        return Resolution(aliases[candidate], "alias", [])
    if candidate in existing:
        return Resolution(candidate, "exact", [])
    parsed = parse_subject(candidate)
    if parsed is None:
        return Resolution(candidate, "miss", [])
    t, slug = parsed
    if t == "person" and "@" in slug:
        return Resolution(candidate, "miss", [])  # addresses are exact by construction
    scored: list[tuple[str, float]] = []
    ctoks = _tokens(slug)
    for k in existing:
        kt, _, kslug = k.partition("/")
        if kt != t or "@" in kslug:
            continue
        sim = trigram_similarity(slug, kslug)
        if t in ("topic", "pref", "list"):
            shared = ctoks & _tokens(kslug)
            if len(shared) >= 2 and (ctoks <= _tokens(kslug) or _tokens(kslug) <= ctoks):
                sim = max(sim, 0.99)
        scored.append((k, sim))
    scored.sort(key=lambda x: -x[1])
    threshold = NEAR_TRIGRAM if t in ("topic", "pref", "list") else NEAR_TRIGRAM_STRICT
    if scored and scored[0][1] >= threshold:
        return Resolution(scored[0][0], "near", scored[:10])
    return Resolution(candidate, "miss", scored[:10])


# --- recurrence keys ----------------------------------------------------------------

STOP = {"the", "a", "an", "for", "of", "and", "to", "in", "on", "your", "you", "re", "fwd", "fw", "is", "at"}
# Calendar words collapse so "September closure dates" and "October closure
# dates" recur on the same shape.
CALENDAR = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
}


def subject_shape(subject_hdr: str) -> str:
    s = (subject_hdr or "").lower()
    s = re.sub(r"^(\s*(re|fwd|fw|aw|sv)\s*:\s*)+", "", s)
    s = re.sub(r"\[[^\]]{0,40}\]", " ", s)
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"[^a-z#\s]+", " ", s)
    s = re.sub(r"#(\s*#)+", "#", s)
    return " ".join(t for t in s.split() if t not in STOP and t not in CALENDAR)[:72]


def keys_for(subject: str, facet: str, from_addr: str = "", list_id: str = "", subject_hdr: str = "") -> list[str]:
    out = [f"key:{subject}|{facet}"]
    for a in addresses_in(from_addr or ""):
        out.append(f"addr:{a}")
        out.append(f"dom:{a.rsplit('@', 1)[-1]}")
    if list_id:
        out.append(f"list:{list_id.strip('<> ').lower()}")
    if subject_hdr:
        shape = subject_shape(subject_hdr)
        if shape:
            out.append(f"shape:{shape}")
    return out
