"""A memory session: what it is handed, the vault it works in, and what comes
back. One fresh session per turn of a conversation, as the lab measures them.

The prompt and the frames are the shape `mem session` reads back out of a
transcript (memory/src/transcript.rs). The prompt, the tools, the schema and
the date paragraph after its first sentence are the lab's
(lab/harness/src/arrival.rs and session.rs); tests/test_vault.py holds each
copy to its original."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wanda import household
from wanda.config import Config
from wanda.transcript import harmless, is_mine, plain

log = logging.getLogger(__name__)

PROMPT = (
    "I am wanda.\n\nToday is {date}.\n\n{arrival}\n\nDo three things, in this order.\n\n"
    "First, work out what is already known that bears on this. Read the indexes,\n"
    "navigate to what looks relevant, and use `mem recall` on the two or three\n"
    "things this is actually about. Put what was found in `recalled`, most relevant\n"
    "first, and what would be said back in `answer`.\n\n"
    "Second, record what should be remembered from it, using `mem`.\n\n"
    "Third, before finishing, invoke the `enrich` skill: link what this session wrote to\n"
    "what was already here. Then list what this session wrote, edges included, in `recorded`.\n\n"
    "Run mem as: mem\n"
)

# What each place is called in its frame, and what the messages before this
# one are called there. Anyone in this Slack can open a public channel and its
# threads without joining, so those frames say so rather than counting only
# the members as readers.
PLACES = {
    "dm": ("a direct message", "The conversation so far:"),
    "group": ("a group direct message", "The conversation so far:"),
    "channel": ("a Slack channel", "The conversation so far:"),
    "public": ("a public Slack channel", "The conversation so far:"),
    "thread": ("a Slack thread", "The thread so far:"),
    "public thread": ("a Slack thread in a public channel", "The thread so far:"),
}
EVERYONE = " Everyone in it sees what I say there."
# After the name of everyone neither her nor on the allowlist, a person or an
# app, wherever a frame other than a 1:1 DM's names them: among its readers,
# at the head of what they said and where a message mentions them.
OUTSIDE = " (outside the household)"
# the same, for one whose chosen name is, or looks like (`alike`), one a
# member goes or went by, so that their words are not read as that member's
OUTSIDE_NAMESAKE = " (another person in this Slack, outside the household)"
# after an allowed id not let in whose name is, or looks like, a member's,
# and after anyone so named in a 1:1 DM
NAMESAKE = " (another person in this Slack)"
# a mention past what a frame looks up, and a poster with no name to use
SOMEONE = "someone" + OUTSIDE
# on a public frame's opening line when anyone outside the household can read
# it; elsewhere the marks on its readers say so
OUTSIDERS_READ = " Some who can read it are outside the household."
# on the opening line, after OUTSIDERS_READ, when Slack would not say who is
# in the conversation: the readers named are then the turn's speakers
UNLISTED = " I could not find out who else is in it."
# on the opening line, after those, once for each earlier session that took
# the turn's messages and ended without answering them, oldest first: the
# session it retries, the one whose later turn failed after an answer, or one
# a stop or a crash cut short.
# {sid8} is the session's first eight characters, which `mem session` takes
# as a prefix of its id.
RETRIED = ("An earlier session of mine for this, {sid8}, ended before I answered; what it wrote to memory is "
           "still there.")
# on the opening line, before RETRIED's, when the turn's newest message
# reaches its session late, as after a stop: the session is framed at its own
# start, and told when the message was sent, and each interval she was not
# running since the turn's oldest one, so that it answers knowing how long
# ago it was asked
LATE_TURN = "What {speaker} says below was sent at {sent} and reaches me only now."
DOWN = "I was not running from {since} until {until}."
ME = "me"
# Who posted one of her alerts, as a frame shows it: an alert carries the
# harness's words, so it is never shown as something she said.
ALERTED = "an alert posted in my name"
# Past this many readers besides her, a frame names the household's, members
# and allowed ids, and counts the rest without looking them up, as CROWD.
NAMED_READERS = 12
CROWD = "{n} others outside the household"
# Outside a thread, the conversation so far is its last RECENT_HOURS: the
# household's newest EARLIER lines, hers among them, and up to OUTSIDE_EARLIER
# of anyone else's from the oldest of those on, so that others' lines never
# push the household's out of view. A reply sent after midnight still arrives
# with what it answers. What came before is in the vault, and in the
# transcripts `mem session` reads.
RECENT_HOURS = 12
EARLIER = 20
OUTSIDE_EARLIER = 20
# A post marked outside the household is cut once what the frame shows of it
# after its line head, its labels and indents included, would pass CUT_AT
# characters, so that no one else's posts can fill the frame; the household's
# posts and her alerts never are.
CUT_AT = 4000
CUT = " [{n} more characters cut]"
# What a frame shows of all such posts together, the newest first, each still
# cut at CUT_AT; one past it shows only how much was cut. In a dense script,
# CJK or emoji, 49 posts at CUT_AT could pass what a session's context holds,
# which would fail the member's turn.
OUTSIDE_CUT_AT = 40000
# The mark the harness posts its alerts with (Slack message metadata), which
# only an app can attach. An alert is for the people who keep wanda running: a
# session is shown hers as an alert posted in her name, never as something she
# said. NOTE_EVENT is on failure notes posted in Claude Code's words, which
# conversations still hold: a session is not shown one. Her own notes carry no
# mark, and are shown as hers.
ALERT_EVENT = "wanda_alert"
NOTE_EVENT = "wanda_note"

# What a quote in a chosen name becomes, so that the quotation marks a frame
# puts around the name are never closed inside it.
SINGLE = "\u2019"
# Two or more single quote marks in a row read as a double one (TeX closes a
# quotation with ''), so a run of them in a chosen name, SINGLE and the marks
# that look like it, becomes one SINGLE, invisible and combining characters
# between them included, since they show nothing between the marks.
SINGLES = frozenset("'`\u00b4\u02b9\u02bb\u02bc\u02c8\u05f3\u1fef\u1ffd\u2019\u2032\u2035\ua78b\ua78c\uff07")
# The 22 double quotation marks and corner brackets of Unicode's Quotation_Mark
# property, and 17 marks outside it that look like one. Any other character
# Unicode files as an opening or closing quote (Pi, Pf) but SINGLE is caught
# too (`_quote`).
QUOTES = frozenset(
    '"\u00ab\u00bb\u201c\u201d\u201e\u201f\u2e42\u300c\u300d\u300e\u300f'
    '\u301d\u301e\u301f\ufe41\ufe42\ufe43\ufe44\uff02\uff62\uff63'
    '\u2033\u2036\u02ba\u02dd\u3003\u05f4'
    '\u02ee\u02f6\u2034\u2057\u275d\u275e\u2760\u1cd3\U0001f676\U0001f677\U0001f678')
# Unicode's Default_Ignorable_Code_Point, the 17 ranges of
# DerivedCoreProperties.txt (Unicode 15.0), which Python's unicodedata does
# not expose: zero-width characters, the soft hyphen and the Hangul fillers
# among them.
IGNORABLE = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
             (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
             (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
             (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
# The 50 Cyrillic and Greek letters, upper and lower case, that look Latin, as
# the Latin letters they look like.
LOOKALIKE = str.maketrans(
    "\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0425\u0405\u0406\u0408"
    "\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u0455\u0456\u0458\u0501\u04bb\u04cf\u051b\u051d"
    "\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7"
    "\u03b1\u03bf\u03c1\u03b9\u03ba\u03bd\u03c5",
    "ABEKMHOPCTXSIJ" "aeopcyxsijdhlqw" "ABEZHIKMNOPTYX" "aopikvu")

TODAY = "Today is {weekday}, {date}, and it is {time} here ({zone}) as this session begins. "
AFTER_DATE = (
    "Any other date I am shown is the machine's, not mine — the date in my system prompt, "
    "the date the shell reports, the timestamps on files. Every judgement about the date, "
    "about how long ago something happened, and about what is overdue uses {date} as now. "
    "Everything I know about these people comes from this history and from the vault I am "
    "working in. My surroundings are not part of it: the machine, files outside the vault, "
    "the shell environment, the git repository and whatever account this session is signed "
    "in as tell me nothing about anyone here, and none of it belongs in memory. Working out "
    "what was meant, and what it implies, from what was actually said is exactly my job."
)

TOOLS = "Read,Glob,Grep,Bash,Skill"
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recalled", "answer", "recorded"],
    "properties": {
        "recalled": {
            "type": "array",
            "description": "what I brought to bear on this, most relevant first, as node ids or names",
            "items": {"type": "string"},
        },
        "answer": {
            "type": "string",
            "description": "what I would say back, empty if I would say nothing",
        },
        "recorded": {
            "type": "array",
            "description": "one line per thing I wrote to memory",
            "items": {"type": "string"},
        },
    },
}
# A session that did the work and then filled the schema with scaffolding.
# Matched whole and normalised, never as a substring or on length: a short
# answer is still an answer.
PLACEHOLDER = (
    "test", "test entry", "testing", "todo", "tbd", "placeholder",
    "example", "sample", "n a", "answer here", "your answer", "my answer", "dummy",
)


def settings_problem(cfg: Config) -> str | None:
    if not cfg.slack_owner_user_ids:
        return ("WANDA_SLACK_OWNER_USER_IDS is empty: anyone in the workspace could start "
                "a session that reads the household's memory")
    if not cfg.tz:
        return "WANDA_TZ is not set: every session is told the household's date and time in it"
    try:
        ZoneInfo(cfg.tz)
    except (ZoneInfoNotFoundError, ValueError):
        return f"WANDA_TZ={cfg.tz} is not a time zone this machine knows"
    if cfg.memory_sessions not in (1, 2):
        return f"WANDA_MEMORY_SESSIONS={cfg.memory_sessions}: memory sessions run one or two at a time"
    return None


# --- who is in a conversation ---

def full_members(people: list[dict]) -> list[str]:
    """Everyone in this Slack who can open a public channel without joining
    it: not a guest, a bot, or a deactivated account."""
    return [u["id"] for u in people if u.get("id") and u["id"] != "USLACKBOT" and not (
        u.get("is_bot") or u.get("deleted") or u.get("is_restricted") or u.get("is_ultra_restricted"))]


# --- what a session is handed ---

def where(p: dict) -> str:
    public = p.get("channel_type") == "channel"
    if p.get("in_thread"):
        return "public thread" if public else "thread"
    return {"im": "dm", "mpim": "group", "channel": "public"}.get(p.get("channel_type") or "", "channel")


def _ignorable(c: str) -> bool:
    o = ord(c)
    return any(lo <= o <= hi for lo, hi in IGNORABLE)


def fold(name: str) -> str:
    """The form two names are compared in to tell whether one looks like the
    other, and for nothing else: compatibility forms and accents taken apart
    (NFKD, since NFKC would compose a letter and a dot below back into one
    letter); format, combining, enclosing and default-ignorable characters
    dropped; the look-alike Cyrillic and Greek letters made Latin; casefolded."""
    s = unicodedata.normalize("NFKD", household.spelled(name))
    s = "".join(c for c in s if unicodedata.category(c) not in ("Cf", "Mn", "Me") and not _ignorable(c))
    return s.translate(LOOKALIKE).casefold()


def alike(name: str, namesakes) -> bool:
    """Whether `name` is, or looks like, one of `namesakes`: folded, it is one
    of them folded, or as long as one and different from it only where it
    holds a character outside ASCII. A name in another script as long as a
    member's is caught too; the mark that follows, another person, is true of
    anyone it is given to."""
    f = fold(name)
    return any(f == n or (len(f) == len(n) and all(a == b or not a.isascii() for a, b in zip(f, n)))
               for n in {fold(m) for m in namesakes})


def _quote(c: str) -> bool:
    return c in QUOTES or (c != SINGLE and unicodedata.category(c) in ("Pi", "Pf"))


def _fields(u: dict) -> tuple:
    prof = u.get("profile") or {}
    return prof.get("display_name"), prof.get("real_name"), u.get("name")


def _usable(field: str | None) -> str | None:
    """A field of someone's Slack profile as the name a frame quotes them by:
    on one line, each quote in it SINGLE and each run of quote marks one;
    None when it folds to nothing, `me` or `wanda`, which would read as no
    one or as her."""
    name = household.spelled(field)
    if fold(name) in ("", *household.SELF):
        return None
    return _runs("".join(SINGLE if _quote(c) else c for c in name))


def _runs(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] in SINGLES:
            j, marks, end = i + 1, 1, i + 1
            while j < len(s) and (s[j] in SINGLES or _ignorable(s[j]) or unicodedata.category(s[j]) == "Mn"):
                if s[j] in SINGLES:
                    marks, end = marks + 1, j + 1
                j += 1
            if marks > 1:
                out.append(SINGLE)
                i = end
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _quoted(name: str | None, uid: str, namesakes, *, outside: bool) -> str:
    """Anyone neither her nor a member, by `name` in quotation marks, so that
    it cannot carry the frame's own structure, then their mark; by their id
    when they have no name to use."""
    if name is None:
        return uid + (OUTSIDE if outside else "")
    same = alike(name, namesakes)
    mark = (OUTSIDE_NAMESAKE if same else OUTSIDE) if outside else (NAMESAKE if same else "")
    return f"“{name}”{mark}"


def names(ids, users: dict[str, dict], told: dict[str, str], namesakes, own: frozenset[str], kin,
          marked: bool = True) -> dict[str, str]:
    """Slack id to the name a frame calls each of `ids` by, and every
    member's besides. A member is called by the name sessions are told
    (`told`), and her own ids by Slack's name, so that a mention of her reads
    as her name. Anyone else, where `marked`, by the first of their display
    name, full name and Slack name that can be used, in quotation marks: an
    allowed id (`kin`) unmarked, anyone else outside the household, and either
    as another person where the name is, or looks like (`alike`), one of
    `namesakes`, which a member goes or went by. One with no name to use, or
    whom Slack did not describe (`users`), is called by their id. In a 1:1 DM
    (`marked` false) by the name Slack shows, marked only as another person."""
    out = {}
    for uid in ids:
        if uid in told:
            continue
        u = users.get(uid)
        if uid in own:
            out[uid] = next((f for f in _fields(u or {}) if f), uid)
        elif not marked:
            name = next((n for f in _fields(u or {}) if (n := household.spelled(f))), uid)
            out[uid] = name + (NAMESAKE if alike(name, namesakes) else "")
        else:
            chosen = next((n for f in _fields(u) if (n := _usable(f))), None) if u else None
            out[uid] = _quoted(chosen, uid, namesakes, outside=uid not in kin)
    return out | told


def readers(ids: list[str], users: dict[str, dict], named: dict[str, str], own: frozenset[str],
            told: dict[str, str], kin) -> tuple[list[str], set[str]]:
    """Who can read what is said in a conversation, as a frame names them,
    and the ids among them outside the household: neither her nor an allowed
    id (`kin`). An app is named, since one added to a conversation reads it;
    a deactivated account is not. A reader Slack did not describe is named by
    its id and marked outside: if it is a deactivated account, the frame names
    one reader too many, which errs the safe way. Past NAMED_READERS the
    household's are named and the rest counted, never looked up."""
    people = [uid for uid in ids if uid not in own]
    crowd = len(people) > NAMED_READERS
    out, outside = [], set()
    for uid in people:
        if (users.get(uid) or {}).get("deleted"):
            continue
        ours = uid in told or uid in kin
        if not ours:
            outside.add(uid)
        if ours or not crowd:
            out.append(named.get(uid) or uid + ("" if ours else OUTSIDE))
    out.sort()
    if crowd:
        out.append(CROWD.format(n=len(outside)))
    return out, outside


