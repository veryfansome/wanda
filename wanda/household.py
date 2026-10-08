"""The name each household member goes by, and the names they went by.

Slack holds names and changes them; the allowlist holds who is let in; and
transcripts, reminders and memory keep a name after Slack has moved on. So
every name sessions have been told for a member is kept in the run store,
one meta row per member id (`names:<id>`), and a member's change of name in
Slack waits there, shown by doctor, while sessions go on being told the name
memory knows them by, until a session of its own has told memory of the
change and memory's answer gives the new one (`found`, `settle`).

A session is told the newest name in a row's `told`. Slack's name joins it
at a first sight, at a change of capitals, which `mem` reads as the same
name, and when memory has taken the new one. Each name is checked against
what `mem` and the transcript parser would make of it before it is used,
and one that is or was another member's is never used, so every name a
reminder or a transcript gives leads to one member."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

# How often every allowed id's name is read from Slack. A change waits for
# two reads at least half of this apart, so a name seen once in the middle of
# a profile edit, or by two reads seconds apart at a restart, is not handed on.
NAMES_EVERY_S = 600
# where memory/src/transcript.rs reads a speaker as more than one person
SEVERAL = (" (after ", " (then ")
# where the speaker of THREAD_RE (memory/src/transcript.rs) stops, in a frame
# with no earlier lines
NOW = " now"
# Slack's answers that show no one: an id is never let in on one of them alone
SHUT = ("user_not_found", "user_not_visible", "bot", "deleted")
# her name, by which `mem` finds her own node (SELF_NAME, memory/src/lib.rs):
# a mention of her reads as it in every frame, whatever her Slack app is called
NAME = "wanda"
# the labels `mem` resolves to her own node (memory/src/lib.rs)
SELF = ("me", NAME)
# the longest label `mem entity` takes (SUMMARY_MAX in memory/src/lib.rs)
LABEL_MAX = 140
# a label `mem entity` refuses for looking like an id: a hash id, or a kind
# and a local id (id_shaped in memory/src/text.rs)
KINDS = ("person", "place", "org", "group", "thing", "topic", "event", "preference", "trajectory")
HASH_ID = re.compile(r"(?:\d{4}-\d{2}-\d{2}-)?([0-9a-f]{6})")
LOCAL_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
# how `mem` reads a name a rename struck (former_names in memory/src/fm.rs)
WAS_NAMED = re.compile(r"~~was named: (.*?)~~")
# what the run budget answers a session it refuses (BUDGET_REPLIES in
# wanda/main.py): a try refused so is not a failure
REFUSALS = ("busy", "breaker")
# a try's wait after it fails: an hour, doubled each time, up to a day
FIRST_WAIT = timedelta(hours=1)
LONGEST_WAIT = timedelta(days=1)
# What `mem show "person:<name>"` prints (cmd_show and miss_text in
# memory/src/bin/mem.rs). One node is its id on a line of its own, then its
# file; several are an ambiguity, each candidate `<kind>:<id> (<summary or
# label>)`, joined by "; ". A summary is printed as written, so it may hold
# "; person:… (" or a bracket of its own: a candidate is read only where it
# opens the list, after "node: ", or follows the one before, after "); ".
NODE = re.compile(r"[a-z]+:\S+")
AMBIGUOUS = " is more than one node: "
CANDIDATE = re.compile(r"(?:node: |\); )([a-z]+):([0-9a-f]{6}) \(")
# every way it says no person is so named: a mistyped id and the kind alone
# included, both of which a name's first word can read as
NO_PERSON = ("(no node for ", "(no person ", "('person' is a kind, with no id or name after it")


def spelled(s: str | None) -> str:
    """A name as `mem` keeps a label: its whitespace collapsed and trimmed
    (one_line in memory/src/text.rs). Nothing else is folded, since a name
    `mem` keeps apart from another must stay apart here too."""
    return " ".join((s or "").split())


def same(a: str, b: str) -> bool:
    """Whether two names are one to `mem`, which compares labels in any case."""
    return spelled(a).lower() == spelled(b).lower()


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _wait(count: int) -> timedelta:
    return min(FIRST_WAIT * 2 ** max(0, count - 1), LONGEST_WAIT)


def _uncount(tried: dict) -> None:
    # a try is counted while it reads "did not end"; one that ends as no
    # failure is taken back once, however often it is read again
    if tried["error"] == "did not end":
        tried["count"] = max(0, tried["count"] - 1)


def tries(n: int) -> str:
    return f"{n} try" if n == 1 else f"{n} tries"


def _no_member(uid: str) -> str:
    """Why an id Slack has no member for is not let in. The allowlist takes
    member ids, and a direct message's id, which starts with D, is the one
    most often pasted in place of one."""
    said = f"WANDA_SLACK_OWNER_USER_IDS lists {uid}, which Slack has no member for"
    return said + (": a member id starts with U or W; a D… id is a direct message" if uid.startswith("D") else "")


def _id_shaped(name: str) -> bool:
    words = name.split()
    kind, _, local = (words[0].lower() if words else "").rpartition(":")
    m = HASH_ID.fullmatch(local)
    return bool(m and any(c.isdigit() for c in m[1])) or (kind in KINDS and bool(LOCAL_ID.fullmatch(local)))


def flaw(name: str) -> str | None:
    """Why `name` would not read back as itself, from `mem` or from the
    transcript parser, as a phrase to follow the name; None when it would."""
    if name.lower() in SELF:
        return "is the name memory has for wanda's own node"
    if any(s in name for s in SEVERAL):
        return "reads as more than one speaker in a transcript"
    if name.endswith(NOW):
        return "loses its last word in a frame with no earlier lines"
    if len(name) > LABEL_MAX or _id_shaped(name):
        return "is refused by mem as a label"
    if set(name) == {"*"}:
        # `mem show "person:**"` reads the kind alone, even once such a person exists
        return "is read by mem as a kind with no name"
    # a rename writes `~~was named: <name>~~` as it is, and reads it lazily
    if [n for c in WAS_NAMED.findall(f"~~was named: {name}~~") if (n := spelled(c))] != [name]:
        return "is misread by mem once a rename strikes it"
    return None


# --- what memory says about a name ---

@dataclass(frozen=True)
class Found:
    """What `mem show "person:<name>"` found among person nodes."""
    ids: tuple[str, ...] = ()  # the people it found: none, one or several
    label: str = ""  # for one: what it is called, read as fm::label reads it
    made: str = ""  # for one: the session that made it, "" for one made by hand
    said: str = ""  # what it found, in mem's words, for doctor


def unq(v: str) -> str:
    """A front matter value as fm::unq reads it: a double-quoted one exactly,
    an unquoted one as it is."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    return v


