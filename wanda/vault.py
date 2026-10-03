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
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wanda import household
from wanda.config import Config
from wanda.transcript import is_mine, plain

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
GUEST = " (a guest in this Slack)"
# after someone outside the household whose Slack name is one a member goes
# or went by, so their words are not read as that person's
NAMESAKE = " (another person in this Slack)"
ME = "me"
# Past this many, a frame names the household's readers and counts the rest.
NAMED_READERS = 12
# Outside a thread, the conversation so far is its last RECENT_HOURS, at most
# EARLIER messages: a reply sent after midnight still arrives with what it
# answers. What came before is in the vault, and in the transcripts `mem
# session` reads.
RECENT_HOURS = 12
EARLIER = 20
# The marks the harness posts its alerts and its failure notes with (Slack
# message metadata). An alert is for the people who keep wanda running, and a
# failure note carries Claude Code's reason, in its words, not hers: both are
# left out of what a session is shown, where they would read as something she
# said. A session then sees the message a failed run left unanswered as it
# would after silence.
ALERT_EVENT = "wanda_alert"
NOTE_EVENT = "wanda_note"

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

def outsiders(ids: list[str], own: frozenset[str], members: list[str]) -> list[str]:
    """Who in a conversation is neither one of the household's let-in
    members nor her. A memory session runs only where this is no one:
    another person's words would reach a session that holds the household's
    whole memory and has a shell."""
    return sorted(set(ids) - own - set(members))


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


def names(users: dict[str, dict], told: dict[str, str], namesakes: set[str]) -> dict[str, str]:
    """Slack id to name: the name sessions are told for a member of the
    household (`told`), the one Slack shows for anyone else, marked when
    `mem` would read it as one of `namesakes`, lower case, which a member
    goes or went by."""
    out = {}
    for uid, u in users.items():
        prof = u.get("profile") or {}
        name = prof.get("display_name") or prof.get("real_name") or u.get("name") or uid
        out[uid] = name + (NAMESAKE if household.spelled(name).lower() in namesakes else "")
    return out | told


def readers(ids: list[str], users: dict[str, dict], named: dict[str, str],
            own: frozenset[str], told: dict[str, str]) -> list[str]:
    """Who can read what is said in a conversation. A guest is marked as one,
    unless the household names them (`told`); bots, deactivated accounts and
    her own ids read nothing. A reader Slack would not describe fails the
    frame, as an unreadable member list does."""
    out = []
    for uid in ids:
        if uid in own:
            continue
        if uid not in told and uid not in users:
            raise LookupError(f"no Slack record for {uid}")
        u = users.get(uid) or {}
        if u.get("is_bot") or u.get("deleted"):
            continue
        guest = uid not in told and (u.get("is_restricted") or u.get("is_ultra_restricted"))
        out.append(named.get(uid, uid) + (GUEST if guest else ""))
    out.sort()
    if len(out) > NAMED_READERS:
        known = [n for n in out if n in told.values()]
        out = known + [f"{len(out) - len(known)} others"]
    return out


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


def earlier(messages: list[dict], ts: str, place: str, named: dict[str, str],
            own: frozenset[str], now: datetime) -> list[tuple[str, str, str]]:
    """The conversation before this message, oldest first, as (when, who,
    text): a thread whole, anywhere else its recent part, and in either
    without the harness's alerts and failure notes. Her own posts after the
    message are kept, in their place: a turn is framed under its
    conversation's lock, so they are what she said to the turns before, and
    a message sent while one of those ran would otherwise look unanswered."""
    in_thread = place.endswith("thread")
    since = now.timestamp() - RECENT_HOURS * 3600
    out = []
    for m in messages:
        try:
            at = float(m.get("ts") or 0)
        except ValueError:
            continue
        if m.get("subtype") in ("channel_join", "channel_leave"):
            continue
        # whoever posted it: her own ids can be unknown when a frame is built,
        # only an app can attach metadata, and the household guard keeps every
        # other app out of these conversations
        if (m.get("metadata") or {}).get("event_type") in (ALERT_EVENT, NOTE_EVENT):
            continue
        mine = is_mine(m, own)
        if (at >= float(ts) and not mine) or (not in_thread and at < since):
            continue
        who = ME if mine else named.get(m.get("user") or "", m.get("username") or "someone")
        out.append((stamp(at, now), who, message_text(m.get("text"), m.get("files"), named)))
    return out if in_thread else out[-EARLIER:]


def _indent(text: str, n: int) -> str:
    # indented, no line of a message can end the part of the prompt it sits in:
    # the parser takes a blank line and an unindented one as the next part
    return text.strip().replace("\n", "\n" + " " * n)