def message_text(text: str | None, files: list | None, named: dict[str, str]) -> str:
    body = plain(text or "", named).strip()
    if files:
        attached = ", ".join(f if isinstance(f, str) else f.get("name", "file") for f in files)
        body = f"{body} [attached: {attached}]".strip()
    return body


def stamp(at: float, now: datetime) -> str:
    """When an earlier message was sent, in the household's time: the time
    alone on the day of `now`, with the weekday and date on any other."""
    when = datetime.fromtimestamp(at, now.tzinfo)
    return f"{when:%H:%M}" if when.date() == now.date() else f"{when:%a %Y-%m-%d %H:%M}"


def _at(m: dict) -> float | None:
    try:
        return float(m.get("ts") or 0)
    except ValueError:
        return None


def from_outside(m: dict, own: frozenset[str], kin) -> bool:
    """Whether a post is marked outside the household: one whose Slack user
    is neither hers nor an allowed id, as an app posting as itself is, or one
    with no Slack user. A post under an allowed id's user is theirs, even one
    an app sent for them. In a 1:1 DM (`kin` None), where only she and the
    member post, none is."""
    return kin is not None and not is_mine(m, own) and m.get("user") not in kin


def _showable(m: dict, ts: str, own: frozenset[str]) -> bool:
    # her marked failure note carries Claude Code's words; another app's post
    # with the same mark is shown, its poster marked
    if _at(m) is None or m.get("subtype") in ("channel_join", "channel_leave"):
        return False
    mine = is_mine(m, own)
    if mine and (m.get("metadata") or {}).get("event_type") == NOTE_EVENT:
        return False
    return mine or _at(m) < float(ts)