def found(code: int, out: str) -> Found:
    """What `mem show "person:<name>"` answered, with its exit code. `person:`
    looks among person nodes by label and by a name a rename struck, and with
    no person so named shows a node of another kind, or lists several, so
    only a person counts. Raises on any other answer: a busy vault, which
    wrote and read nothing, or a shape not known here."""
    text = out.strip()
    first, _, rest = text.partition("\n")
    if code == 0 and NODE.fullmatch(first):
        if not first.startswith("person:"):
            return Found(said=f"no person, but {first}")
        meta = {}
        if rest.startswith("---\n") and "\n---" in rest[4:]:
            for line in rest[4:4 + rest[4:].index("\n---")].split("\n"):
                key, sep, value = line.partition(": ")
                if sep:
                    meta[key] = unq(value)
        label = spelled(meta.get("name") or meta.get("summary"))
        return Found((first,), label, meta.get("made", ""), f"{first} ({label})")
    if code == 1 and AMBIGUOUS in first:
        ids = tuple(f"{kind}:{local}" for kind, local in CANDIDATE.findall(text) if kind == "person")
        listed = first.partition(AMBIGUOUS)[2].removesuffix(". An id says which.)")
        return Found(ids, said=listed if ids else f"no person, but {listed}")
    if code == 1 and first.startswith(NO_PERSON):
        return Found(said="no one")
    raise ValueError(f"mem show answered (exit {code}): {text[:300]}")