def arrival_text(place: str, speaker: str, text: str, readers: list[str],
                 earlier: list[tuple[str, str, str]], also: list[str] = ()) -> str:
    """The message as the session sees it. A direct message with nothing
    before it keeps the lab's frame; any other frame says who reads what is
    said there and shows what came before, each line with when it was sent.
    `also` is everyone else whose message this turn takes. The closing line
    names them too, so that `mem session` reads such an exchange back as no
    one person's, and nobody's request is taken for the speaker's."""
    said = _indent(text, 4)
    after = f", after {' and '.join(also)}" if also else ""
    if place == "dm" and not earlier and not also:
        return f"{speaker} says to me, in a direct message:\n\n    {said}"
    room, heading = PLACES[place]
    who = ", ".join(readers or [speaker])
    if place.startswith("public"):
        opening = f"In {room} that anyone in this Slack can read; {who} and I are in it."
    else:
        opening = f"In {room} that {who} and I read." + ("" if place == "dm" else EVERYONE)
    lines = "".join(f"    {when} {sp}: {_indent(tx, 8)}\n" for when, sp, tx in earlier if tx.strip())
    block, now = (f"{heading}\n\n{lines}\n", "now ") if lines else ("", "")
    return f"{opening}\n\n{block}{speaker} {now}says{after}:\n\n    {said}"


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
    the lab reads it from the result text, so both are read."""
    for candidate in (structured, result_text):
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except ValueError:
                continue
        if isinstance(candidate, dict) and isinstance(candidate.get("answer"), str):
            return candidate
    return None


def answer(out: dict) -> str:
    text = out["answer"].strip()
    if text and normalised(text) in PLACEHOLDER:
        log.warning("dropping a placeholder answer: %r", text)
        return ""
    return text


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
# where `mem` keeps each kind of node (memory/src/fm.rs, KIND_DIR)
KIND_DIRS = ("people", "places", "orgs", "groups", "things", "topics", "events", "prefs", "trajectories")
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
    return check(cfg, now)


def broken_nodes(vault: Path) -> list[str]:
    """Node files `mem` passes over without a word: no closed front matter,
    or nothing to call the node by. A person `mem` cannot see is minted again
    the next time someone names them."""
    out = []
    for kind in KIND_DIRS:
        for f in sorted((vault / kind).glob("*.md")):
            if f.name == "CLAUDE.md":
                continue
            try:
                text = f.read_text()
            except (OSError, UnicodeDecodeError):
                text = ""
            end = text.find("\n---", 4)
            if not text.startswith("---\n") or end < 0 or not NAMED.search(text[4:end]):
                out.append(str(f.relative_to(vault)))
    return out


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


def check(cfg: Config, now: datetime) -> str | None:
    """Whether a session would find its standing texts, nodes `mem` can read,
    a vault that takes a write, and a `mem` that reads."""
    env = session_env(cfg, "", now)
    mem = shutil.which("mem", path=env.get("PATH"))
    if not mem:
        return "mem is not on PATH"
    try:
        root = (_templates(mem) / "root.md").read_text()
    except OSError as e:
        return f"the templates beside mem: {e}"
    try:
        # held shared, as the lock contract asks of anything that reads the
        # vault outside `mem`: the files are then read between `mem` commands
        with _locked(cfg.vault_dir, fcntl.LOCK_SH):
            standing = _standing(cfg.vault_dir, root)
            broken = broken_nodes(cfg.vault_dir)
    except OSError as e:
        return f"reading {cfg.vault_dir}: {e}"
    if not standing:
        return f"{cfg.vault_dir / 'CLAUDE.md'} does not carry the standing texts"
    if broken:
        try:
            last = last_snapshot(cfg)
        except OSError as e:
            last = f"unknown ({e})"
        return (f"{len(broken)} node file(s) mem cannot read, {', '.join(broken[:5])}; the last "
                f"snapshot, {last}, holds the vault as it was (README, State)")
    try:
        # held as anything other than `mem` that writes the live vault holds it
        with _locked(cfg.vault_dir, fcntl.LOCK_EX):
            (cfg.vault_dir / WRITE_PROBE).write_text("a write the vault takes\n")
            (cfg.vault_dir / WRITE_PROBE).unlink()
    except OSError as e:
        return f"writing to {cfg.vault_dir}: {e}"
    try:
        done = _run([mem, "recall", "me"], cwd=cfg.vault_dir, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return f"mem recall me: {e}"
    if done.returncode != 0:
        return f"mem recall me: exit {done.returncode}: {(done.stdout + done.stderr)[-300:]}"
    return None


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