def counts(m: dict, ts: str, own: frozenset[str], kin=None, turn: float | None = None) -> bool:
    """Whether a message read is one of the household's lines a frame can
    show before the message `ts`: a member's, an allowed id's or hers, not her
    note or a join, and after the message hers alone; given `turn`, the time
    of the turn's oldest message, only one before it. `shown` counts the
    household's window by it, and fetch_context stops reading by it."""
    return _showable(m, ts, own) and not from_outside(m, own, kin) and (turn is None or _at(m) < turn)


def _newest(lines: list[dict], n: int) -> list[dict]:
    # lines[-0:] would be every line
    return lines[-n:] if n > 0 else []


def shown(messages: list[dict], ts: str, place: str, own: frozenset[str], now: datetime, *,
          kin=None, thread: int = 50, turn: float | None = None) -> list[dict]:
    """The messages a frame shows before the message `ts`, oldest first,
    picked before their posters are looked up. Outside a thread, the
    household's newest EARLIER of the last RECENT_HOURS, and of anyone else's
    the newest OUTSIDE_EARLIER from the oldest of those on, or over the whole
    span when the household has fewer; in a thread, its first message, where
    it can be shown, and of the household's replies and of others' the newest
    `thread` - 1 each, in the same way. So no one else's lines push the
    household's out of view. `kin`, the allowed ids, is None in a 1:1 DM,
    where every poster is the household's. Her posts after the message are
    kept, as `earlier` says. Given `turn`, the time of the turn's oldest
    message, the hours and the household's count go back from it, and every
    household line from it on is shown besides: a turn that takes many
    messages, sent over a night say, is framed whole however late it runs."""
    in_thread = place.endswith("thread")
    since = (now.timestamp() if turn is None else turn) - RECENT_HOURS * 3600
    lines = [m for m in messages if _showable(m, ts, own) and (in_thread or _at(m) >= since)]
    first, ours_n, theirs_n = [], EARLIER, OUTSIDE_EARLIER
    if in_thread:
        # a thread's first message is the earliest message read; when it
        # cannot be shown, as her failure note cannot, no reply takes its place
        first = [m for m in lines[:1] if m is messages[0]]
        lines, ours_n, theirs_n = lines[len(first):], thread - 1, thread - 1
    ours = _newest([m for m in lines if counts(m, ts, own, kin, turn)], ours_n)
    start = _at(ours[0]) if ours and len(ours) == ours_n else float("-inf")
    if turn is not None:
        ours += [m for m in lines if counts(m, ts, own, kin) and _at(m) >= turn]
    theirs = _newest([m for m in lines if from_outside(m, own, kin) and _at(m) >= start], theirs_n)
    keep = {id(m) for m in ours + theirs}
    return first + [m for m in lines if id(m) in keep]


def _poster(m: dict, named: dict[str, str], namesakes) -> str:
    """Who posted a post marked outside the household, as a frame names
    them. A post with no Slack user is named by the name it was posted under,
    by the same rule as anyone else's chosen name."""
    if user := m.get("user"):
        return named.get(user) or user + OUTSIDE
    name = _usable(m.get("username"))
    return SOMEONE if name is None else _quoted(name, "", namesakes, outside=True)


def _labelled(text: str, label: str, *, cut: int | None = None) -> tuple[str, int]:
    """A post's text with `label` and ": " at the head of every further line
    but a blank one, so that none reads as a line of the conversation; and,
    where `cut` is given, cut on what the frame shows of it after its line
    head: whole lines while that stays within `cut`, a first line longer than
    that cut inside, the rest named by how many of the message's own
    characters it held. Returns the text and the size of what it shows of
    the post, the measure `cut` bounds."""
    lines, ends = text.splitlines() or [""], text.splitlines(keepends=True) or [""]
    out, size, kept = [], 0, 0
    for i, line in enumerate(lines):
        head = f"{label}: " if i and line.strip() else ""
        # a further line is shown on a line of its own, at arrival_text's
        # indent of eight
        cost = len(line) if not i else len("\n" + " " * 8 + head + line)
        if cut is not None and size + cost > cut:
            if not i:
                out.append(line[:cut])
                size = kept = cut
            else:
                # the break after the last line kept is not shown either
                kept -= len(ends[i - 1]) - len(lines[i - 1])
            break
        out.append(head + line)
        size += cost
        kept += len(ends[i])
    left = len(text) - kept
    return "\n".join(out) + (CUT.format(n=f"{left:,}") if left > 0 else ""), size


def earlier(messages: list[dict], ts: str, place: str, named: dict[str, str], own: frozenset[str],
            now: datetime, *, kin=None, thread: int = 50, namesakes=(),
            turn: float | None = None) -> list[tuple[str, str, str]]:
    """The conversation before this message as a frame shows it (`shown`),
    oldest first, as (when, who, text). Her alert is shown as an alert posted
    in her name, every further line of it labelled so. A post marked outside
    the household is shown under its poster's marked name, every further line
    labelled with it, so that none of it reads as the household's, and cut at
    CUT_AT, all such posts together at OUTSIDE_CUT_AT, the newest kept first,
    but a thread's first message, cut at CUT_AT alone since it says what the
    thread is about and would otherwise be charged last;
    `namesakes` marks a poster with no Slack user as `names` marks anyone.
    Her own posts after the message are kept, in their place: a turn is
    framed under its conversation's lock, so they are what she said to the
    turns before, and a message sent while one of those ran would otherwise
    look unanswered."""
    kept = shown(messages, ts, place, own, now, kin=kin, thread=thread, turn=turn)
    head = kept[0] if place.endswith("thread") and kept and kept[0] is messages[0] else None
    out, room = [], OUTSIDE_CUT_AT
    # newest first, so that others' newest posts are the ones kept whole
    for m in reversed(kept):
        text = message_text(m.get("text"), m.get("files"), named)
        if is_mine(m, own):
            alert = (m.get("metadata") or {}).get("event_type") == ALERT_EVENT
            who, text = (ALERTED, _labelled(text, ALERTED)[0]) if alert else (ME, text)
        elif from_outside(m, own, kin):
            who = _poster(m, named, namesakes)
            if m is head:
                text = _labelled(text, who, cut=CUT_AT)[0]
            else:
                text, size = _labelled(text, who, cut=min(CUT_AT, room))
                room -= size
        else:
            who = named.get(m.get("user") or "", m.get("username") or "someone")
        out.append((stamp(_at(m), now), who, text))
    return out[::-1]