def settle(old: str, new: str, by_old: Found, by_new: Found, sid: str, ran_ok: bool) -> str:
    """What memory's answer means for a change from `old` to `new` handed to
    it by session `sid`: "advance", "keep" or "keep, alerted". It advances
    when the new name finds exactly one person, named it in any capitals,
    who is also found by the old name, which finds every person named or
    once named so, or was made by that session while the old name finds no
    one; and, after a run recorded ok (`ran_ok`), when neither name finds
    anyone. Otherwise memory kept the old name: by its own choice when the
    new name finds no one, or only people the old one finds too; and
    alerted when it finds someone the old one does not, as a second person,
    an ambiguity a rename made or a namesake already there, which no
    session can undo, since a struck name keeps answering."""
    if len(by_new.ids) == 1 and same(by_new.label, new) and (
            by_new.ids[0] in by_old.ids or (not by_old.ids and sid and by_new.made == sid)):
        return "advance"
    if ran_ok and not by_new.ids and not by_old.ids:
        return "advance"
    if set(by_new.ids) <= set(by_old.ids):
        return "keep"
    return "keep, alerted"


def memory_said(old: str, new: str, by_old: Found, by_new: Found) -> str:
    """Whom memory finds by each name, for doctor's line on a keep."""
    return f"person:{new} finds {by_new.said}; person:{old} finds {by_old.said}"


def _blank() -> dict:
    return {"told": [],
            "slack": {"name": None, "field": "", "why": "", "display": "", "full": "", "read": "", "seen": "",
                      "unread": {"at": "", "error": ""}},
            "out": None, "out_day": "", "tried": None, "kept": None}


