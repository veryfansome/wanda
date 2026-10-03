"""Sessions the clock starts rather than a message.

Two things wake her: each person's morning look, once a day, and an
undertaking of hers whose `--by` names a time of day, when that time comes,
for the person who asked for it. Quiet hours hold back only the morning look,
which nobody asked for. What has fired is kept in the store, so a restart
neither repeats a wake nor forgets one that has not run yet."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta

log = logging.getLogger("wanda.clock")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# The clock frame and what a morning look says woke her. The lab renders the
# same two (CLOCK and MORNING in lab/harness/src/arrival.rs), and `mem session`
# reads the frame back, so all three change together.
CLOCK = ("No message started this session. What I say now reaches {speaker} alone, in a direct "
         "message.\n\n    {text}")
MORNING = "It is {weekday}, {time}, and this is my look at the day ahead for {speaker}."
# what woke her for a timed undertaking, above the item as `mem due` prints it
COME_DUE = "It is {weekday}, {time}, and this has come due:"
# below the item, when a session woken for it before was cut short: what the
# store holds of that session, a note that the reminder was given among it,
# can read as given, though nothing reached the person
AGAIN = "A session I began for this earlier was cut short before anything reached {speaker}."
# last under an item asked for under a name its asker no longer goes by,
# `{asked}` as the item gives it and `{now}` the name sessions are told now: a
# session that cannot tie the two names could take the reminder for someone
# else's
ASKED_THEN = "When this was asked, I knew {now} as {asked}."

# One item as `mem due` prints it, whatever its date was written as. The
# indented lines under it say who and what it involves, any rule it is held
# to, and who asked.
DUE = re.compile(r"^`trajectory:(?P<id>\w+)`  (?P<by>[^,]+), ")
# The one shape of a time of day the clock wakes her at, as `mem` writes it;
# memory/src/due.rs reads the same shape, so the look's list marks as still
# to come only what the clock wakes for.
TIMED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
INVOLVES = "    involves: "
ASKED_BY = "    asked by: "
# the line a look's list puts under an item the clock will give at its time
# (memory/src/due.rs, STILL_TO_COME)
STILL_TO_COME = "    still to come: "
# the whole of that line, for one whose time has come while its wake still
# waits, which due.rs, knowing nothing of the wakes, cannot mark (still_to_come)
STILL_TO_COME_LINE = (STILL_TO_COME + "the clock gives it to {asker} at {time}, but only while it is open "
                      "and timed so")
# the labels her own node goes by, which only her own undertakings link to;
# memory/src/due.rs holds the same two for the look's list
SELF = ("me", "wanda")

# A timed undertaking noticed more than this late, after a restart or a wake
# that kept failing to start, is not woken: hours after the time asked for, it
# is not the reminder that was asked for. It is kept as a reminder not given.
LATE = timedelta(hours=2)
# A look that has not run by noon is skipped for the day; by then it is not a
# look at the day ahead. The next look's list starts from the last look
# recorded ok, so what came due in between is still put to it.
LOOK_BY = time(12, 0)


@dataclass(frozen=True)
class Item:
    id: str
    by: str
    lines: tuple[str, ...]  # as `mem due` printed them, the item's own line first

    def field(self, prefix: str) -> str:
        return next((ln[len(prefix):] for ln in self.lines if ln.startswith(prefix)), "")

    @property
    def involves(self) -> list[str]:
        return [n.strip().lower() for n in self.field(INVOLVES).split(";") if n.strip()]

    @property
    def asked_by(self) -> str:
        return self.field(ASKED_BY).strip().lower()


def items(due: str) -> list[Item]:
    """What `mem due` printed, item by item. An indented line belongs to the
    item above it, and to none once any other line comes between: one item's
    rule or asker in another's frame could reach someone it is kept from."""
    out: list[tuple[str, str, list[str]]] = []
    lines: list[str] | None = None
    for line in due.splitlines():
        if m := DUE.match(line):
            lines = [line]
            out.append((m["id"], m["by"], lines))
        elif line.startswith("    "):
            if lines is not None:
                lines.append(line)
        else:
            lines = None
    return [Item(i, by, tuple(lines)) for i, by, lines in out]


@dataclass(frozen=True)
class Wake:
    key: str      # what the store keeps so this wake fires once
    person: str   # the member id whose direct message what she says goes to
    text: str     # the indented lines of the frame: what woke her
    about: str = ""  # for a timed wake, its item's id; `by` is its time; an alert names both
    by: str = ""
    # for a timed wake, who asked, as its item names them, lower case: its key
    # and its records keep the name, which is what a look's list shows
    asked: str = ""

    def arrival(self, speaker: str, listed: list[str] | tuple[str, ...] = ()) -> str:
        """The frame, naming the person as sessions know them, with the list
        a morning look is handed below what woke her, when there is one; the
        lab composes it the same way."""
        text = self.text + ("\n\n    " + "\n    ".join(listed) if listed else "")
        return CLOCK.format(speaker=speaker, text=text)


def hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def mornings(spec: list[str]) -> dict[str, time]:
    """`U0123456789@08:00` entries, as WANDA_MORNINGS lists them, each
    member id kept as given, as the allowlist keeps it. An entry with no id,
    or no time of day, is a ValueError."""
    out = {}
    for item in spec:
        uid, at = item.split("@")
        if not uid.strip():
            raise ValueError(item)
        out[uid.strip()] = hhmm(at)
    return out


def quiet_hours(spec: str) -> tuple[time, time] | None:
    """`21:30-07:00`; empty for none."""
    if not spec.strip():
        return None
    start, end = spec.split("-")
    return hhmm(start), hhmm(end)


def settings_problem(spec: list[str], quiet_spec: str, allowed: list[str]) -> str | None:
    """What is wrong with the clock's settings, in one sentence, or None. The
    daemon refuses to start on it and doctor reports it."""
    try:
        looks = mornings(spec)
    except ValueError:
        return (f"WANDA_MORNINGS={','.join(spec)} is not a list of <member id>@HH:MM, "
                "as U0123456789@08:00,U0987654321@07:30")
    given = [item.split("@")[0].strip() for item in spec]
    if twice := sorted({uid for uid in given if given.count(uid) > 1}):
        return f"WANDA_MORNINGS gives {', '.join(twice)} more than one time, and a person has one look a day"
    # a look is a session with the household's memory, in the person's own
    # direct message: only someone allowed to start sessions has one
    if unknown := sorted(set(looks) - set(allowed)):
        return (f"WANDA_MORNINGS names {', '.join(unknown)}, which is not in WANDA_SLACK_OWNER_USER_IDS: "
                "it takes member ids, as U0123456789@08:00")
    if late := sorted(name for name, at in looks.items() if at >= LOOK_BY):
        return (f"WANDA_MORNINGS puts {', '.join(late)} at or after {LOOK_BY:%H:%M}, "
                "when a look is skipped for the day")
    try:
        quiet = quiet_hours(quiet_spec)
    except ValueError:
        return f"WANDA_QUIET_HOURS={quiet_spec} is not HH:MM-HH:MM, as 21:30-07:00"
    # a look waits for quiet hours to end, and is skipped at noon
    if shut := sorted(name for name, at in looks.items()
                      if all(is_quiet(time(m // 60, m % 60), quiet)
                             for m in range(at.hour * 60 + at.minute, LOOK_BY.hour * 60))):
        return (f"WANDA_QUIET_HOURS={quiet_spec} keeps {' and '.join(shut)} from having a look "
                f"before {LOOK_BY:%H:%M}, when a look is skipped for the day")
    return None


def is_quiet(t: time, quiet: tuple[time, time] | None) -> bool:
    if quiet is None:
        return False
    start, end = quiet
    # quiet hours usually run past midnight, where the start is the later time
    return start <= t < end if start <= end else (t >= start or t < end)


def morning_wakes(now: datetime, looks: dict[str, time], quiet: tuple[time, time] | None,
                  last_look: Callable[[str], str | None], called: Callable[[str], str]) -> list[Wake]:
    """Each person whose morning has come and who has had no look today, by
    member id, the look saying the name sessions know them by (`called`). A
    look missed while the daemon was down runs when it is back, before noon,
    and says the time it actually runs at."""
    if is_quiet(now.time(), quiet) or now.time() >= LOOK_BY:
        return []
    today = now.date().isoformat()
    return [
        Wake(f"clock:morning:{uid}", uid,
             MORNING.format(weekday=WEEKDAYS[now.weekday()], time=now.strftime("%H:%M"),
                            speaker=called(uid)))
        for uid, at in looks.items()
        if now.time() >= at and last_look(uid) != today
    ]


def missed(now: datetime, looks: dict[str, time], last_look: Callable[[str], str | None]) -> list[str]:
    """Each person whose look did not run today, once noon has come."""
    if now.time() < LOOK_BY:
        return []
    today = now.date().isoformat()
    return [person for person in looks if last_look(person) != today]


def asked_then(item: Item, askers: dict[str, str], told: dict[str, str] | None) -> str:
    """ASKED_THEN as a line of the item, indented as its own lines are, when
    the name it was asked under leads to a member whom sessions are now told
    another name for (`told`, each let-in id's name); "" otherwise."""
    asked = item.field(ASKED_BY).strip()
    now = (told or {}).get(askers.get(item.asked_by, ""))
    if not now or now.lower() == item.asked_by:
        return ""
    return "    " + ASKED_THEN.format(now=now, asked=asked)


def due_wakes(now: datetime, due: list[Item], askers: dict[str, str], fired: Callable[[str], bool],
              said: set[str], lost: Callable[[Item, str], None] = lambda item, why: None,
              told: dict[str, str] | None = None) -> list[Wake]:
    """Each undertaking of hers whose `--by` carries a time of day that has
    come, for the person who asked for it, at that time whatever the hour, in
    the direct message of the member id `askers` leads their name to. A
    bare date is the morning look's: the whole day is its moment. One whose
    time has come and that is not woken (not hers, no one person known to
    have asked, a time no clock shows, or noticed too late) is said once in
    the log, with its id and why; one noticed too late, which the clock was
    to give, is also handed to `lost`, since the reminder asked for was not
    given. One asked for under a name the person no longer goes by says so
    under the item (ASKED_THEN, from `told`)."""
    wall = now.replace(tzinfo=None)
    out: list[Wake] = []

    def dropped(item: Item, why: str) -> None:
        if (item.id, why) not in said:
            said.add((item.id, why))
            log.warning("clock: %s (%s) is not woken for: %s", item.id, item.by, why)

    for item in due:
        if len(item.by) == len("YYYY-MM-DD"):
            continue
        mine = any(n in SELF for n in item.involves)
        try:
            if not TIMED.match(item.by):
                raise ValueError(item.by)
            at = datetime.fromisoformat(item.by)
        except ValueError:
            dropped(item, "it has a time no clock shows")
            continue
        if at > wall:
            continue
        if not mine:
            dropped(item, "it is not an undertaking of hers")
        elif item.asked_by not in askers:
            dropped(item, f"who asked is not known ({item.asked_by or 'no one person'})")
        else:
            key = f"clock:due:{item.id}:{item.by}:{item.asked_by}"
            if fired(key):
                continue
            if at <= wall - LATE:
                why = f"its time was more than {LATE.seconds // 3600} h gone when the clock saw it"
                dropped(item, why)
                lost(item, why)
                continue
            head = COME_DUE.format(weekday=WEEKDAYS[now.weekday()], time=now.strftime("%H:%M"))
            lines = item.lines + ((then,) if (then := asked_then(item, askers, told)) else ())
            out.append(Wake(key, askers[item.asked_by], "\n    ".join((head, *lines)), item.id, item.by,
                            item.asked_by))
    return out


def still_to_come(listed: list[str], now: datetime, askers: dict[str, str],
                  fired: Callable[[str], bool], told: dict[str, str] | None = None) -> list[str]:
    """A look's list, with each undertaking of hers whose time has come and
    whose wake has not run marked as still to come, as due.rs marks those
    later that day. A look that starts after such a time, while the wake waits
    behind a session or a restart, would otherwise be handed the reminder
    unmarked, and could close the one the clock is about to give. The test is
    `due_wakes`' own: the clock wakes for it at the next due check. The mark
    says what the clock will do as the look starts: a look still running when
    the reminder's time is LATE gone has been told of a wake the clock can no
    longer give, and the reminder is kept as not given. Any item asked for
    under a name a member no longer goes by, whoever asked, says so last
    (ASKED_THEN, from `told`)."""
    wall = now.replace(tzinfo=None)
    waiting: dict[str, list[str]] = {}
    for item in items("\n".join(listed)):
        if (then := asked_then(item, askers, told)) and then not in item.lines:
            waiting.setdefault(item.id, []).append(then)
        if (item.field(STILL_TO_COME) or not TIMED.match(item.by) or item.asked_by not in askers
                or not any(n in SELF for n in item.involves)):
            continue
        try:
            at = datetime.fromisoformat(item.by)
        except ValueError:
            # a time no clock shows (T24:00, written by hand): the clock never
            # wakes for it, which due_wakes logs, and the look still runs
            continue
        if (wall - LATE < at <= wall
                and not fired(f"clock:due:{item.id}:{item.by}:{item.asked_by}")):
            # the asker as `mem due` printed them, as due.rs's own mark does
            waiting.setdefault(item.id, []).insert(0, STILL_TO_COME_LINE.format(
                asker=item.field(ASKED_BY).strip(), time=item.by[11:]))
    out: list[str] = []
    owed: list[str] = []
    for line in listed:
        # last under the item, as due.rs puts it: after its own indented lines
        if owed and not line.startswith("    "):
            out += owed
            owed = []
        out.append(line)
        if m := DUE.match(line):
            owed = waiting.get(m["id"], [])
    return out + owed


def first_start(at: time, quiet: tuple[time, time] | None) -> time:
    """When a look set for `at` can first start: then, or when the quiet
    hours it falls in end."""
    return quiet[1] if is_quiet(at, quiet) else at


def look_healthy(outcome: str | None, now: datetime, running: timedelta, start: time) -> bool:
    """Whether a person's last look, as the store records it ("<date> <HH:MM>
    <outcome>"), is one doctor passes: it spoke or stayed silent today, one
    that spoke and then failed in a later turn included; or it started no
    longer ago than a look can take; or, until today's is late, none has run
    yet or the day before's spoke or stayed silent. Today's is late at noon,
    or once a look's length has passed since it could first start (`start`),
    since it waits behind another session no longer than that. A look a
    restart cut short stays `started`, one that keeps failing before it is
    claimed reads `could not start`, and a day with no look leaves the day
    before's, or `skipped`. The alert for a look that cannot start may not
    say whose it is, so this is where it shows."""
    late = now.time() >= LOOK_BY or now - datetime.combine(now.date(), start, now.tzinfo) > running
    if not outcome:
        return not late
    day, hm, what = outcome.split(" ", 2)
    at = datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=now.tzinfo)
    if what == "started":
        return now - at <= running
    if what.partition(",")[0] not in ("spoke", "silent"):
        return False
    return at.date() == now.date() or (at.date() == now.date() - timedelta(days=1) and not late)