def _indent(text: str, n: int) -> str:
    # indented, no line of a message can end the part of the prompt it sits in:
    # the parser takes a blank line and an unindented one as the next part.
    # Broken at every boundary str.splitlines() knows, since a reader may take
    # any of them for the start of a line.
    return ("\n" + " " * n).join(text.strip().splitlines())


def arrival_text(place: str, speaker: str, text: str, readers: list[str],
                 earlier: list[tuple[str, str, str]], also: list[str] = (), *, outside: bool = False,
                 unlisted: bool = False, opening: tuple[str, ...] = ()) -> str:
    """The message as the session sees it. A direct message with nothing
    before it keeps the lab's frame; any other frame says who reads what is
    said there and shows what came before, each line with when it was sent.
    `also` is everyone else whose message this turn takes. The closing line
    names them too, so that `mem session` reads such an exchange back as no
    one person's, and nobody's request is taken for the speaker's. `outside`
    says, in a public channel or a thread in one, which anyone in this Slack
    can read, that some who can are outside the household; elsewhere the marks
    on its readers say so. `unlisted` says that Slack would not say who else
    is in the conversation. `opening` is the sentences that go after those on
    the opening line, LATE_TURN's, DOWN's and RETRIED's; a direct message
    with any takes the shape every other frame has, since the lab's has no
    line to hold them."""
    said = _indent(text, 4)
    after = f", after {' and '.join(also)}" if also else ""
    if place == "dm" and not earlier and not also and not opening:
        return f"{speaker} says to me, in a direct message:\n\n    {said}"
    room, heading = PLACES[place]
    who = ", ".join(readers or [speaker])
    if place.startswith("public"):
        head = (f"In {room} that anyone in this Slack can read; {who} and I are in it."
                + (OUTSIDERS_READ if outside else ""))
    else:
        head = f"In {room} that {who} and I read." + ("" if place == "dm" else EVERYONE)
    head += UNLISTED if unlisted else ""
    head += "".join(" " + sentence for sentence in opening)
    lines = "".join(f"    {when} {sp}: {_indent(tx, 8)}\n" for when, sp, tx in earlier if tx.strip())
    block, now = (f"{heading}\n\n{lines}\n", "now ") if lines else ("", "")
    return f"{head}\n\n{block}{speaker} {now}says{after}:\n\n    {said}"


def prompt(date: str, arrival: str) -> str:
    # the arrival goes in last, so nothing in someone's words is taken for a slot
    return PROMPT.replace("{date}", date).replace("{arrival}", arrival)


# A message added to the conversation while its session works, as that session
# is handed it: at its next step, or as a further turn once it has given an
# answer that has not been sent. The last of the session's answers that says
# something is posted, so the frame says so; `mem session` reads the frame back
# (ADDED_RE in memory/src/transcript.rs), the closing sentences ending the
# message.
ADDED = ("{speaker} adds this in the same {room} at {when}, before anything I say back has been sent:"
         "\n\n    {text}\n\n"
         "Nothing I have said back in this session has been sent yet. The last answer I give in this session "
         "that says something is the one sent, so that is where anything said here gets its answer.")


def added_text(place: str, speaker: str, text: str, when: str) -> str:
    room = PLACES[place][0].removeprefix("a ")
    return (ADDED.replace("{speaker}", speaker).replace("{room}", room).replace("{when}", when)
            .replace("{text}", _indent(text, 4)))


# A session no message started, whose answer reaches no one: the harness's own
# news for memory. `mem session` reads it back (NOBODY_RE in
# memory/src/transcript.rs) with no speaker, and shows what was said there as
# said to no one.
NOBODY = "No message started this session. What I say now reaches no one.\n\n    {text}"
# A member's name in Slack has changed; both names are ones everyone in the
# workspace sees. Its third and fourth sentences are the rule memory's answer
# is read by once the session ends (settle in wanda/household.py), and change
# with it.
RENAMED = ("The person I have known in this Slack as {old} is named {new} there now. It is the same person; "
           "only the name I take for them from this Slack has changed. When my memory is read after this "
           "session ends, their messages, and others' mentions of them, start to reach me under {new} if it "
           "finds exactly one person by the name {new}, named {new}, in any capitals, who is also found by "
           "the name {old}, or was made in this session while the name {old} finds no one; and, if this "
           "session ended without failing, also if it finds no one by either name. Otherwise they go on "
           "reaching me under {old}. What they said before now, and what I hold about them, may name them "
           "{old}. A note of mine that names them only in its words has no link to them: `mem recall` "
           "reaches it by neither name, and `mem search` finds it only by a word of three or more characters "
           "that it holds.")


def renamed_text(old: str, new: str) -> str:
    """What a names session is handed: the change, in the frame of a session
    whose answer reaches no one."""
    return NOBODY.format(text=_indent(RENAMED.format(old=old, new=new), 4))