class Household:
    """Every member's row, as the run store keeps it. The daemon holds one,
    loaded at its start and changed only between awaits, each change saved
    at once by whoever made it (`save`), so a read in flight cannot undo
    another's. Rows of ids taken off the allowlist stay: a name once
    someone's in the household is not given to anyone else."""

    def __init__(self, rows: dict[str, dict], allowed: list[str]):
        self.rows = rows
        self.allowed = list(allowed)

    @classmethod
    def load(cls, store, allowed: list[str]) -> Household:
        """Writes nothing: doctor and a script read the store with it."""
        return cls({key.removeprefix("names:"): json.loads(value)
                    for key, value in store.meta_starting("names:").items()}, allowed)

    def save(self, store, uid: str) -> None:
        store.set_meta(f"names:{uid}", json.dumps(self.rows[uid]))

    def _row(self, uid: str) -> dict:
        return self.rows.setdefault(uid, _blank())

    # --- who is let in, and by what name ---

    def told(self, uid: str) -> str | None:
        """The name sessions are told for this id, or None while it has none."""
        told = (self.rows.get(uid) or {}).get("told") or []
        return told[-1]["name"] if told else None

    def told_names(self) -> dict[str, str]:
        """Who is let in: each allowed id with a name sessions are told, in
        the allowlist's order, whatever Slack answers about it now."""
        return {uid: name for uid in self.allowed if (name := self.told(uid))}

    def askers(self) -> dict[str, str]:
        """Every name a let-in id has been told by, lower case, to that id:
        a reminder or a transcript can give any of them."""
        return {t["name"].lower(): uid for uid in self.told_names() for t in self.rows[uid]["told"]}

    def namesakes(self) -> set[str]:
        """The names, lower case, that mark anyone else who goes by them as
        another person: every name a member has been told by, and a change
        memory kept the earlier name for while their Slack still shows it."""
        out = set(self.askers())
        for uid in self.told_names():
            kept, shown = self.rows[uid]["kept"], self.rows[uid]["slack"]["name"]
            if kept and shown and same(shown, kept["name"]):
                out.add(shown.lower())
        return out

    def _holder(self, uid: str, name: str) -> str | None:
        """Another id that has `name`: one told it, now or before, allowed
        or not; or an allowed one whose Slack shows it as a change not yet
        taken, or whose try is for it."""
        for other, row in self.rows.items():
            if other != uid and any(same(t["name"], name) for t in row["told"]):
                return other
        for other in self.allowed:
            row = self.rows.get(other)
            if other == uid or row is None:
                continue
            shown, told = row["slack"]["name"], self.told(other)
            if shown and told and not same(shown, told) and same(shown, name):
                return other
            if row["tried"] and same(row["tried"]["name"], name):
                return other
        return None

    def refusal(self, uid: str, name: str) -> str | None:
        """Why `name` cannot be this id's, said after it, or None."""
        if no := flaw(name):
            return no
        if holder := self._holder(uid, name):
            return f"is {holder}'s"
        return None

    def usable(self, uid: str, user: dict) -> tuple[str | None, str, str]:
        """The name Slack gives this id that sessions can be told: its display
        name, the one it chose and the one Slack puts in mentions, or its full
        name when the display name cannot be used. Returns the name, the
        field it came from, and why each field before it was passed over;
        with no name, why neither can be used."""
        prof = user.get("profile") or {}
        why = []
        for field, raw in (("display name", prof.get("display_name")),
                           ("full name", prof.get("real_name") or user.get("real_name"))):
            name = spelled(raw)
            if not name:
                why.append(f"{field} empty")
            elif no := self.refusal(uid, name):
                why.append(f"{field} {name} {no}")
            else:
                return name, field, "; ".join(why)
        return None, "", "; ".join(why)

    # --- what Slack says ---

    def observe(self, uid: str, user: dict, now: datetime) -> str:
        """One read of this id that Slack answered with its record. Returns
        what changed, for the log, or ""."""
        row = self._row(uid)
        if user.get("is_bot") or user.get("deleted"):
            return self._shut(uid, "bot" if user.get("is_bot") else "deleted", now)
        told = self.told(uid)
        name, field, why = self.usable(uid, user)
        s, at = row["slack"], _iso(now)
        before = (s["name"], s["why"])
        if name != s["name"]:
            s["seen"] = at
        prof = user.get("profile") or {}
        s.update(name=name, field=field, why=why, display=prof.get("display_name") or "",
                 full=prof.get("real_name") or user.get("real_name") or "", read=at)
        # a record that shows a person ends what an answer showing no one said
        if row["out"] and row["out"]["why"] in SHUT:
            row["out"] = None
        if told is None:
            if name is None:
                if row["out"]:
                    return ""
                row["out"] = {"why": "no usable name", "since": at, "alerted": False}
                return f"names: {uid} is not let in: no name Slack gives can be used ({why})"
            row["out"] = None
            row["told"].append({"name": name, "since": at, "session": ""})
            return f"names: {uid} is let in as {name} ({field})"
        said = ""
        if name is None:
            if before != (None, why):
                said = f"names: {uid} has no name in Slack sessions can use ({why}); sessions say {told}"
        elif same(name, told) and name != told:
            # `mem` finds the one node by either spelling
            row["told"][-1] = {"name": name, "since": at, "session": ""}
            said = f"names: {uid} is {name} to sessions from now on, the same name in other capitals"
        elif name != before[0] and self.awaiting(uid):
            said = f"names: {uid} is {name} in Slack now; sessions say {told} until memory has been told"
        kept = row["kept"]
        # once Slack has shown the name sessions are told in two reads apart,
        # any keep ends, so a later change to the kept name is handed again
        if (kept and name and same(name, self.told(uid))
                and (_at(s["read"]) - _at(s["seen"])).total_seconds() >= NAMES_EVERY_S / 2):
            row["kept"] = None
            said = f"names: {uid} is {name} in Slack again; the keep of {kept['name']} ends"
        return said

    def unread(self, uid: str, error: str, now: datetime) -> str:
        """A read of this id that failed. Slack's answer that shows no one
        stands; any other failure leaves the name as it was, and is said for
        the log once a UTC day."""
        if error in SHUT:
            return self._shut(uid, error, now)
        s = self._row(uid)["slack"]
        logged = s["unread"]["at"][:10] == _iso(now)[:10]
        s["unread"] = {"at": _iso(now), "error": error}
        if logged:
            return ""
        told = self.told(uid)
        return (f"names: could not read {uid} from Slack: {error}; "
                + (f"sessions say {told}" if told else "not let in until a read gives a name"))

    def _shut(self, uid: str, why: str, now: datetime) -> str:
        row = self._row(uid)
        if row["out"] and row["out"]["why"] == why:
            return ""
        row["out"] = {"why": why, "since": _iso(now), "alerted": False}
        if told := self.told(uid):
            return f"names: Slack no longer shows {uid} ({why}); sessions say {told}, as before"
        if why == "user_not_found":
            return f"names: {uid} is not let in: {_no_member(uid)}"
        return f"names: {uid} is not let in: Slack does not show them ({why})"

    # --- a change of name, and memory ---

    def awaiting(self, uid: str) -> str | None:
        """Slack's name for a let-in id when it is neither the name sessions
        are told nor a change memory kept the earlier name for."""
        row, told = self.rows.get(uid), self.told(uid)
        if row is None or told is None:
            return None
        shown, kept = row["slack"]["name"], row["kept"]
        if not shown or same(shown, told) or (kept and same(shown, kept["name"])):
            return None
        return shown

    def awaits_memory(self, store, tried: dict) -> bool:
        """Whether a try's own run was recorded and memory has not yet been
        read for its outcome: an ok run, a run while the try still reads "did
        not end", or a stop's cancelled run while it reads "stopped mid-run",
        after which the model may have written."""
        run = store.session_run(tried["session"]) if tried["session"] else None
        return run is not None and (run["status"] == "ok" or tried["error"] == "did not end"
                                    or (run["status"] == "cancelled" and tried["error"] == "stopped mid-run"))

    def due(self, store, now: datetime) -> tuple[str, str, str] | None:
        """The first let-in id, in the allowlist's order, whose change is
        ready to be handed to memory, as (id, the name sessions are told, the
        new one). Writes nothing: a name held here is found by the id's next
        read, which counts the same names."""
        for uid, told in self.told_names().items():
            new = self.awaiting(uid)
            if new is None:
                continue
            s, tried = self.rows[uid]["slack"], self.rows[uid]["tried"]
            if (_at(s["read"]) - _at(s["seen"])).total_seconds() < NAMES_EVERY_S / 2:
                continue
            if self.refusal(uid, new):
                continue
            if tried and (self.awaits_memory(store, tried)
                          or (same(tried["name"], new) and now < _at(tried["next"]))):
                continue
            return uid, told, new
        return None

    def trying(self, uid: str, name: str, sid: str, now: datetime) -> None:
        """A try, written before its session runs: counted and backed off
        now, so one that never ends is not run again at every start."""
        tried = self._row(uid)["tried"]
        count = (tried["count"] if tried and same(tried["name"], name) else 0) + 1
        self._row(uid)["tried"] = {"name": name, "session": sid, "at": _iso(now), "error": "did not end",
                                   "count": count, "next": _iso(now + _wait(count))}

    def advance(self, uid: str, name: str, sid: str, now: datetime) -> bool:
        """Memory has taken `name` for this id: sessions are told it from
        now on. False, and the earlier name kept, when another id has been
        told it by then."""
        row = self._row(uid)
        row["tried"] = None
        if holder := next((o for o, r in self.rows.items()
                           if o != uid and any(same(t["name"], name) for t in r["told"])), None):
            row["slack"]["why"] = f"{name} is {holder}'s"
            return False
        row["told"].append({"name": name, "since": _iso(now), "session": sid})
        row["kept"] = None
        return True

    def keep(self, uid: str, name: str, sid: str, said: str, plain: bool, now: datetime) -> None:
        """Memory kept the earlier name: `plain` when that was its choice,
        otherwise the new name finds someone the earlier one does not."""
        row = self._row(uid)
        row["kept"] = {"name": name, "at": _iso(now), "session": sid, "memory": said, "plain": plain,
                       "alerted": False}
        row["tried"] = None

    def failed(self, uid: str, name: str, error: str, now: datetime, *, counted: bool = True) -> None:
        """A try that ended with no outcome, its count and wait as `trying`
        set them. Not `counted`, a refusal before the model ran, `error` being
        the run budget's verdict: its count is taken back and it waits an
        hour."""
        row = self._row(uid)
        tried = row["tried"]
        if not tried or not same(tried["name"], name):
            tried = row["tried"] = {"name": name, "session": "", "at": _iso(now), "error": "did not end",
                                    "count": 1, "next": _iso(now + _wait(1))}
        if not counted:
            _uncount(tried)
            tried["next"] = _iso(now + FIRST_WAIT)
        tried["error"] = error

    def stopped(self, uid: str, now: datetime, *, mid_run: bool = False) -> None:
        """A stop, which is not a failure: the count is taken back and the
        change is due again at once, unless the stop came after the model
        began (`mid_run`), when memory is read first, since it may have
        written."""
        tried = self._row(uid)["tried"]
        if tried:
            _uncount(tried)
            tried.update(error="stopped mid-run" if mid_run else "stopped", next=_iso(now))

    def untried(self, uid: str) -> None:
        """A try memory did not take, for a name Slack no longer shows: there
        is nothing left to hand."""
        self._row(uid)["tried"] = None

    def unanswered(self, uid: str, error: str) -> None:
        """Memory could not be read after the try's own run: not a failure,
        since the model ran and may have written. The count is taken back,
        and `error` kept for doctor; `due` holds the id until a read of
        memory succeeds."""
        tried = self._row(uid)["tried"]
        if tried:
            _uncount(tried)
            tried["error"] = error

    def kept_again(self, uid: str, plain: bool, said: str) -> str:
        """A kept change read again with no session, memory still not giving
        the new name. An alerted keep that memory now keeps by its own
        choice, as once a second person is forgotten, ends, so a session of
        its own is told the change again. A plain keep whose new name memory
        now gives someone else as well becomes an alerted one. Returns what
        changed, for the log, or ""."""
        row = self._row(uid)
        kept = row["kept"]
        if kept is None or kept["plain"] == plain:
            return ""
        if plain:
            row["kept"] = None
            return (f"names: {uid}'s change to {kept['name']} is handed again: memory no longer gives that name "
                    "to someone else")
        kept.update(plain=False, alerted=False, memory=said)
        return (f"names: {uid} stays {self.told(uid)} to sessions: memory now gives {kept['name']} to someone else "
                f"as well ({said})")

    # --- the alerts that go once ---

    def unalerted(self, now: datetime) -> list[str]:
        """Each allowed id whose `out` has not been alerted, at most one
        alert a UTC day each, so an id that flaps is said once."""
        today = _iso(now)[:10]
        return [uid for uid in self.allowed if (row := self.rows.get(uid)) and row["out"]
                and not row["out"]["alerted"] and row["out_day"] != today]

    def alerted(self, uid: str, out: dict, now: datetime) -> None:
        """The alert about `out` has gone: the answer that showed no one when
        the alert was made, which a read may have ended or replaced while it
        was posted. Either way the id is not alerted again that UTC day."""
        out["alerted"] = True
        self.rows[uid]["out_day"] = _iso(now)[:10]

    def unalerted_keeps(self) -> list[str]:
        """Each allowed id whose change memory did not take as theirs alone,
        not yet alerted: once for each such keep."""
        return [uid for uid in self.allowed if (row := self.rows.get(uid)) and (kept := row["kept"])
                and not kept["plain"] and not kept["alerted"]]

    # --- what the log and doctor say ---

    def asker(self, asked: str) -> str:
        """Who asked for a reminder, by the name it was asked under, lower
        case: that name as sessions were told it, the id it leads to, and the
        name sessions are told now when that is another."""
        uid = self.askers().get(asked)
        if uid is None:
            return asked
        then = next(t["name"] for t in self.rows[uid]["told"] if t["name"].lower() == asked)
        now = self.told(uid)
        return f"{then} ({uid})" if same(then, now) else f"{then} ({uid}, now {now})"

    def summary(self, uid: str) -> str:
        """An id and the name sessions are told for it, for the start's log."""
        told, row = self.told(uid), self.rows.get(uid)
        if told is None:
            return f"{uid} not let in"
        s = row["slack"]
        if row["out"]:
            return f"{uid} as {told} (Slack: {row['out']['why']})"
        if s["name"] and same(s["name"], told):
            return f"{uid} as {told} ({s['field']})"
        return f"{uid} as {told} (Slack shows {s['name'] or 'no name sessions can use'})"

    def state(self, store, uid: str, now: datetime, zone: tzinfo) -> tuple[bool, str]:
        """Doctor's line for an id, from the store alone, and whether it
        passes: one that fails needs a person."""
        row = self.rows.get(uid)

        def when(iso: str) -> str:
            at = _at(iso).astimezone(zone)
            return f"{at:%H:%M}" if at.date() == now.astimezone(zone).date() else f"{at:%Y-%m-%d %H:%M}"

        if row is None:
            return True, "no name yet; the first start reads Slack"
        s, out, told = row["slack"], row["out"], self.told(uid)
        unread = s["unread"]["at"] > s["read"]
        stale = not s["read"] or now - _at(s["read"]) > timedelta(days=1)
        could_not = (f"Slack could not be read: {s['unread']['error']} "
                     f"(last read {when(s['read']) if s['read'] else 'never'})")
        if told is None:
            if out and out["why"] == "no usable name":
                return False, f"not let in: no name Slack gives can be used ({s['why']})"
            if out and out["why"] == "user_not_found":
                return False, f"not let in: {_no_member(uid)}"
            if out:
                return False, f"not let in: Slack does not show them ({out['why']})"
            if unread:
                return False, f"not let in: {could_not}"
            return True, "no name yet; the first start reads Slack"
        latest = row["told"][-1]
        line = f"sessions say {told}, since {when(latest['since'])}" + (
            f" (session {latest['session'][:8]})" if latest["session"] else "")
        ok = True
        if out:
            ok, line = False, f"Slack no longer shows them ({out['why']}); sessions say {told}, as before"
        elif s["name"] is None and s["read"]:
            ok, line = False, f"{line}; no name Slack gives can be used ({s['why']})"
        elif s["name"]:
            line += (f"; Slack shows {s['name']} (display name {spelled(s['display']) or 'empty'}, "
                     f"full name {spelled(s['full']) or 'empty'})")
            kept = row["kept"]
            shows_kept = kept and same(s["name"], kept["name"])
            if self.awaiting(uid):
                line += f" since {when(s['seen'])}, awaiting handoff"
            elif shows_kept and kept["plain"]:
                line += f", which memory keeps as {told} (session {kept['session'][:8]}): {kept['memory']}"
            elif not shows_kept:
                line += f", read {when(s['read'])}"
            if kept and not kept["plain"]:
                # until the keep ends, whatever Slack shows meanwhile
                ok = False
                line += (": memory did not take it" if shows_kept else f"; memory did not take {kept['name']}") + (
                    f" as theirs alone (session {kept['session'][:8]}): {kept['memory']}")
            if s["why"]:
                line += f"; {s['why']}"
        if tried := row["tried"]:
            fine, said = self._try_state(store, tried, when)
            ok = ok and fine
            # a try outlives a change Slack has since moved on from
            line += (said if same(tried["name"], s["name"] or "") and self.awaiting(uid)
                     else f"; a handoff of {tried['name']}{said}")
        if unread:
            ok = ok and not stale
            line += f"; {could_not}"
        return ok, line

    def _try_state(self, store, tried: dict, when) -> tuple[bool, str]:
        session, error = tried["session"][:8], tried["error"]
        # before awaits_memory: a try still reading "did not end" has had no
        # memory read, whether or not its run was recorded; its session is
        # running, or a stop or crash cut it short and the next start reads it
        if error == "did not end":
            return False, ": did not end (running now, or the daemon has not started since)"
        if self.awaits_memory(store, tried):
            if error == "stopped mid-run":
                return False, f": stopped mid-run (session {session}); memory is read again at each refresh"
            return False, (f": memory's answer could not be read (session {session}): {error}; "
                           "read again at each refresh")
        if error == "cut short":
            return False, f": cut short; next after {when(tried['next'])}"
        if error == "stopped":
            return True, f": the last try was stopped; next after {when(tried['next'])}"
        if error in REFUSALS:
            return True, f": the last try was refused ({error}); next after {when(tried['next'])}"
        return False, (f": {tries(tried['count'])}, the last at {when(tried['at'])}: {error}; "
                       f"next after {when(tried['next'])}")