def _texts(content) -> list[str]:
    """The words of a message a session was handed: a string, or text blocks
    alone, as a turn's input is; a tool's result is not one."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list) and content and all(
            isinstance(b, dict) and b.get("type") == "text" for b in content):
        return [b.get("text") or "" for b in content]
    return []


def _notice(entry: dict) -> bool:
    """A turn Claude Code began with a background command's notice of its
    end, not with a message."""
    texts = _texts((entry.get("message") or {}).get("content"))
    origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
    return origin.get("kind") == "task-notification" or bool(texts) and texts[0].lstrip().startswith(
        "<task-notification>")


def handed(vault: Path, sid: str) -> list[str] | None:
    """Every message a session was handed after its first, as its transcript
    records them: one taken in at a turn's next step (an attachment of type
    queued_command, in the mode of a message), one that began a further turn,
    and one Claude Code took into a turn with another (a later text block of
    the same message, the opening one included). Not a background command's
    notice of its end, which Claude Code hands over the same ways in a mode
    of its own, nor its own meta notes. None when the transcript cannot be
    read."""
    try:
        lines = (transcripts_dir(vault) / f"{sid}.jsonl").read_text(errors="replace").splitlines()
    except OSError:
        return None
    out, opened = [], False
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        a = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
        if e.get("type") == "attachment" and a.get("type") == "queued_command":
            if a.get("commandMode") == "prompt" and not a.get("isMeta"):
                out += _texts(a.get("prompt"))
        elif e.get("type") == "user" and not e.get("isMeta") and not _notice(e):
            texts = _texts((e.get("message") or {}).get("content"))
            out += texts if opened else texts[1:]
            opened = opened or bool(texts)
    return out


class Turn(NamedTuple):
    # begun by a message, not by a background command's notice of its end
    member: bool
    # the messages that began it, as `handed` gives them: the first turn's
    # without the prompt
    texts: list[str]


def turn_starts(vault: Path, sid: str) -> list[Turn] | None:
    """Each turn of a session, in order, as its transcript records the
    message or the notice that began it. Claude Code gives each turn a
    result, in the same order, so a turn past the session's last result
    failed. None when the transcript cannot be read."""
    try:
        lines = (transcripts_dir(vault) / f"{sid}.jsonl").read_text(errors="replace").splitlines()
    except OSError:
        return None
    out, opened = [], False
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("type") != "user" or e.get("isMeta"):
            continue
        if _notice(e):
            out.append(Turn(False, []))
        elif texts := _texts((e.get("message") or {}).get("content")):
            out.append(Turn(True, texts if opened else texts[1:]))
            opened = True
    return out


def date_paragraph(now: datetime) -> str:
    """The system prompt's date paragraph for every memory session, the
    clock's included. `now` is in the household's zone."""
    date = now.date().isoformat()
    return (TODAY.format(weekday=f"{now:%A}", date=date, time=f"{now:%H:%M}", zone=now.tzname())
            + AFTER_DATE.replace("{date}", date))


def transcripts_dir(vault: Path) -> Path:
    """Where Claude Code keeps the transcripts of sessions run in the vault. It
    turns every character that is not a letter or a digit into '-', and `mem`
    on its own turns only '/' (docs/issues.md, 23)."""
    key = re.sub(r"[^A-Za-z0-9]", "-", str(vault.resolve()))
    return Path.home() / ".claude" / "projects" / key


def refused_for_a_busy_vault(cfg: Config, since: datetime) -> int:
    """How many `mem` calls the sessions' transcripts show refused since
    `since` because the vault stayed busy (BUSY), each counted once, in the
    tool result of the command that made it: `mem` prints a refusal at the
    start of a line, and `mem session` shows an earlier one after an arrow,
    so a later look at a refused exchange is not counted again. A refusal in
    a command Claude Code moved to the background goes to that task's output
    file and is not counted."""
    n = 0
    for path in transcripts_dir(cfg.vault_dir).glob("*.jsonl"):
        try:
            if path.stat().st_mtime < since.timestamp():
                continue
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if BUSY not in line:
                continue
            try:
                entry = json.loads(line)
                if datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")) < since:
                    continue
                content = entry["message"]["content"]
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            for c in content if isinstance(content, list) else ():
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    out = c.get("content")
                    if isinstance(out, list):
                        out = "\n".join(b.get("text", "") for b in out if isinstance(b, dict))
                    n += len(REFUSED.findall(str(out)))
    return n


class LookBack(NamedTuple):
    # `mem session` calls over every transcript, as written in the commands
    calls: int
    # how long the commands that hold them took, each from its call to its
    # result in the transcript
    seconds: float


def looks_back(cfg: Config, sid: str) -> LookBack:
    """How many `mem session` calls with --last, --with or --day a session's
    Bash commands hold, each counted once as written, one in a loop too, and
    how long those commands took. Each such call reads every transcript
    kept, on the Mac's mount, which is what a session's time is weighed
    against."""
    n, took, began = 0, 0.0, {}
    try:
        lines = (transcripts_dir(cfg.vault_dir) / f"{sid}.jsonl").read_text(errors="replace").splitlines()
    except OSError:
        return LookBack(0, 0.0)
    for line in lines:
        if "mem session" not in line and not (began and "tool_result" in line):
            continue
        try:
            entry = json.loads(line)
            content = entry["message"]["content"]
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        try:
            # apart, so that a call on a line whose time will not read still counts
            at = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError, AttributeError):
            at = None
        for c in content if isinstance(content, list) else ():
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("name") == "Bash":
                command = str((c.get("input") or {}).get("command", ""))
                calls = sum(1 for call in re.split(r"&&|\|\||[;|\n]", command) if LOOK_BACK.search(call))
                n += calls
                if calls and at is not None:
                    # an id that is not a string pairs with nothing
                    with contextlib.suppress(TypeError):
                        began[c.get("id")] = at
            elif c.get("type") == "tool_result" and at is not None:
                try:
                    took += max(0.0, (at - began.pop(c.get("tool_use_id"))).total_seconds())
                except (KeyError, TypeError):
                    # not a look back's result, or a time with no zone beside
                    # one with a zone
                    continue
    return LookBack(n, took)


def session_env(cfg: Config, sid: str, now: datetime) -> dict[str, str]:
    """The session's whole environment. The daemon's own settings, the Slack
    tokens among them, are left out: a session answers in its report, and one
    that could reach Slack itself could answer twice."""
    date = now.date().isoformat()
    env = {k: v for k, v in os.environ.items() if not k.startswith("WANDA_")}
    env.update({
        # Claude Code otherwise has every session keep a memory of its own,
        # outside the vault
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "MEM_VAULT": str(cfg.vault_dir),
        "MEM_DATE": date,
        # mem rewrites the machine's date to MEM_DATE in what a session writes,
        # which is for a simulated date. Here MEM_DATE is the local date and the
        # machine's is UTC's, a day ahead every evening: stated equal, there is
        # nothing to rewrite, and an evening's mention of tomorrow stays tomorrow
        "MEM_REAL_DATE": date,
        # mem's own local date and the times `mem session` shows
        "MEM_UTC_OFFSET": str(int(now.utcoffset().total_seconds())),
        # the shell's `date` then agrees with the date paragraph
        "TZ": cfg.tz,
        "MEM_SESSION": sid,
        "MEM_TRANSCRIPTS": str(transcripts_dir(cfg.vault_dir)),
    })
    return env


# --- what comes back ---

def normalised(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())


def report(structured, result_text: str | None) -> dict | None:
    """What the session reported. Claude Code puts it in structured_output;
    the lab reads it from the result text, so both are read. A placeholder
    answer beside a placeholder in `recalled` or `recorded` is the schema
    filled with scaffolding, and no report; beside anything else it is her
    answer, since a member may ask her to say just "test"."""
    for candidate in (structured, result_text):
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except ValueError:
                continue
        if isinstance(candidate, dict) and isinstance(candidate.get("answer"), str):
            return None if _scaffolding(candidate) else candidate
    return None


def _scaffolding(out: dict) -> bool:
    def holds(field) -> bool:
        return any(isinstance(v, str) and normalised(v) in PLACEHOLDER
                   for v in (field if isinstance(field, list) else [field]))
    return holds(out["answer"]) and (holds(out.get("recalled")) or holds(out.get("recorded")))


def answer(out: dict) -> str:
    """The answer a report gives, rendered harmless, as it is posted: the
    answer the run store keeps is the one posted, at once or later."""
    return harmless(out["answer"].strip())


def answers(results: list[dict]) -> list[str]:
    """The answers a session's results carry, in order."""
    return [answer(o) for ev in results
            if (o := report(ev.get("structured_output"), ev.get("result"))) is not None]


def transcript_answers(vault: Path, sid: str) -> list[str]:
    """The answers a session gave, in order, as its transcript keeps each
    turn's structured output; none when it cannot be read."""
    try:
        lines = (transcripts_dir(vault) / f"{sid}.jsonl").read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        a = e.get("attachment") if isinstance(e, dict) and isinstance(e.get("attachment"), dict) else {}
        if a.get("type") == "structured_output" and (o := report(a.get("data"), None)) is not None:
            out.append(answer(o))
    return out


def last_said(given: list[str]) -> str:
    """The last of a session's answers that says something, or "": the
    product posts that one, since a turn after it may rightly say nothing."""
    return next((a for a in reversed(given) if a), "")


# --- the vault ---

# Claude Code prunes a transcript only once a setting it reads names the
# period, and sessions read the vault's settings alone. root.md tells sessions
# the transcripts are kept for a month.
SETTINGS = '{"cleanupPeriodDays": 30}\n'
# each directory `mem` keeps nodes in, and the kind of node it keeps there
# (memory/src/fm.rs, KIND_DIR)
KINDS = {"people": "person", "places": "place", "orgs": "org", "groups": "group", "things": "thing",
         "topics": "topic", "events": "event", "prefs": "preference", "trajectories": "trajectory"}
KIND_DIRS = tuple(KINDS)
NAMED = re.compile(r'^(?:name|summary): *"?[^"\s]', re.MULTILINE)
# How long the daemon waits for the vault's lock before giving up: a snapshot
# is skipped, a startup fails.
LOCK_WAIT_S = 60
# How `mem` says it gave up waiting for the vault (memory/src/bin/mem.rs), at
# the start of a line, as `mem` prints every refusal
BUSY = "the store could not be held for this call: it stayed busy"
REFUSED = re.compile(r"(?m)^\(" + re.escape(BUSY))
# a `mem session` call that reads every transcript kept, where one naming a
# session reads that one alone
LOOK_BACK = re.compile(r"\bmem\s+session\b.*\s--(?:last|with|day)(?:[\s=]|$)")
# The index older `mem` builds kept in the vault, an editor's settings, and
# the file a write fills before renaming it over a node.
SNAPSHOT_SKIP = (":!.index.db", ":!.obsidian", ":!.*.part", ":!**/.*.part")
# A snapshot past this is stopped, git and all; the housekeeping, which packs
# every loose object over the Mac's mount, gets longer.
SNAPSHOT_TIMEOUT_S = LOCK_WAIT_S + 60
HOUSEKEEPING_TIMEOUT_S = 600
# how long a stopped group's leader (flock, for a snapshot) is waited for
# after each signal, and then the whole group: bounded, since a git in a call
# on a stalled mount would otherwise hold up every later snapshot
STOP_GRACE_S = 5
# Two gits writing one repository at once fail on its index.lock, so the
# snapshots and the housekeeping take turns.
_ONE_AT_A_TIME = threading.Lock()
# the lock files git leaves in a repository when it is stopped part way
GIT_LOCKS = ("index.lock", "HEAD.lock", "packed-refs.lock", "config.lock")
# where `check` proves the vault takes a write, named as `mem`'s own
# half-written files are, so one left by a kill is never snapshotted
WRITE_PROBE = ".wanda-check.part"


def _run(argv: list[str], cwd: Path | None = None, env: dict[str, str] | None = None,
         timeout: int = 120):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def _run_group(argv: list[str], cwd: Path | None = None, env: dict[str, str] | None = None,
               timeout: float = 120) -> subprocess.CompletedProcess:
    """A command in a process group of its own, stopped whole if it outlives
    `timeout`: whatever it started stops with it, so nothing it ran keeps
    writing after the daemon has given up on it, but for a call on the Mac's
    mount still under way STOP_GRACE_S after the kill, which is logged.
    SIGTERM first, which ends flock, a snapshot's leader, at once; then
    SIGKILL, with up to STOP_GRACE_S for the leader after each signal and
    then for the whole group to be gone."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # macOS, where the tests run, refuses a signal to a group whose
        # members have all ended and are not yet reaped (EPERM): such a group
        # does nothing more, and counts as gone
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, sig)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(STOP_GRACE_S)
        # a git in the middle of a call on the Mac's mount ends only when the
        # call returns: until the whole group is gone, it is not stopped
        deadline = time.monotonic() + STOP_GRACE_S
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.02)
        else:
            log.warning("%s: still running %s s after it was killed", argv[0], STOP_GRACE_S)
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _git(argv: list[str], cwd: Path | None = None, *, then: str = "") -> None:
    """One of the setup's git commands, raising with git's own words when it
    fails, and `then`, what to look at about it, after them."""
    done = _run(["git", *argv], cwd=cwd)
    if done.returncode != 0:
        raise subprocess.SubprocessError(
            f"git {' '.join(argv)}: exit {done.returncode}: {done.stderr.strip()[:300]}{then}")


def _templates(mem: str) -> Path:
    # where `mem` itself reads them (memory/src/index.rs)
    beside = Path(mem).resolve().parent / "templates"
    return beside if beside.is_dir() else Path(os.environ.get("MEM_TEMPLATES", "templates"))


def _standing(vault: Path, root: str) -> bool:
    try:
        return (vault / "CLAUDE.md").read_text().startswith(root.rstrip() + "\n\n")
    except OSError:
        return False


@contextlib.contextmanager
def _locked(vault: Path, how: int):
    """The vault's lock, held as `mem` holds it: exclusively by anything else
    that writes the live vault, shared by anything else that reads it."""
    fd = os.open(vault, os.O_RDONLY)
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, how | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"the vault stayed locked for {LOCK_WAIT_S} s") from None
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


def _empty(vault: Path) -> bool:
    """No root CLAUDE.md and no node file. Directories do not count: a `mem`
    read on an empty vault leaves its kind directories behind, so one look by
    hand at a lost vault would otherwise pass it as a vault that was set up."""
    if (vault / "CLAUDE.md").exists():
        return False
    return not any(f.name != "CLAUDE.md" for kind in KIND_DIRS for f in (vault / kind).glob("*.md"))


def has_snapshots(cfg: Config) -> bool:
    """Whether snapshots.git is there, by opening its HEAD: the VM answers a
    look at a directory on the Mac's mount from a view up to 20 s old, and an
    open goes to the Mac. One there that cannot be read counts as there."""
    try:
        (cfg.snapshots_dir / "HEAD").read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def lost(cfg: Config, known_since: str | None) -> str | None:
    """What was had and is gone, or None. `known_since` is the day this run
    store first had a working vault. An empty vault was lost (in Docker, with
    its volume) when the run store or the snapshot repository says there was
    one, or when the snapshot repository cannot be read, and a missing
    snapshot repository (in Docker, with the Mac's directory) when the run
    store says there was a vault: starting an empty one would hide the loss.
    The run store and the snapshots are kept apart so that each can say what
    the other lost."""
    if _empty(cfg.vault_dir):
        had = [f"the run store has had a vault since {known_since}"] if known_since else []
        if has_snapshots(cfg):
            try:
                if (last := last_snapshot(cfg)) != "none":
                    had.append(f"snapshots.git holds it as of {last}")
            except OSError as e:
                had.append(f"snapshots.git, which may hold it, could not be read ({e})")
        if had:
            return (f"the vault at {cfg.vault_dir} is empty, though {' and '.join(had)}: restore it, "
                    "or start an empty one (README, State)")
    if known_since and not has_snapshots(cfg):
        return (f"snapshots.git missing from {cfg.expanded_data_dir}, though the run store has had a "
                f"vault since {known_since}: put it back, or go on without the snapshots before now "
                "(README, State)")
    return None


def prepare(cfg: Config, now: datetime, known_since: str | None = None) -> str | None:
    """Sets the vault up for sessions, then checks it. Returns what is wrong."""
    vault = cfg.vault_dir
    if problem := lost(cfg, known_since):
        return problem
    env = session_env(cfg, "", now)
    mem = shutil.which("mem", path=env.get("PATH"))
    if not mem:
        return "mem is not on PATH"
    templates = _templates(mem)
    try:
        vault.mkdir(parents=True, exist_ok=True)
        wanted = {f".claude/skills/{name}/SKILL.md": (templates / f"{name}.md").read_text()
                  for name in ("enrich", "retract")} | {".claude/settings.json": SETTINGS}
        # released before `mem` runs below, which takes the lock itself
        with _locked(vault, fcntl.LOCK_EX):
            if not (vault / ".git").exists():
                # Claude Code takes CLAUDE.md and its memory from the enclosing
                # repository, whatever the working directory
                _git(["init", "-q"], cwd=vault)
            # Claude Code puts this repository's status in every session's
            # context, where every lab session read "(clean)". Ignoring the
            # files instead would hide them from the Grep tool, whose search
            # honours git's ignore files. Set only when the repository does
            # not have it: a config.lock a stopped git left would otherwise
            # fail every start of a vault that needs no change.
            shown = _run(["git", "config", "--local", "--get", "status.showUntrackedFiles"], cwd=vault)
            if shown.stdout.strip() != "no":
                # every value replaced: git refuses a plain write to a key set
                # more than once, as a session's own `git config --add` can
                _git(["config", "--replace-all", "status.showUntrackedFiles", "no"], cwd=vault,
                     then=" (README, State)")
            for rel, text in wanted.items():
                path = vault / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists() or path.read_text() != text:
                    path.write_text(text)
        root = (templates / "root.md").read_text()
    except (OSError, subprocess.SubprocessError) as e:
        return f"setting up {vault}: {e}"
    if not has_snapshots(cfg):
        try:
            _git(["init", "-q", "--bare", str(cfg.snapshots_dir)])
        except (OSError, subprocess.SubprocessError) as e:
            return (f"making {cfg.snapshots_dir}: {e}; the container's view of a directory the Mac has just "
                    "moved or made can be 20 s old, and Docker starts wanda again by itself (README, State)")
    if not _standing(vault, root):
        # the root CLAUDE.md is generated by every write, from the templates
        # beside `mem`: a new vault has none, and after an upgrade it holds the
        # texts as they were. A write of her own node makes it current.
        try:
            done = _run([mem, "entity", "--kind", "person", "--name", "me"], cwd=vault, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            return f"mem entity: {e}"
        if done.returncode != 0 or "panicked" in done.stderr:
            return f"mem entity: exit {done.returncode}: {(done.stdout + done.stderr)[-300:]}"
    return check(cfg, now)[0]


def _front(text: str) -> dict[str, str] | None:
    """A node file's front matter, each field as written, or None for a file
    `mem` passes over without a word: no closed front matter, or nothing to
    call the node by."""
    end = text.find("\n---", 4)
    if not text.startswith("---\n") or end < 0 or not NAMED.search(text[4:end]):
        return None
    return {key: value for key, sep, value in (line.partition(": ") for line in text[4:end].split("\n")) if sep}


def _read(f: Path) -> str:
    try:
        return f.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def _decoded(b: bytes) -> str:
    try:
        return b.decode()
    except UnicodeDecodeError:
        return ""


def _nodes(vault: Path) -> dict[str, dict[str, str] | None]:
    """Every node file, by its path in the vault, with its front matter, or
    None where `mem` cannot read it."""
    return {str(f.relative_to(vault)): _front(_read(f))
            for kind in KIND_DIRS for f in sorted((vault / kind).glob("*.md")) if f.name != "CLAUDE.md"}


def broken_nodes(vault: Path) -> list[str]:
    """Node files `mem` passes over without a word. It does not see the node,
    and what links to it reads without it; a person it cannot see is minted
    again the next time someone names them."""
    return [path for path, front in _nodes(vault).items() if front is None]


def _names(front: dict[str, str]) -> set[str]:
    """Every name `mem` finds a node by, in lower case: its name, and its
    aliases, which `mem` writes from every name it has had."""
    raw = front.get("aliases", "")
    try:
        aliases = json.loads(raw or "[]")
    except ValueError:
        aliases = raw.strip("[]").split(",")  # as written before every value was quoted
    if not isinstance(aliases, list):
        aliases = [aliases]
    return {household.unq(str(n)).lower() for n in [front.get("name", ""), *aliases]} - {""}


def node_id(path: str) -> str:
    """`kind:id` for a node file's path in the vault."""
    kind, _, name = path.partition("/")
    return f"{KINDS[kind]}:{name.removesuffix('.md')}"


class Unreadable(NamedTuple):
    """A node file `mem` cannot read, and what became of it."""
    path: str  # in the vault
    commit: str = ""  # the newest snapshot holding a copy `mem` reads, if one does
    put_back: bool = False
    made_since: tuple[str, ...] = ()  # the nodes, by id, that keep it out
    error: str = ""  # what stopped the look, which is made again at each pass

    def why(self) -> tuple:
        """What became of it, a failure's words aside: they can change from
        one try to the next while the failure stays."""
        return self.put_back, tuple(self.made_since), bool(self.error)

    def left_out(self) -> str:
        """Why it is left out, as the alert and doctor say it."""
        if self.error:
            return f"left out for now: {self.error}"
        if self.made_since:
            return (f"left out: {', '.join(self.made_since)}, made since, "
                    f"{'carries' if len(self.made_since) == 1 else 'carry'} its name")
        return "left out: no snapshot holds a readable copy"

    def said(self) -> str:
        """As the `memory` alert says it."""
        if self.put_back:
            return (f"{self.path} could not be read; put back from snapshot {self.commit}, the damaged copy "
                    "kept in damaged/ in the data directory")
        return (f"{self.path} could not be read and is {self.left_out()}"
                + ("; doctor names both (README, State)" if self.made_since else ""))


# how long a read of snapshots.git may take before the file waits for the next try
SNAPSHOT_READ_S = 120


def _in_snapshots(cfg: Config, *argv: str) -> bytes | None:
    """What a read of snapshots.git printed, or None where it holds no such
    path or no commit yet. Raises SubprocessError when git does not answer:
    a repository that cannot say what it holds is not one that holds
    nothing."""
    try:
        # in the C locale, so git's words for a path or a commit it does not
        # have are the ones looked for below
        done = subprocess.run(["git", "--git-dir", str(cfg.snapshots_dir), *argv], capture_output=True,
                              env=os.environ | {"LC_ALL": "C"}, timeout=SNAPSHOT_READ_S)
    except subprocess.TimeoutExpired:
        raise subprocess.SubprocessError(f"git {argv[0]}: no answer in {SNAPSHOT_READ_S} s") from None
    except OSError as e:
        raise subprocess.SubprocessError(f"git {argv[0]}: {e}") from e
    if done.returncode == 0:
        return done.stdout
    said = done.stderr.decode(errors="replace").strip()
    if "does not exist in" in said or "does not have any commits yet" in said:
        return None
    raise subprocess.SubprocessError(f"git {argv[0]}: exit {done.returncode}: {said[:300]}")


def _readable_copy(cfg: Config, path: str) -> tuple[str, bytes] | None:
    """The newest snapshot holding a copy of `path` that `mem` reads, and the
    copy. The newest that holds the file at all can hold it damaged."""
    for commit in _decoded(_in_snapshots(cfg, "log", "--format=%h", "--", path) or b"").split():
        copy = _in_snapshots(cfg, "show", f"{commit}:{path}")
        if copy is not None and _front(_decoded(copy)) is not None:
            return commit, copy
    return None


def _made_since(cfg: Config, nodes: dict[str, dict[str, str] | None], path: str, commit: str,
                names: set[str]) -> list[str]:
    """The readable nodes, by id, that carry one of `names` and whose file
    did not carry it in snapshot `commit`: made or renamed since, while `mem`
    could not see the node at `path`. Once that node read again `mem` would
    refuse every command naming the name. One whose file carried it there was
    its namesake before the damage, and is no bar."""
    found = []
    for other, front in nodes.items():
        if front is None or other == path or not (shared := _names(front) & names):
            continue
        then = _in_snapshots(cfg, "show", f"{commit}:{other}")
        before = _front(_decoded(then)) if then is not None else None
        if before is None or shared - _names(before):
            found.append(node_id(other))
    return found


def _write_whole(f: Path, text: bytes) -> None:
    """As `mem` writes a node (memory/src/vault.rs, write_whole): to a hidden
    file beside it, which then takes its name, so the path holds the old file
    or the new one and never one cut short. The name does not end in `.md`,
    and SNAPSHOT_SKIP leaves it out, so one left by a death part way is
    neither read as a node nor snapshotted."""
    part = f.with_name(f".{f.name}.part")
    try:
        part.write_bytes(text)
        os.replace(part, f)
    except OSError:
        with contextlib.suppress(OSError):
            part.unlink()
        raise


def _put_back_one(cfg: Config, path: str, commit: str, copy: bytes) -> Unreadable | None:
    """The file at `path` written over with `copy`, the damaged file kept in
    the data directory first, unless a node made since carries its name.
    The file is read again, the names read and the copy written in one
    exclusive hold of the vault, so that no `mem` call mints the name
    between the look and the write. None when the file is no longer there
    to put back or reads again: `mem forget` removed it, or a write or the
    mount put it right."""
    f = cfg.vault_dir / path
    with _locked(cfg.vault_dir, fcntl.LOCK_EX):
        try:
            damaged = f.read_bytes()
        except FileNotFoundError:
            return None
        if _front(_decoded(damaged)) is not None:
            return None
        nodes = _nodes(cfg.vault_dir)
        if made := _made_since(cfg, nodes, path, commit, _names(_front(_decoded(copy)))):
            return Unreadable(path, commit, made_since=tuple(made))
        kept = cfg.expanded_data_dir / "damaged" / f"{path}.{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_bytes(damaged)
        _write_whole(f, copy)
    return Unreadable(path, commit, put_back=True)


def put_back(cfg: Config) -> list[Unreadable]:
    """Each node file `mem` cannot read, put back as the newest snapshot
    holding a copy it reads has it, or left out, with what became of it.
    A file left out stays as it is: `mem` does not see the node, and the
    start goes on (`check`). It takes its turn with the snapshots and the
    housekeeping, one git at a time in snapshots.git, as they do. Raises
    OSError when the vault cannot be read."""
    vault = cfg.vault_dir
    if not vault.is_dir():
        return []
    with _locked(vault, fcntl.LOCK_SH):
        broken = broken_nodes(vault)
    found = []
    if not broken:
        return found
    with _ONE_AT_A_TIME:
        for path in broken:
            try:
                if (copy := _readable_copy(cfg, path)) is None:
                    found.append(Unreadable(path))
                elif (outcome := _put_back_one(cfg, path, *copy)) is not None:
                    found.append(outcome)
            except subprocess.SubprocessError as e:
                found.append(Unreadable(path, error=f"snapshots.git did not answer ({e})"))
            except OSError as e:
                found.append(Unreadable(path, error=str(e)))
    return found


def last_snapshot(cfg: Config) -> str:
    """The newest snapshot, as its short hash and subject, or "none" while
    snapshots.git has no commit. Raises OSError when it cannot be read: a
    repository that cannot say what it holds is not one that holds nothing."""
    try:
        # in the C locale, so git's words for a repository with no commit are
        # the ones looked for below
        done = _run(["git", "--git-dir", str(cfg.snapshots_dir), "log", "-1", "--format=%h %s"],
                    env=os.environ | {"LC_ALL": "C"})
    except subprocess.SubprocessError as e:
        raise OSError(f"git log: {e}") from e
    if done.returncode == 0 and done.stdout.strip():
        return done.stdout.strip()
    if "does not have any commits yet" in done.stderr:
        return "none"
    raise OSError(f"git log: exit {done.returncode}: {done.stderr.strip()[:300]}")


def check(cfg: Config, now: datetime) -> tuple[str | None, list[str]]:
    """Whether a session would find its standing texts, a node `mem` can
    read, a vault that takes a write, and a `mem` that reads; and the node
    files `mem` cannot read, which `put_back` has put back or left out."""
    env = session_env(cfg, "", now)
    mem = shutil.which("mem", path=env.get("PATH"))
    if not mem:
        return "mem is not on PATH", []
    try:
        root = (_templates(mem) / "root.md").read_text()
    except OSError as e:
        return f"the templates beside mem: {e}", []
    try:
        # held shared, as the lock contract asks of anything that reads the
        # vault outside `mem`: the files are then read between `mem` commands
        with _locked(cfg.vault_dir, fcntl.LOCK_SH):
            standing = _standing(cfg.vault_dir, root)
            nodes = _nodes(cfg.vault_dir)
    except OSError as e:
        return f"reading {cfg.vault_dir}: {e}", []
    broken = [path for path, front in nodes.items() if front is None]
    read = [path for path, front in nodes.items() if front is not None]
    if not standing:
        return f"{cfg.vault_dir / 'CLAUDE.md'} does not carry the standing texts", broken
    if broken and not read:
        try:
            last = last_snapshot(cfg)
        except OSError as e:
            last = f"unknown ({e})"
        return (f"{len(broken)} node file(s) mem cannot read, {', '.join(broken[:5])}; the last "
                f"snapshot, {last}, holds the vault as it was (README, State)"), broken
    try:
        # held as anything other than `mem` that writes the live vault holds it
        with _locked(cfg.vault_dir, fcntl.LOCK_EX):
            (cfg.vault_dir / WRITE_PROBE).write_text("a write the vault takes\n")
            (cfg.vault_dir / WRITE_PROBE).unlink()
    except OSError as e:
        return f"writing to {cfg.vault_dir}: {e}", broken
    # Where no file of hers reads, `mem` finds no one by her names, and a node
    # it does read shows that it reads. Two of hers still fail the check, as
    # they would fail every session naming her.
    hers = [path for path in read if path.startswith("people/")
            and household.unq(nodes[path].get("name") or nodes[path].get("summary", "")).lower() in household.SELF]
    probe = "me" if hers or not read else node_id(read[0])
    try:
        done = _run([mem, "recall", probe], cwd=cfg.vault_dir, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return f"mem recall {probe}: {e}", broken
    if done.returncode != 0:
        return f"mem recall {probe}: exit {done.returncode}: {(done.stdout + done.stderr)[-300:]}", broken
    return None, broken


def _clear_git_locks(repo: Path) -> list[str]:
    """Lock files a git stopped part way left in `repo`. Called only while
    the snapshots take turns, when no git of the daemon's is running there
    unless a killed one was still in a call on the Mac's mount when
    `_run_group` gave up on it, which it logs: left in place, each would
    fail every later snapshot."""
    found = [repo / name for name in GIT_LOCKS] + sorted((repo / "refs").rglob("*.lock"))
    removed = []
    for f in found:
        try:
            f.unlink()
        except FileNotFoundError:
            continue
        except OSError as e:
            # git's own failure on it then says what is wrong
            log.warning("could not remove %s: %s", f, e)
            continue
        log.warning("removed %s, left by a git that was stopped part way", f)
        removed.append(str(f))
    return removed


def snapshot(cfg: Config, message: str) -> str | None:
    """The vault as it stands, committed to the snapshots repository when it
    has changed, so any write a session made can be undone by hand. Taken
    under the vault's lock, shared, so it never holds half of a `mem`
    command. flock alone holds the lock, for as long as git runs, and passes
    it to nothing git starts; git's own housekeeping is left to `housekeep`.
    Snapshots take turns, and one that outlives its time is stopped whole,
    git included. Returns what the household should be told, a failure or a
    lock file removed, and never stops a reply."""
    env = os.environ | {
        "GIT_DIR": str(cfg.snapshots_dir), "GIT_WORK_TREE": str(cfg.vault_dir),
        "GIT_AUTHOR_NAME": "wanda", "GIT_AUTHOR_EMAIL": "wanda@localhost",
        "GIT_COMMITTER_NAME": "wanda", "GIT_COMMITTER_EMAIL": "wanda@localhost",
    }
    # a commit is what starts git's housekeeping, which would hold the
    # snapshots repository past the snapshot
    script = ('m=$1; shift; git add -A -- . "$@" && '
              '{ git diff --cached --quiet || git -c gc.auto=0 commit -q -m "$m"; }')
    said = []
    with _ONE_AT_A_TIME:
        if removed := _clear_git_locks(cfg.snapshots_dir):
            said.append(f"removed {', '.join(removed)}, left by a git that was stopped part way")
        try:
            done = _run_group(["flock", "-s", "-o", "-w", str(LOCK_WAIT_S), "-E", "75", str(cfg.vault_dir),
                               "sh", "-c", script, "snapshot", message, *SNAPSHOT_SKIP],
                              cwd=cfg.vault_dir, env=env, timeout=SNAPSHOT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            said.append(f"failed: it ran past {SNAPSHOT_TIMEOUT_S} s and was stopped")
        except (OSError, subprocess.SubprocessError) as e:
            said.append(f"failed: {e}")
        else:
            if done.returncode == 75:
                said.append(f"failed: the vault stayed locked for {LOCK_WAIT_S} s")
            elif done.returncode != 0:
                # the start of git's message, which names the file it stopped on
                said.append(f"failed: exit {done.returncode}: {(done.stderr + done.stdout).strip()[:300]}")
    return f"snapshot {message!r}: {'; '.join(said)}" if said else None


def housekeep(cfg: Config) -> str | None:
    """git's housekeeping of the snapshots repository, which snapshots do
    not start: it packs loose objects once there are enough of them (git's
    gc.auto). It takes its turn with the snapshots, and holds nothing of the
    vault, which it never reads. Returns what went wrong."""
    with _ONE_AT_A_TIME:
        try:
            # in the foreground, where its time limit and its stop reach it
            done = _run_group(["git", "-c", "gc.autoDetach=false", "--git-dir", str(cfg.snapshots_dir),
                               "gc", "--auto", "--quiet"], timeout=HOUSEKEEPING_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return f"housekeeping of snapshots.git ran past {HOUSEKEEPING_TIMEOUT_S} s and was stopped"
        except (OSError, subprocess.SubprocessError) as e:
            return f"housekeeping of snapshots.git: {e}"
    if done.returncode != 0:
        return f"housekeeping of snapshots.git: exit {done.returncode}: {(done.stderr + done.stdout).strip()[:300]}"
    return None
