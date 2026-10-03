"""The clock: when it wakes her and for whom, that it wakes her once across
restarts and clock changes, and what the session it starts is handed."""

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wanda import clock, vault
from wanda.config import Config
from wanda.household import Household
from wanda.main import BUDGET_REPLIES, REOPENED, Processor, run_daemon, run_doctor
from wanda.runner import RunnerService, RunResult
from wanda.store import Store, utcnow

ROOT = Path(__file__).resolve().parent.parent
LA = ZoneInfo("America/Los_Angeles")
LOOKS = clock.mornings(["U1@08:00", "U2@07:30"])
QUIET = clock.quiet_hours("21:30-07:00")
# the names sessions know fan and mei by, and the ids those names lead to
NAMES = {"U1": "fan", "U2": "mei"}
ASKERS = {"fan": "U1", "mei": "U2"}

# what `mem due --after 2026-09-29` printed on 2026-10-01 for a small vault
# built through `mem`: her undertakings at a time of day, one kept from mei,
# one mei asked for on fan's behalf, one inside quiet hours and one at a time
# no clock shows, written before `mem` refused such times; a bare-date
# undertaking; an appointment at a time of day that is not hers; and a
# delivery gone by, from an email
DUE = """\
`trajectory:27212d`  2026-10-01T24:00, today  I undertook to remind mei at midnight to lock the shed
    involves: me; mei
    asked by: mei
`trajectory:07f41d`  2026-10-01T21:45, today  I undertook to remind fan at 9:45pm to take his tablet
    involves: me; fan
    asked by: fan
`trajectory:b6647b`  2026-10-01T19:00, today  I undertook to remind fan at 7 to pick up the gift for mei
    involves: me; fan; mei
    constrained_by: keep the gift from mei
    asked by: fan
`trajectory:9cdc0d`  2026-10-01T18:00, today  the meeting at 6pm
    involves: mei
    asked by: mei
`trajectory:a24e0d`  2026-10-01T17:00, today  I undertook to remind fan at 5 to call the plumber
    involves: me; fan
    asked by: mei
`trajectory:eca883`  2026-10-01, today  I undertook to remind mei on the 1st to post the letter
    involves: me; mei
    asked by: mei
`trajectory:914581`  2026-09-30, 1 day ago  the kettle should have arrived by the 30th
    involves: fan
"""
ITEMS = clock.items(DUE)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("WANDA_"):
            monkeypatch.delenv(key, raising=False)


def minutes(start: datetime, end: datetime):
    """Every minute between two instants, as the household's wall clock shows
    it — stepping in UTC, so a clock change is lived through, not skipped."""
    t = start.astimezone(timezone.utc)
    while t < end.astimezone(timezone.utc):
        yield t.astimezone(LA)
        t += timedelta(minutes=1)


def run_mornings(start, end, store_meta: dict, down=(), looks=LOOKS):
    fired = []
    for now in minutes(start, end):
        if any(a <= now < b for a, b in down):
            continue
        for w in clock.morning_wakes(now, looks, QUIET, lambda p: store_meta.get(f"clock:morning:{p}"), NAMES.get):
            store_meta[w.key] = now.date().isoformat()
            fired.append((now, w))
    return fired


def test_each_morning_wakes_once_per_person_per_day():
    meta: dict = {}
    fired = run_mornings(datetime(2026, 10, 1, 0, 0, tzinfo=LA), datetime(2026, 10, 4, 0, 0, tzinfo=LA), meta)
    assert [(n.strftime("%m-%d %H:%M"), w.person) for n, w in fired] == [
        ("10-01 07:30", "U2"), ("10-01 08:00", "U1"),
        ("10-02 07:30", "U2"), ("10-02 08:00", "U1"),
        ("10-03 07:30", "U2"), ("10-03 08:00", "U1"),
    ]


def test_a_morning_missed_while_down_runs_when_back_and_says_when():
    meta: dict = {}
    down = [(datetime(2026, 10, 1, 6, 0, tzinfo=LA), datetime(2026, 10, 1, 10, 17, tzinfo=LA))]
    fired = run_mornings(datetime(2026, 10, 1, 0, 0, tzinfo=LA), datetime(2026, 10, 2, 0, 0, tzinfo=LA), meta, down)
    assert [(n.strftime("%H:%M"), w.person) for n, w in fired] == [("10:17", "U1"), ("10:17", "U2")]
    assert fired[0][1].text == "It is Thursday, 10:17, and this is my look at the day ahead for fan."


def test_a_morning_missed_until_noon_is_skipped_for_the_day():
    meta: dict = {}
    down = [(datetime(2026, 10, 1, 6, 0, tzinfo=LA), datetime(2026, 10, 1, 12, 0, tzinfo=LA))]
    fired = run_mornings(datetime(2026, 10, 1, 0, 0, tzinfo=LA), datetime(2026, 10, 2, 9, 0, tzinfo=LA), meta, down)
    assert [(n.strftime("%m-%d %H:%M"), w.person) for n, w in fired] == [
        ("10-02 07:30", "U2"), ("10-02 08:00", "U1")]


def test_a_restart_after_a_look_does_not_repeat_it(tmp_path):
    first = Store(tmp_path / "w.db")
    now = datetime(2026, 10, 1, 8, 5, tzinfo=LA)
    w = clock.morning_wakes(now, {"U1": LOOKS["U1"]}, QUIET, lambda p: first.get_meta(f"clock:morning:{p}"),
                            NAMES.get)[0]
    first.set_meta(w.key, now.date().isoformat())
    first.close()
    again = Store(tmp_path / "w.db")
    later = now + timedelta(minutes=30)
    assert clock.morning_wakes(later, {"U1": LOOKS["U1"]}, QUIET,
                               lambda p: again.get_meta(f"clock:morning:{p}"), NAMES.get) == []
    tomorrow = now + timedelta(days=1)
    assert len(clock.morning_wakes(tomorrow, {"U1": LOOKS["U1"]}, QUIET,
                                   lambda p: again.get_meta(f"clock:morning:{p}"), NAMES.get)) == 1


def test_a_morning_inside_quiet_hours_waits_for_them_to_end():
    meta: dict = {}
    fired = run_mornings(datetime(2026, 10, 1, 0, 0, tzinfo=LA), datetime(2026, 10, 1, 12, 0, tzinfo=LA),
                         meta, looks=clock.mornings(["U1@06:15"]))
    assert [n.strftime("%H:%M") for n, _ in fired] == ["07:00"]


@pytest.mark.parametrize("start", [datetime(2026, 10, 31, 0, 0), datetime(2027, 3, 13, 0, 0)],
                         ids=["clocks go back", "clocks go forward"])
def test_a_clock_change_neither_doubles_nor_drops_a_morning(start):
    meta: dict = {}
    fired = run_mornings(start.replace(tzinfo=LA), (start + timedelta(days=3)).replace(tzinfo=LA), meta)
    days = [n.date().isoformat() for n, w in fired if w.person == "U1"]
    assert len(days) == 3 and len(set(days)) == 3
    assert all(n.strftime("%H:%M") == "08:00" for n, w in fired if w.person == "U1")


def due_at(now, fired=frozenset(), said=None):
    return clock.due_wakes(now, ITEMS, ASKERS, lambda k: k in fired, set() if said is None else said)


def test_a_timed_undertaking_wakes_only_the_person_who_asked_when_its_time_comes():
    day = datetime(2026, 10, 1, tzinfo=LA)
    assert due_at(day.replace(hour=16, minute=55)) == []
    # fan's plumber call was asked for by mei: the wake is hers
    at5 = due_at(day.replace(hour=17, minute=0))
    assert [(w.person, w.key) for w in at5] == [("U2", "clock:due:a24e0d:2026-10-01T17:00:mei")]
    # the 6pm meeting is not her undertaking, so nothing wakes for it
    assert [w.key for w in due_at(day.replace(hour=18, minute=5))] == [at5[0].key]
    # a gift kept from mei wakes fan, who asked, and never mei
    at7 = due_at(day.replace(hour=19, minute=3), {w.key for w in at5})
    assert [w.person for w in at7] == ["U1"]
    assert at7[0].arrival("fan") == (
        "No message started this session. What I say now reaches fan alone, in a direct message.\n\n"
        "    It is Thursday, 19:03, and this has come due:\n"
        "    `trajectory:b6647b`  2026-10-01T19:00, today  I undertook to remind fan at 7 to pick up "
        "the gift for mei\n"
        "        involves: me; fan; mei\n"
        "        constrained_by: keep the gift from mei\n"
        "        asked by: fan")


def test_a_timed_wake_fires_once_and_not_when_long_past():
    day = datetime(2026, 10, 1, tzinfo=LA)
    first = due_at(day.replace(hour=17, minute=5))
    assert len(first) == 1
    assert due_at(day.replace(hour=17, minute=10), {w.key for w in first}) == []
    # more than two hours late is not woken for
    assert [w.key for w in due_at(day.replace(hour=19, minute=1))] == [
        "clock:due:b6647b:2026-10-01T19:00:fan"]
    assert due_at(day.replace(hour=21, minute=1)) == []


def test_a_time_asked_for_inside_quiet_hours_still_wakes():
    at = due_at(datetime(2026, 10, 1, 21, 45, tzinfo=LA))
    assert [(w.person, w.key) for w in at] == [("U1", "clock:due:07f41d:2026-10-01T21:45:fan")]


def test_one_whose_asker_is_not_known_is_left_to_the_morning():
    unknown = clock.items(DUE.replace("    asked by: fan\n", ""))
    keys = {w.key for h in range(24) for m in (0, 30)
            for w in clock.due_wakes(datetime(2026, 10, 1, h, m, tzinfo=LA), unknown, ASKERS,
                                     lambda k: False, set())}
    assert not any("b6647b" in k or "07f41d" in k for k in keys)
    assert any("a24e0d" in k for k in keys)


def test_a_bare_date_and_a_date_gone_by_are_left_to_the_morning():
    keys = {w.key for h in range(24) for w in due_at(datetime(2026, 10, 1, h, 30, tzinfo=LA))}
    assert not any("eca883" in k or "914581" in k for k in keys)


def test_a_time_no_clock_shows_is_skipped_and_said_once(caplog):
    """Hers or not: the due check skips it and says so once."""
    items = ITEMS + clock.items("`trajectory:5e1a0c`  2026-10-01T24:00, today  parents' evening runs "
                                "to midnight\n    involves: mei\n    asked by: mei\n")
    said: set = set()
    with caplog.at_level(logging.WARNING, logger="wanda.clock"):
        for h in (17, 18, 19):
            woke = clock.due_wakes(datetime(2026, 10, 1, h, 0, tzinfo=LA), items, ASKERS,
                                   lambda k: False, said)
            assert woke, "the rest of the list is still woken for"
    got = [r.getMessage() for r in caplog.records]
    for item in ("27212d", "5e1a0c"):
        assert got.count(f"clock: {item} (2026-10-01T24:00) is not woken for: it has a time no "
                         "clock shows") == 1, got


def test_what_is_not_woken_is_said_once_with_its_id_and_why(caplog):
    """Not hers, no one person known to have asked, or noticed more than two
    hours late: each is in the log once, from the tick its time has come."""
    unknown = clock.items(DUE.replace("    asked by: fan\n", ""))
    said: set = set()
    with caplog.at_level(logging.WARNING, logger="wanda.clock"):
        for at in (datetime(2026, 10, 1, 16, 0, tzinfo=LA), datetime(2026, 10, 1, 21, 50, tzinfo=LA),
                   datetime(2026, 10, 1, 21, 55, tzinfo=LA)):
            assert clock.due_wakes(at, unknown, ASKERS, lambda k: False, said) == []
    got = sorted(r.getMessage() for r in caplog.records)
    assert got == sorted([
        "clock: 27212d (2026-10-01T24:00) is not woken for: it has a time no clock shows",
        "clock: 07f41d (2026-10-01T21:45) is not woken for: who asked is not known (no one person)",
        "clock: b6647b (2026-10-01T19:00) is not woken for: who asked is not known (no one person)",
        "clock: 9cdc0d (2026-10-01T18:00) is not woken for: it is not an undertaking of hers",
        "clock: a24e0d (2026-10-01T17:00) is not woken for: its time was more than 2 h gone when "
        "the clock saw it",
    ]), got
    # one already woken is not said to be late
    caplog.clear()
    clock.due_wakes(datetime(2026, 10, 1, 21, 50, tzinfo=LA), ITEMS, ASKERS,
                    lambda k: "a24e0d" in k, set())
    assert not any("a24e0d" in r.getMessage() for r in caplog.records)


def test_her_own_node_goes_by_the_same_labels_in_the_list_and_the_wakes():
    src = (ROOT / "memory/src/due.rs").read_text()
    labels = re.search(r"pub const SELF: \[&str; 2\] = \[(.*?)\];", src).group(1)
    assert tuple(re.findall(r'"(\w+)"', labels)) == clock.SELF


def test_the_clock_marks_a_reminder_in_due_rs_words():
    """A look's list carries one mark, whether due.rs or the clock put it."""
    src = (ROOT / "memory/src/due.rs").read_text()
    words = re.search(r'pub const STILL_TO_COME: &str =\s*"(.*?)";', src, re.DOTALL).group(1)
    assert clock.STILL_TO_COME_LINE == "    " + words


def test_a_time_mem_would_refuse_is_said_and_its_lines_stay_its_own(caplog):
    """A time written by hand in a shape `mem` refuses is not woken for, and
    is said once, as due.rs does not mark it as still to come in the look's
    list; its indented lines, a rule and an asker among them, never join
    another item's frame."""
    due = ("`trajectory:1ba5c2`  2026-10-01T09:30, today  I undertook to remind fan at 9:30 to ring the bank\n"
           "    involves: me; fan\n    asked by: fan\n"
           "`trajectory:cec212`  2026-09-30T9:00, 1 day ago  I undertook to remind mei to order the gift\n"
           "    involves: me; mei\n    constrained_by: keep the gift from fan\n    asked by: mei\n"
           "`trajectory:7c443c`  2026-10-01 16:00, today  I undertook to remind mei at 4pm to call the vet\n"
           "    involves: me; mei\n    asked by: mei\n"
           "Come due for fan after 2026-09-30:\n    a stray line under no item\n")
    got = clock.items(due)
    assert [(i.id, i.by, len(i.lines)) for i in got] == [
        ("1ba5c2", "2026-10-01T09:30", 3), ("cec212", "2026-09-30T9:00", 4), ("7c443c", "2026-10-01 16:00", 3)]
    said: set = set()
    woke = []
    with caplog.at_level(logging.WARNING, logger="wanda.clock"):
        for h in (9, 16, 17):
            woke += clock.due_wakes(datetime(2026, 10, 1, h, 35, tzinfo=LA), got, ASKERS, lambda k: False, said)
    # the bank at 9:35, and nothing for the two by hand at 16:35 or after
    assert [(w.person, w.about) for w in woke] == [("U1", "1ba5c2")]
    assert "keep the gift from fan" not in woke[0].text and "mei" not in woke[0].text
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs.count("clock: cec212 (2026-09-30T9:00) is not woken for: it has a time no clock shows") == 1
    assert msgs.count("clock: 7c443c (2026-10-01 16:00) is not woken for: it has a time no clock shows") == 1


def rust_const(path: str, name: str) -> str:
    """A `pub const NAME: &str = "...";` as the program holds it."""
    src = (ROOT / path).read_text()
    lit = re.search(rf'pub const {name}: &str = "(.*?)";', src, re.DOTALL)
    assert lit, f"{name} is gone from {path}"
    return re.sub(r"\\\n\s*", "", lit.group(1)).replace("\\n", "\n")


def test_the_frame_is_the_one_the_lab_renders_and_mem_reads_back():
    if not (ROOT / "lab/harness/src/arrival.rs").exists():
        pytest.skip("no lab in this checkout")
    assert rust_const("lab/harness/src/arrival.rs", "CLOCK") == clock.CLOCK
    assert rust_const("lab/harness/src/arrival.rs", "MORNING") == clock.MORNING
    # the composed look, as arrival.rs's own test pins it
    w = clock.Wake("clock:morning:U2", "U2",
                   clock.MORNING.format(weekday="Thursday", time="08:00", speaker="mei"))
    assert w.arrival("mei", ["Come due for mei after 2026-07-18:",
                             "`trajectory:aaaaaa`  2026-07-23, today  x"]) == (
        "No message started this session. What I say now reaches mei alone, in a direct message.\n\n"
        "    It is Thursday, 08:00, and this is my look at the day ahead for mei.\n\n"
        "    Come due for mei after 2026-07-18:\n    `trajectory:aaaaaa`  2026-07-23, today  x")
    assert w.arrival("mei", []) == w.arrival("mei")


class FakeSlack:
    def __init__(self, fail_first=False, refuse=()):
        self.opened = []
        self.fail_first = fail_first
        self.refuse = set(refuse)
        self.alerts = []

    async def alert(self, text):
        self.alerts.append(text)

    async def dm_channel(self, user):
        self.opened.append(user)
        if self.fail_first and len(self.opened) == 1:
            raise RuntimeError("conversations.open: invalid_auth")
        if user in self.refuse:
            raise RuntimeError("conversations.open: user_not_found")
        return f"D-{user}"

    async def channel_type(self, channel):
        # asked before an answer owed is posted later: a clock session's DM
        # is a 1:1 DM
        return "im"


# What the daemon's runner did, as (what it returns, the answer it recorded): a
# budget verdict before the model runs, an error, or None with the answer.
SILENT, SPOKE = (None, ""), (None, "Morning. The letter goes in the post today.")
REFUSED, FAILED = ("busy", None), ("claude reported an error", None)


def settings(tmp_path, **kw) -> Config:
    return Config(_env_file=None, **{"mornings": ["U1@08:00", "U2@07:30"], "slack_owner_user_ids": "U1,U2",
                                     "tz": "America/Los_Angeles", "data_dir": tmp_path} | kw)


def named(store: Store, names: dict[str, str] = NAMES) -> Store:
    """The run store as a start leaves it once Slack has given each id its
    name, which sessions are then told."""
    h = Household({}, list(names))
    for uid, name in names.items():
        h.observe(uid, {"profile": {"display_name": name}}, datetime(2026, 9, 1, tzinfo=timezone.utc))
        h.save(store, uid)
    return store


def processor(tmp_path, slack, outcomes, mem_out=""):
    store = named(Store(tmp_path / "p.db"))
    p = Processor(settings(tmp_path), store, asyncio.Queue(), slack, RunnerService("/bin/true"))
    turns, alerts, mems = [], [], []

    async def memory_turn(task, arrival, now, *, channel, reply_thread, owed, state=None):
        turns.append((channel, task["thread_ts"], arrival, owed,
                      store.get_meta("clock:morning:U1"), store.get_meta("clock:morning:U2"),
                      reply_thread, p._task_locks[task["id"]].locked()))
        await asyncio.sleep(0)
        error, answer = outcomes.pop(0)
        if error not in BUDGET_REPLIES:
            # memory_turn records every run the model started, and posts
            # an answer it has, which Slack takes here
            store.record_run(kind="agent", task_id=task["id"], session_id="s", started_at=utcnow(),
                             exit_code=0, cost_usd=0.0, status="error" if error else "ok",
                             error=error, result_text=answer, notified=1)
        return error

    async def alert_once(kind, text):
        alerts.append((kind, text))

    async def mem(now, *args):
        mems.append(args)
        return mem_out

    p.memory_turn = memory_turn
    p._alert_once = alert_once
    p._mem = mem
    return p, store, turns, alerts, mems


def test_the_loop_starts_one_wake_at_a_time_in_the_persons_own_dm(tmp_path):
    listed = "Come due for mei after 2026-09-30:\n`trajectory:eca883`  2026-10-01, today  x\n    involves: me; mei\n"
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [SILENT, SPOKE], listed)
    now = datetime(2026, 10, 1, 8, 1, tzinfo=LA)

    async def go():
        wakes = clock.morning_wakes(now, LOOKS, QUIET, lambda q: store.get_meta(f"clock:morning:{q}"), NAMES.get)
        assert len(wakes) == 2
        p._wake(wakes, now)
        p._wake(wakes, now)  # one is running: the other waits
        await asyncio.gather(*p._bg)
        wakes = clock.morning_wakes(now, LOOKS, QUIET, lambda q: store.get_meta(f"clock:morning:{q}"), NAMES.get)
        p._wake(wakes, now)
        await asyncio.gather(*p._bg)
        return clock.morning_wakes(now, LOOKS, QUIET, lambda q: store.get_meta(f"clock:morning:{q}"), NAMES.get)

    assert asyncio.run(go()) == []
    assert [t[0] for t in turns] == ["D-U1", "D-U2"]
    # in the person's DM, not owed, and under their conversation's lock
    assert all(t[1] == "conversation" and t[3] is False and t[6] is None and t[7] for t in turns)
    # a first look is handed what came due since yesterday, as of its own time
    assert mems == [("due", "--for=fan", "--after", "2026-09-30", "--at", "08:01"),
                    ("due", "--for=mei", "--after", "2026-09-30", "--at", "08:01")]
    assert turns[1][2] == (
        "No message started this session. What I say now reaches mei alone, in a direct message.\n\n"
        "    It is Thursday, 08:01, and this is my look at the day ahead for mei.\n\n"
        "    Come due for mei after 2026-09-30:\n    `trajectory:eca883`  2026-10-01, today  x\n"
        "        involves: me; mei")
    assert store.get_meta("clock:outcome:U1") == "2026-10-01 08:01 silent"
    assert store.get_meta("clock:outcome:U2") == "2026-10-01 08:01 spoke"
    assert alerts == []


def test_the_next_look_is_handed_what_came_due_since_the_last_that_ran(tmp_path):
    """A look that failed said nothing, so the next one is handed its day too;
    a look that ran, spoken or silent, moves the start on."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [FAILED, SILENT, SPOKE])
    store.set_meta("clock:morning:U1", "2026-09-27")
    store.set_meta("clock:listed:U1", "2026-09-27")
    for day in (1, 2, 3):
        now = datetime(2026, 10, day, 8, 0, tzinfo=LA)
        w = clock.morning_wakes(now, {"U1": LOOKS["U1"]}, QUIET,
                                lambda q: store.get_meta(f"clock:morning:{q}"), NAMES.get)
        asyncio.run(p._clock_session(w[0], now))
        assert store.get_meta("clock:morning:U1") == now.date().isoformat(), "one look a day"
    assert [m[3] for m in mems] == ["2026-09-27", "2026-09-27", "2026-10-02"]
    task = store.get_task_by_thread("D-U1", "conversation")
    assert p._listed("U1", task) == "2026-10-03"


def test_a_restart_after_the_run_is_recorded_still_moves_the_list_on(tmp_path):
    """The start moves from the run the look recorded, so a restart between
    that record and the clock's own write does not hand the same day twice;
    a run the restart cancelled does not move it."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [SILENT, SILENT])
    store.create_task(None, "D-U1", "conversation", kind="dm", reply_thread=None)
    task = store.get_task_by_thread("D-U1", "conversation")
    store.set_meta("clock:listed:U1", "2026-09-29")
    store.set_meta("clock:trying:U1", f"2026-09-30 {task['id']} {store.newest_run(task['id'])} 2026-09-29")
    store.record_run(kind="agent", task_id=task["id"], session_id="s", started_at=utcnow(), exit_code=0,
                     cost_usd=0.0, status="ok", result_text="Morning.", notified=1)
    look = lambda now: clock.morning_wakes(now, {"U1": LOOKS["U1"]}, QUIET, lambda q: None, NAMES.get)[0]  # noqa: E731
    now = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    asyncio.run(p._clock_session(look(now), now))
    assert mems[0][3] == "2026-09-30"
    store.set_meta("clock:trying:U1", f"2026-10-02 {task['id']} {store.newest_run(task['id'])} 2026-10-01")
    store.record_run(kind="agent", task_id=task["id"], session_id="s", started_at=utcnow(), exit_code=None,
                     cost_usd=0.0, status="cancelled", error="daemon shut down mid-run")
    now = datetime(2026, 10, 3, 8, 0, tzinfo=LA)
    asyncio.run(p._clock_session(look(now), now))
    assert mems[1][3] == "2026-10-01"


def test_a_runner_that_raises_is_read_from_the_run_it_recorded(tmp_path):
    """A raise is settled as a return is, before the lock is let go: with no
    run recorded the look failed and hands its day on, so a reply recorded
    later in that DM is not taken for it; with one recorded, that run says
    how the look went."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [])
    store.set_meta("clock:listed:U1", "2026-09-30")
    raised = iter([FileNotFoundError("claude"), RuntimeError("after its record")])

    async def memory_turn(task, arrival, now, *, channel, reply_thread, owed, state=None):
        e = next(raised)
        if isinstance(e, RuntimeError):
            store.record_run(kind="agent", task_id=task["id"], session_id="s", started_at=utcnow(),
                             exit_code=0, cost_usd=0.0, status="ok", result_text="Morning.", notified=1)
        raise e
    p.memory_turn = memory_turn
    look = lambda now: clock.morning_wakes(now, {"U1": LOOKS["U1"]}, QUIET, lambda q: None, NAMES.get)[0]  # noqa: E731
    day1 = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    asyncio.run(p._clock_session(look(day1), day1))
    assert store.get_meta("clock:outcome:U1") == "2026-10-01 08:00 failed"
    assert not store.get_meta("clock:trying:U1")
    # fan writes in his DM later that day, and that reply's run is recorded there
    task = store.get_task_by_thread("D-U1", "conversation")
    store.record_run(kind="agent", task_id=task["id"], session_id="r", started_at=utcnow(), exit_code=0,
                     cost_usd=0.0, status="ok", result_text="Sure.", notified=1)
    day2 = datetime(2026, 10, 2, 8, 0, tzinfo=LA)
    asyncio.run(p._clock_session(look(day2), day2))
    assert [m[3] for m in mems] == ["2026-09-30", "2026-09-30"]
    assert store.get_meta("clock:outcome:U1") == "2026-10-02 08:00 spoke"
    assert p._listed("U1", task) == "2026-10-02"
    assert [a[1] for a in alerts] == ["a morning look on 2026-10-01 failed; doctor says whose"]


def test_a_failure_before_the_model_runs_claims_nothing_and_alerts(tmp_path):
    """The alert names no one, and doctor shows whose look it is from that
    minute: the alerts can be read by the person something is kept from."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(fail_first=True), [SPOKE])
    store.set_meta("clock:outcome:U1", "2026-09-30 08:00 spoke")
    now = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    w = clock.morning_wakes(now, {"U1": LOOKS["U1"]}, QUIET, lambda q: store.get_meta(f"clock:morning:{q}"),
                            NAMES.get)[0]
    asyncio.run(p._clock_session(w, now))
    assert turns == [] and store.get_meta("clock:morning:U1") is None
    assert alerts == [("clock", "a morning look could not start on 2026-10-01; doctor says whose")]
    assert store.get_meta("clock:outcome:U1") == "2026-10-01 08:00 could not start"
    running = timedelta(minutes=30)
    assert not clock.look_healthy(store.get_meta("clock:outcome:U1"), now.replace(hour=9), running,
                                  LOOKS["U1"])
    # the next tick tries again, and the claim is in place before the run starts
    asyncio.run(p._clock_session(w, now + timedelta(minutes=1)))
    assert [t[4] for t in turns] == ["2026-10-01"]
    assert store.get_meta("clock:outcome:U1") == "2026-10-01 08:01 spoke"


def test_a_look_refused_before_it_ran_is_released_and_a_failed_one_is_kept(tmp_path):
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [REFUSED, FAILED])
    store.set_meta("clock:morning:U1", "2026-09-30")
    now = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    w = clock.morning_wakes(now, {"U1": LOOKS["U1"]}, QUIET, lambda q: store.get_meta(f"clock:morning:{q}"),
                            NAMES.get)[0]
    asyncio.run(p._clock_session(w, now))
    assert store.get_meta("clock:morning:U1") == "2026-09-30"
    asyncio.run(p._clock_session(w, now))
    assert store.get_meta("clock:morning:U1") == "2026-10-01"
    assert store.get_meta("clock:outcome:U1") == "2026-10-01 08:00 failed"
    assert [a[1] for a in alerts] == ["a morning look on 2026-10-01 was refused before it ran; doctor says whose",
                                      "a morning look on 2026-10-01 failed; doctor says whose"]


def test_a_wake_that_keeps_failing_to_start_goes_behind_the_others(tmp_path):
    """fan's DM cannot be opened: his look fails before its claim every time,
    and mei's look and her reminder still run, at their times."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(refuse={"U1"}), [SILENT, SPOKE])
    looks = clock.mornings(["U1@07:00", "U2@08:00"])
    bins = clock.items("`trajectory:f4a207`  2026-10-01T08:05, today  I undertook to remind mei at "
                       "8:05 to put the bins out\n    involves: me; mei\n    asked by: mei\n")

    async def tick(hh, mm):
        now = datetime(2026, 10, 1, hh, mm, tzinfo=LA)
        wakes = clock.morning_wakes(now, looks, QUIET, lambda q: store.get_meta(f"clock:morning:{q}"), NAMES.get)
        wakes += clock.due_wakes(now, bins, ASKERS, lambda k: bool(store.get_meta(k)), set())
        p._wake(wakes, now)
        await asyncio.gather(*p._bg)

    async def go():
        for hh, mm in ((7, 0), (7, 1), (8, 0), (8, 1), (8, 5), (8, 6)):
            await tick(hh, mm)
    asyncio.run(go())
    assert [(t[0], t[2].split("\n")[2].strip()[:40]) for t in turns] == [
        ("D-U2", "It is Thursday, 08:00, and this is my lo"),
        ("D-U2", "It is Thursday, 08:05, and this has come")]
    assert alerts and all(a[1] == "a morning look could not start on 2026-10-01; doctor says whose"
                          for a in alerts)


def test_names_are_one_spelling_whatever_case_slack_gives(tmp_path):
    """An asker leads to an id in any case, and the clock's frames say the
    name as Slack spells it, as a message's frame does; the look's list is
    asked for by that name whole."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [SPOKE, SILENT])
    named(store, {"U1": "Fan", "U2": "Mei"})
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    assert p.household.askers() == {"fan": "U1", "mei": "U2"}
    capital = clock.items(DUE.replace("asked by: mei", "asked by: Mei").replace("asked by: fan", "asked by: Fan"))
    at5 = datetime(2026, 10, 1, 17, 0, tzinfo=LA)
    wakes = clock.due_wakes(at5, capital, p.household.askers(), lambda k: False, set())
    assert [(w.person, w.key, w.asked) for w in wakes] == [("U2", "clock:due:a24e0d:2026-10-01T17:00:mei", "mei")]
    asyncio.run(p._clock_session(wakes[0], at5))
    eight = datetime(2026, 10, 2, 8, 0, tzinfo=LA)
    look = clock.morning_wakes(eight, clock.mornings(["U1@08:00"]), QUIET, lambda q: None, p.household.told)
    asyncio.run(p._clock_session(look[0], eight))
    assert [t[0] for t in turns] == ["D-U2", "D-U1"] and alerts == []
    assert turns[0][2].startswith("No message started this session. What I say now reaches Mei alone")
    assert turns[1][2] == ("No message started this session. What I say now reaches Fan alone, in a direct "
                           "message.\n\n    It is Friday, 08:00, and this is my look at the day ahead for Fan.")
    assert mems == [("due", "--for=Fan", "--after", "2026-10-01", "--at", "08:00")]
    assert clock.settings_problem(["U1@08:00", "U2@07:30"], "21:30-07:00", p.cfg.slack_owner_user_ids) is None


def test_a_name_beginning_with_a_hyphen_reaches_mem_whole(tmp_path):
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [SILENT])
    named(store, {"U1": "-fan"})
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    eight = datetime(2026, 10, 2, 8, 0, tzinfo=LA)
    [look] = clock.morning_wakes(eight, clock.mornings(["U1@08:00"]), QUIET, lambda q: None, p.household.told)
    asyncio.run(p._clock_session(look, eight))
    assert mems == [("due", "--for=-fan", "--after", "2026-10-01", "--at", "08:00")]


def test_only_ids_let_in_have_looks_and_ask(tmp_path, caplog):
    """An allowed id with no name sessions know, which Slack has not given,
    is not let in: no look for it, a look it did not have is not skipped,
    and no reminder leads to it. Its look is said once a day."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [SILENT, SILENT])
    store._exec("DELETE FROM meta WHERE key='names:U2'")
    p.household = Household.load(store, p.cfg.slack_owner_user_ids)
    assert p.household.told_names() == {"U1": "fan"} and p.household.askers() == {"fan": "U1"}
    morning = datetime(2026, 10, 1, 8, 1, tzinfo=LA)
    with caplog.at_level(logging.WARNING, logger="wanda"):
        let_in = p._looks_let_in(LOOKS, morning)
        p._looks_let_in(LOOKS, morning + timedelta(minutes=1))
    assert list(let_in) == ["U1"]
    assert [r.getMessage() for r in caplog.records] == [
        "clock: no look for U2: not let in until Slack gives a name for them (doctor)"]
    noon = datetime(2026, 10, 1, 12, 1, tzinfo=LA)
    assert clock.missed(noon, p._looks_let_in(LOOKS, noon), lambda q: None) == ["U1"]
    wakes = clock.due_wakes(datetime(2026, 10, 1, 17, 0, tzinfo=LA), clock.items(DUE), p.household.askers(),
                            lambda k: False, set())
    assert [w.asked for w in wakes] == []


def test_a_look_is_kept_under_the_member_id_and_says_the_name(tmp_path):
    """WANDA_MORNINGS gives a member id: the look is opened in that id's DM,
    says the name sessions know the person by, and every record of it is
    kept under the id."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [SPOKE])
    trying = []
    inner = p.memory_turn

    async def memory_turn(task, arrival, now, **kw):
        trying.append(store.get_meta("clock:trying:U1"))
        return await inner(task, arrival, now, **kw)
    p.memory_turn = memory_turn
    now = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    assert clock.mornings(["U1@08:00"]) == {"U1": time(8, 0)}
    [w] = clock.morning_wakes(now, clock.mornings(["U1@08:00"]), QUIET, lambda q: None, p.household.told)
    assert (w.key, w.person) == ("clock:morning:U1", "U1")
    asyncio.run(p._clock_session(w, now))
    assert turns[0][0] == "D-U1"
    assert turns[0][2] == ("No message started this session. What I say now reaches fan alone, in a direct "
                           "message.\n\n    It is Thursday, 08:00, and this is my look at the day ahead for fan.")
    assert mems == [("due", "--for=fan", "--after", "2026-09-30", "--at", "08:00")]
    assert trying == ["2026-10-01 1 0 2026-09-30"]
    assert store.meta_starting("clock:") == {
        "clock:morning:U1": "2026-10-01", "clock:outcome:U1": "2026-10-01 08:00 spoke",
        "clock:trying:U1": "", "clock:listed:U1": "2026-10-01 1 2026-09-30", "clock:marked": "[]"}


def test_the_start_settles_a_look_whose_id_has_left_the_mornings(tmp_path):
    """A look a crash cut short is settled at the next start though its id
    has since been taken out of WANDA_MORNINGS: its run reported, so the
    next look's list starts after its day."""
    store = named(Store(tmp_path / "p.db"))
    p = Processor(settings(tmp_path, mornings=["U2@07:30"]), store, asyncio.Queue(), FakeSlack(),
                  RunnerService("/bin/true"))
    store.create_task(None, "D-U1", "conversation", kind="dm", reply_thread=None)
    store.set_meta("clock:trying:U1", "2026-10-01 1 0 2026-09-30")
    store.record_run(kind="agent", task_id=1, session_id="s", started_at=utcnow(), exit_code=0,
                     cost_usd=0.0, status="ok", result_text="", notified=1)
    p.settle_wakes(datetime(2026, 10, 1, 8, 30, tzinfo=LA))
    assert store.get_meta("clock:trying:U1") == ""
    assert store.get_meta("clock:listed:U1") == "2026-10-01 1 2026-09-30"


# fan's reminder at 7, asked under his name with a capital
FAN_AT7 = """\
`trajectory:b6647b`  2026-10-01T19:00, today  I undertook to remind fan at 7 to pick up the gift for mei
    involves: me; fan; mei
    constrained_by: keep the gift from mei
    asked by: Fan
"""


def test_a_reminder_wakes_the_dm_its_asker_leads_to_and_keeps_their_name(tmp_path, monkeypatch):
    """`asked by: Fan` wakes in the DM of the id `fan` leads to. The wake's
    key and its records keep the name, lower case, as a look's list shows it,
    a wake rebuilt after one was cut short included; the clock's own mark in
    a look's list says it as `mem due` printed it."""
    key = "clock:due:b6647b:2026-10-01T19:00:fan"
    at7 = datetime(2026, 10, 1, 19, 3, tzinfo=LA)
    (tmp_path / "failed").mkdir()
    p, store, turns, alerts, mems = processor(tmp_path / "failed", FakeSlack(), [FAILED], FAN_AT7)
    waking = []
    inner = p.memory_turn

    async def memory_turn(task, arrival, now, **kw):
        waking.append(json.loads(store.get_meta("clock:waking"))[key]["asked"])
        return await inner(task, arrival, now, **kw)
    p.memory_turn = memory_turn
    [w] = asyncio.run(p._due_wakes(at7))
    assert (w.person, w.key, w.asked) == ("U1", key, "fan")
    asyncio.run(p._clock_session(w, at7))
    assert turns[0][0] == "D-U1" and "reaches fan alone" in turns[0][2] and "    asked by: Fan" in turns[0][2]
    assert waking == ["fan"]
    assert [(r["id"], r["asked"]) for r in json.loads(store.get_meta("clock:lost"))] == [("b6647b", "fan")]
    # an answer Slack has not taken is owed under the same name
    (tmp_path / "owed").mkdir()
    p, slack, store = refused_post(tmp_path / "owed", monkeypatch, "It is 7: the gift for mei.")
    p._mem = lambda now, *args: asyncio.sleep(0, FAN_AT7)
    [w] = asyncio.run(p._due_wakes(at7))
    asyncio.run(p._clock_session(w, at7))
    assert [(o["id"], o["asked"]) for o in json.loads(store.get_meta("clock:owed"))] == [("b6647b", "fan")]
    # cut short and released at the start: woken again, told so by name
    (tmp_path / "again").mkdir()
    p, store, turns, alerts, mems = processor(tmp_path / "again", FakeSlack(), [], FAN_AT7)
    store.set_meta("clock:waking", json.dumps({key: {"id": "b6647b", "by": "2026-10-01T19:00", "asked": "fan",
                                                      "task": 1, "last": 0, "before": "", "again": True}}))
    [w] = asyncio.run(p._due_wakes(at7))
    assert (w.person, w.key, w.asked, w.about, w.by) == ("U1", key, "fan", "b6647b", "2026-10-01T19:00")
    assert w.text.endswith("\n    " + clock.AGAIN.format(speaker="fan"))
    # a look at 19:03 marks it as `mem due` printed its asker, and its wake,
    # claimed after the mark, gives what the mark held
    listed = ["Come due for fan after 2026-09-30:"] + FAN_AT7.splitlines()
    marked = clock.still_to_come(listed, at7, ASKERS, lambda k: False)
    assert marked[-1] == "    still to come: the clock gives it to Fan at 19:00, but only while it is open and timed so"
    p._mark(marked, "2026-10-02T02:03:00+00:00")
    assert marks(store) == [("b6647b", "2026-10-01T19:00", "fan")]
    store.set_meta(key, "2026-10-02T02:06:00+00:00")
    p._check_marked(datetime(2026, 10, 1, 19, 10, tzinfo=LA), "")
    assert marks(store) == [] and store.get_meta("clock:lost") is None


def test_the_names_the_clock_wakes_for_are_read_at_each_due_check(tmp_path, caplog):
    """A reminder asked under a name no allowed id has is not woken for, and
    is said once; the names are read again at the next check, which wakes it
    once one leads to an id."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [], AT7.replace("asked by: fan", "asked by: jane"))
    at7 = datetime(2026, 10, 1, 19, 3, tzinfo=LA)
    with caplog.at_level(logging.WARNING, logger="wanda.clock"):
        assert asyncio.run(p._due_wakes(at7)) == []
        assert asyncio.run(p._due_wakes(at7 + timedelta(minutes=1))) == []
    assert [r.getMessage() for r in caplog.records] == [
        "clock: b6647b (2026-10-01T19:00) is not woken for: who asked is not known (jane)"]
    # memory takes U1's change of name to jane
    p.household.advance("U1", "jane", "s-1", datetime.now(timezone.utc))
    assert [(w.person, w.key) for w in asyncio.run(p._due_wakes(at7 + timedelta(minutes=2)))] == [
        ("U1", "clock:due:b6647b:2026-10-01T19:00:jane")]


class Refusing(FakeSlack):
    """A Slack that opens DMs and takes no post."""

    def __init__(self):
        super().__init__()
        self.alerts = []

    async def reply(self, thread_ts, text, channel=None, note=False):
        raise RuntimeError("ratelimited")

    async def alert(self, text):
        self.alerts.append(text)


class Answers:
    """Stands in for claude: every session answers with this."""

    def __init__(self, answer):
        self.agent_sem = asyncio.Semaphore(2)
        self.answer = answer

    async def run(self, prompt, **kw):
        out = {"recalled": [], "answer": self.answer, "recorded": []}
        return RunResult(ok=True, structured=out, result_text=json.dumps(out), session_id="x")


def refused_post(tmp_path, monkeypatch, answer):
    """The daemon's own `memory_turn` with a Slack that opens DMs and takes no
    post, and `mem` standing in."""
    slack = Refusing()
    store = named(Store(tmp_path / "p.db"))
    p = Processor(settings(tmp_path, email_triage=False), store, asyncio.Queue(), slack,
                  RunnerService("/bin/true"))
    p.runner = Answers(answer)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.vault.housekeep", lambda cfg: None)

    async def mem(now, *args):
        return ""
    p._mem = mem
    return p, slack, store


def test_a_look_whose_post_slack_refused_spoke_and_is_delivered_later(tmp_path, monkeypatch):
    """The run stays owed and `memory_turn` returns None, so the look is
    recorded as spoken and nothing is alerted; once delivery gets it through,
    its day moves the next list on."""
    p, slack, store = refused_post(tmp_path, monkeypatch, "Morning. The plumber is at 5.")
    now = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    w = clock.morning_wakes(now, {"U1": LOOKS["U1"]}, QUIET, lambda q: None, NAMES.get)[0]
    asyncio.run(p._clock_session(w, now))
    assert store.get_meta("clock:outcome:U1") == "2026-10-01 08:00 spoke"
    assert [r["result_text"] for r in store.pending_deliveries()] == ["Morning. The plumber is at 5."]
    assert slack.alerts == []
    task = store.get_task_by_thread("D-U1", "conversation")
    assert p._listed("U1", task) == "2026-09-30", "not delivered yet"

    async def takes(thread_ts, text, channel=None, note=False):
        pass
    slack.reply = takes
    asyncio.run(p.deliver_pending())
    assert p._listed("U1", task) == "2026-10-01"


def test_a_look_whose_answer_delivery_gave_up_on_hands_its_day_on(tmp_path, monkeypatch):
    """Delivery gives up after its tries and alerts the run: the person never
    saw the look, so the next look is handed what it was."""
    from wanda.main import MAX_DELIVERY_ATTEMPTS
    p, slack, store = refused_post(tmp_path, monkeypatch, "Morning. The plumber is at 5.")
    now = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    w = clock.morning_wakes(now, {"U1": LOOKS["U1"]}, QUIET, lambda q: None, NAMES.get)[0]
    asyncio.run(p._clock_session(w, now))
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        asyncio.run(p.drain_mail())
    assert store.pending_deliveries() == [] and len(slack.alerts) == 1, slack.alerts
    task = store.get_task_by_thread("D-U1", "conversation")
    assert p._listed("U1", task) == "2026-09-30"


def test_a_timed_wake_whose_answer_delivery_gave_up_on_is_a_reminder_not_given(tmp_path, monkeypatch):
    from wanda.main import MAX_DELIVERY_ATTEMPTS
    p, slack, store = refused_post(tmp_path, monkeypatch, "It is 7: the gift for mei.")
    at7 = datetime(2026, 10, 1, 19, 3, tzinfo=LA)
    w = due_at(at7, {"clock:due:a24e0d:2026-10-01T17:00:mei"})[0]
    asyncio.run(p._clock_session(w, at7))
    asyncio.run(p.drain_mail())
    assert json.loads(store.get_meta("clock:lost") or "[]") == [], "still owed, still tried"
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        asyncio.run(p.drain_mail())
    lost = json.loads(store.get_meta("clock:lost"))
    assert [(r["id"], r["by"], r["asked"], r["why"]) for r in lost] == [
        ("b6647b", "2026-10-01T19:00", "fan", "its answer could not be posted")]
    assert "1 timed reminder(s) not given at their time: trajectory:b6647b due 2026-10-01T19:00." in "".join(
        slack.alerts), slack.alerts


class Turns:
    """Stands in for claude: a session whose turns gave these answers, in
    order, each kept in its transcript as Claude Code keeps a turn's
    structured output, and whose last turn ended with its answer, or ran out
    of time (`timed_out`)."""

    def __init__(self, answers, timed_out=False):
        self.agent_sem = asyncio.Semaphore(2)
        self.answers, self.timed_out = answers, timed_out

    async def run(self, prompt, *, session_id, cwd, **kw):
        tx = vault.transcripts_dir(Path(cwd))
        tx.mkdir(parents=True, exist_ok=True)
        (tx / f"{session_id}.jsonl").write_text("".join(
            json.dumps({"type": "attachment", "attachment": {"type": "structured_output", "data": {
                "recalled": [], "answer": a, "recorded": []}}}) + "\n" for a in self.answers))
        if self.timed_out:
            return RunResult(ok=False, timed_out=True, error="claude ran past 420 s", session_id=session_id)
        out = {"recalled": [], "answer": self.answers[-1], "recorded": []}
        return RunResult(ok=True, structured=out, result_text=json.dumps(out), session_id=session_id)


class Posts(FakeSlack):
    """A Slack that opens DMs and takes every post."""

    def __init__(self):
        super().__init__()
        self.posts = []

    async def reply(self, thread_ts, text, channel=None, note=False):
        self.posts.append((channel, text, note))


@pytest.mark.parametrize("answers,timed_out,posted,outcome,alert", [
    # a turn begun after the answer, as by a background command's end, said nothing
    (["Morning. The plumber is at 5.", ""], False, "Morning. The plumber is at 5.", "spoke", None),
    # a turn after the answer ran out of time
    (["Morning. The plumber is at 5.", ""], True, "Morning. The plumber is at 5.", "spoke, then failed",
     "a morning look on 2026-10-01 failed after it spoke; doctor says whose"),
    # no turn said anything, and the last ran out of time
    ([""], True, None, "failed", "a morning look on 2026-10-01 failed; doctor says whose"),
])
def test_a_clock_session_posts_the_last_answer_it_gave_that_says_something(tmp_path, monkeypatch, answers,
                                                                          timed_out, posted, outcome, alert):
    """Through the daemon's own `memory_turn`: Claude Code prints the last turn's
    result alone, and the transcript keeps every turn's answer. The last
    that says something is posted and the look spoke, though a later turn
    said nothing or failed; the failure is alerted, as any clock session's
    is, and doctor says whose. With none said, a failure posts nothing."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    slack = Posts()
    store = named(Store(tmp_path / "p.db"))
    p = Processor(settings(tmp_path, email_triage=False), store, asyncio.Queue(), slack,
                  RunnerService("/bin/true"))
    p.runner = Turns(answers, timed_out)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)

    async def mem(now, *args):
        return ""
    p._mem = mem
    now = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    asyncio.run(p._clock_session(clock.morning_wakes(now, {"U1": LOOKS["U1"]}, QUIET, lambda q: None,
                                                     NAMES.get)[0], now))
    assert slack.posts == ([("D-U1", posted, False)] if posted else [])
    run = store.run(1)
    assert (run["status"], run["result_text"]) == (("ok", posted) if posted else ("timeout", ""))
    assert store.get_meta("clock:outcome:U1") == f"2026-10-01 08:00 {outcome}"
    assert clock.look_healthy(store.get_meta("clock:outcome:U1"), now, timedelta(minutes=30),
                              LOOKS["U1"]) is bool(posted)
    assert slack.alerts == ([alert] if alert else [])
    task = store.get_task_by_thread("D-U1", "conversation")
    # spoken, its day starts the next look's list; failed, none has yet
    assert p._listed("U1", task) == ("2026-10-01" if posted else None)


@pytest.mark.parametrize("posted", [True, False], ids=["posted", "post refused"])
def test_a_timed_wake_that_gave_its_reminder_and_then_failed_gave_it(tmp_path, monkeypatch, posted):
    """The reminder is posted, not kept as one not given, and the failure is
    alerted by the reminder's id and time. An answer Slack has not taken yet
    is owed, and the alert says it is being tried again rather than given."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    slack = Posts() if posted else Refusing()
    store = named(Store(tmp_path / "p.db"))
    p = Processor(settings(tmp_path, email_triage=False), store, asyncio.Queue(), slack,
                  RunnerService("/bin/true"))
    p.runner = Turns(["It is 7: the gift for mei.", ""], timed_out=True)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    at7 = datetime(2026, 10, 1, 19, 3, tzinfo=LA)
    w = due_at(at7, {"clock:due:a24e0d:2026-10-01T17:00:mei"})[0]
    asyncio.run(p._clock_session(w, at7))
    asyncio.run(p._flush_lost())
    assert store.get_meta("clock:lost") is None and json.loads(store.get_meta("clock:waking")) == {}
    if posted:
        assert slack.posts == [("D-U1", "It is 7: the gift for mei.", False)]
        assert slack.alerts == ["the reminder trajectory:b6647b due 2026-10-01T19:00 was given, and its "
                                "session then failed"]
    else:
        assert [o["id"] for o in json.loads(store.get_meta("clock:owed"))] == ["b6647b"]
        assert slack.alerts == ["the reminder trajectory:b6647b due 2026-10-01T19:00 was answered and its post "
                                "is being tried again; its session then failed"]


def test_a_clock_alert_slack_refused_is_sent_by_the_next_drain(tmp_path):
    class Flaky(FakeSlack):
        def __init__(self):
            super().__init__()
            self.up, self.alerts = False, []

        async def alert(self, text):
            if not self.up:
                raise RuntimeError("ratelimited")
            self.alerts.append(text)

    slack = Flaky()
    store = named(Store(tmp_path / "p.db"))
    p = Processor(settings(tmp_path, email_triage=False), store, asyncio.Queue(), slack,
                  RunnerService("/bin/true"))
    asyncio.run(p._alert_once("clock", "a morning look on 2026-10-01 failed; doctor says whose"))
    assert slack.alerts == [] and store.get_meta("clock_alert_pending")
    slack.up = True
    asyncio.run(p.drain_mail())
    assert slack.alerts == ["a morning look on 2026-10-01 failed; doctor says whose"]


def test_a_failed_timed_wake_names_the_reminder_it_loses(tmp_path, caplog):
    """Kept in the run store and named by its own kind of alert, by id and
    time, never what it is about or who asked, which can be kept from whoever
    reads the alerts; another clock alert that day does not hold it back."""
    slack = FakeSlack()
    p, store, turns, alerts, mems = processor(tmp_path, slack, [FAILED])
    store.set_meta("clock_alert_date", datetime.now(timezone.utc).date().isoformat())
    at7 = datetime(2026, 10, 1, 19, 3, tzinfo=LA)
    w = due_at(at7, {"clock:due:a24e0d:2026-10-01T17:00:mei"})[0]
    with caplog.at_level(logging.WARNING, logger="wanda"):
        asyncio.run(p._clock_session(w, at7))
    assert alerts == []
    assert ("clock: clock:due:b6647b:2026-10-01T19:00:fan failed, and the reminder is not tried again"
            in [r.getMessage() for r in caplog.records])
    assert store.get_meta(w.key), "claimed, so not woken again"
    asyncio.run(p._flush_lost())
    assert slack.alerts == ["1 timed reminder(s) not given at their time: trajectory:b6647b due "
                            "2026-10-01T19:00. Who asked, why, and how to give one later are in doctor."]


# fan's reminder at 7, as `mem due` prints it while it is open at that time
AT7 = """\
`trajectory:b6647b`  2026-10-01T19:00, today  I undertook to remind fan at 7 to pick up the gift for mei
    involves: me; fan; mei
    constrained_by: keep the gift from mei
    asked by: fan
"""


class Held:
    """Stands in for claude, Slack's post and the snapshot, any one of them
    held until the session is stopped, so that a stop or a crash comes at
    that step; `reached` is set once the wake is there."""

    def __init__(self, step):
        self.step, self.reached = step, asyncio.Event()
        self.agent_sem = asyncio.Semaphore(1)
        self.posted, self.gate = [], threading.Event()

    async def run(self, prompt, **kw):
        if self.step == "model":
            self.reached.set()
            await asyncio.Event().wait()
        out = {"recalled": [], "answer": "It is 7: the gift for mei.", "recorded": []}
        return RunResult(ok=True, structured=out, result_text=json.dumps(out), session_id="x")

    async def dm_channel(self, user):
        return f"D-{user}"

    async def channel_type(self, channel):
        return "im"

    async def alert(self, text):
        self.posted.append(("alert", text))

    async def reply(self, thread_ts, text, channel=None, note=False):
        if self.step == "post":
            self.reached.set()
            await asyncio.Event().wait()
        self.posted.append((channel, text))

    def snapshot(self, cfg, message):
        if self.step == "snapshot":
            self.reached.set()
            self.gate.wait(10)


def cut_short(tmp_path, monkeypatch, step, ending, look=False) -> Path:
    """fan's 19:00 reminder woken at 19:03, or his 08:00 look, through
    the daemon's own `memory_turn`, then stopped as `shutdown` stops it, or
    abandoned as a crash leaves it, at `step`: waiting for his conversation's
    lock, for the one session's place, while the model runs, with its answer
    recorded and not yet posted, or posted, while the snapshot waits its turn.
    Returns the run store as the next start finds it."""
    held = Held(step)
    store = named(Store(tmp_path / "p.db"))
    p = Processor(settings(tmp_path, email_triage=False), store, asyncio.Queue(), held, held)
    monkeypatch.setattr("wanda.vault.snapshot", held.snapshot)
    store.create_task(None, "D-U1", "conversation", kind="dm", reply_thread=None)
    task = store.get_task_by_thread("D-U1", "conversation")
    if look:
        at = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
        w = clock.morning_wakes(at, {"U1": LOOKS["U1"]}, QUIET, lambda q: None, NAMES.get)[0]
        store.set_meta("clock:listed:U1", "2026-09-30")

        async def mem(now, *args):
            return ""
        p._mem = mem
    else:
        at = datetime(2026, 10, 1, 19, 3, tzinfo=LA)
        w = due_at(at, {"clock:due:a24e0d:2026-10-01T17:00:mei"})[0]
    found = tmp_path / "found.db"

    async def go():
        lock = p._task_locks.setdefault(task["id"], asyncio.Lock())
        if step == "lock":
            await lock.acquire()  # a reply in fan's DM, running
        if step == "place":
            await held.agent_sem.acquire()  # a session in another conversation
        p._wake([w], at)
        if step in ("lock", "place"):
            for _ in range(500):
                if store.get_meta(w.key):
                    break  # claimed, and waiting for the lock or the place
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
        else:
            await asyncio.wait_for(held.reached.wait(), 5)
        try:
            if ending == "crash":
                # the store as a process that ends without unwinding leaves it
                with sqlite3.connect(found) as dest:
                    store._db.backup(dest)
            await p.shutdown(grace_s=2)
        finally:
            held.gate.set()
    asyncio.run(go())
    if ending == "stop":
        with sqlite3.connect(found) as dest:
            store._db.backup(dest)
    store.close()
    return found


def started_again(found: Path, tmp_path: Path, at: datetime, due: str = AT7):
    """The next start at `at`, as run_daemon makes it: the wakes settled
    before Slack connects, then the first due check and the mail loop's
    pass, which names a reminder not given."""
    d = tmp_path / f"at-{at:%H%M}"
    d.mkdir()
    store = Store(d / "p.db")
    with sqlite3.connect(found) as src:
        src.backup(store._db)
    slack = FakeSlack()
    p = Processor(settings(d, email_triage=False), store, asyncio.Queue(), slack, RunnerService("/bin/true"))

    async def mem(now, *args):
        return due
    p._mem = mem
    p.settle_wakes(at)
    wakes = asyncio.run(p._due_wakes(at))
    asyncio.run(p._flush_lost())
    lost = [(r["id"], r["why"]) for r in json.loads(store.get_meta("clock:lost") or "[]")]
    owed = [o["id"] for o in json.loads(store.get_meta("clock:owed") or "[]")]
    return [w.key for w in wakes], lost, owed, slack.alerts, store, [w.text for w in wakes]


@pytest.mark.parametrize("ending", ["stop", "crash"])
@pytest.mark.parametrize("step", ["lock", "place", "model", "post", "snapshot"])
def test_a_timed_wake_cut_short_after_its_claim_is_settled_at_the_next_start(tmp_path, monkeypatch, step,
                                                                              ending):
    """A stop, as every upgrade makes, or a crash, at each step after a timed
    wake's claim. At the next start one that posted nothing is woken again
    while its time is no more than two hours gone, and is kept and named as
    a reminder not given after that; one whose answer was recorded and not
    posted is followed until delivery gets it through; one posted was given."""
    found = cut_short(tmp_path, monkeypatch, step, ending)
    key = "clock:due:b6647b:2026-10-01T19:00:fan"
    soon, late = (started_again(found, tmp_path, datetime(2026, 10, 1, *hm, tzinfo=LA))
                  for hm in ((19, 10), (21, 30)))
    if step in ("lock", "place", "model"):
        assert soon[:3] == ([key], [], []), soon
        # and the session woken again is told that nothing reached him
        assert soon[5][0].endswith("\n        asked by: fan\n    A session I began for this earlier was cut "
                                   "short before anything reached fan."), soon[5]
        assert late[:3] == ([], [("b6647b", "its session was cut short, and the daemon was back more than "
                                            "2 h after its time")], []), late
        assert late[3] == ["1 timed reminder(s) not given at their time: trajectory:b6647b due "
                           "2026-10-01T19:00. Who asked, why, and how to give one later are in doctor."]
        # released, and marked so until a due check wakes it or finds it gone
        assert json.loads(soon[4].get_meta("clock:waking")) == {key: {
            "id": "b6647b", "by": "2026-10-01T19:00", "asked": "fan", "task": 1,
            "last": None if step == "lock" else 0, "before": "", "again": True}}
        assert json.loads(late[4].get_meta("clock:waking")) == {}
    elif step == "post":
        for back in (soon, late):
            assert back[:4] == ([], [], ["b6647b"], []), back
            assert [r["result_text"] for r in back[4].pending_deliveries()] == ["It is 7: the gift for mei."]
            assert json.loads(back[4].get_meta("clock:waking")) == {}
    else:
        for back in (soon, late):
            assert back[:4] == ([], [], [], []), back
            assert back[4].pending_deliveries() == []
            assert json.loads(back[4].get_meta("clock:waking")) == {}


def test_the_daemon_settles_a_wake_cut_short_before_slack_connects(tmp_path, monkeypatch):
    """The start settles each wake a stop or a crash cut short, and each look
    a crash cut short, before Slack connects, so that no session has run in
    its DM since: here a wake whose time is long gone, kept as a reminder not
    given, and a look whose run reported, which moves the next list on, by
    the time Slack would connect."""
    seen = []

    async def one_pass(self):
        os.kill(os.getpid(), signal.SIGTERM)

    async def no_clock(self):
        pass

    monkeypatch.setattr("wanda.main.SlackWatcher.start",
                        lambda self: seen.append((self.store.get_meta("clock:lost"),
                                                  self.store.get_meta("clock:trying:U2"),
                                                  self.store.get_meta("clock:listed:U2"))))
    monkeypatch.setattr("wanda.main.SlackWatcher.stop", lambda self: None)
    monkeypatch.setattr("wanda.main.Processor.loop", one_pass)
    monkeypatch.setattr("wanda.main.Processor.clock_loop", no_clock)
    monkeypatch.setattr("wanda.vault.prepare", lambda c, now, since=None: None)
    monkeypatch.setattr("wanda.vault.snapshot", lambda cfg, message: None)
    monkeypatch.setattr("wanda.vault.last_snapshot", lambda cfg: "none")
    monkeypatch.setattr("wanda.main.acquire_lock", lambda path: None)

    async def user_now(self, uid):
        return {"id": uid, "profile": {"display_name": NAMES[uid]}}
    monkeypatch.setattr("wanda.actions.slack.SlackActions.user_now", user_now)
    cfg = settings(tmp_path, slack_bot_token="xoxb-x", slack_app_token="xapp-x", alert_channel="C9",
                   email_triage=False, claude_bin="/bin/true")
    store = named(Store(cfg.db_path))
    store.create_task(None, "D-U1", "conversation", kind="dm", reply_thread=None)
    store.set_meta("clock:due:b6647b:2026-01-01T19:00:fan", "2026-01-02T03:03:00+00:00")
    store.set_meta("clock:waking", json.dumps({"clock:due:b6647b:2026-01-01T19:00:fan": {
        "id": "b6647b", "by": "2026-01-01T19:00", "asked": "fan", "task": 1, "last": 0, "before": ""}}))
    # and mei's look, which a crash cut short after its run reported
    store.create_task(None, "D-U2", "conversation", kind="dm", reply_thread=None)
    store.set_meta("clock:trying:U2", "2026-01-02 2 0 2026-01-01")
    store.record_run(kind="agent", task_id=2, session_id="s", started_at=utcnow(), exit_code=0,
                     cost_usd=0.0, status="ok", result_text="", notified=1)
    store.close()
    asyncio.run(run_daemon(cfg))
    assert [([(r["id"], r["why"]) for r in json.loads(s[0])], s[1], s[2]) for s in seen] == [([(
        "b6647b", "its session was cut short, and the daemon was back more than 2 h after its time")],
        "", "2026-01-02 1 2026-01-01")]


def test_a_wake_cut_short_and_closed_meanwhile_is_a_reminder_not_given(tmp_path, monkeypatch):
    """Released at the start to be woken again, it is not open at that time
    when the clock looks, as when the session cut short closed it: it was
    not given, and is kept and named."""
    found = cut_short(tmp_path, monkeypatch, "model", "crash")
    wakes, lost, owed, alerts, store, _ = started_again(found, tmp_path,
                                                        datetime(2026, 10, 1, 19, 10, tzinfo=LA), due="")
    assert (wakes, owed) == ([], [])
    assert lost == [("b6647b", "its session was cut short, and it was not open at that time when the clock "
                                "looked again")]
    assert json.loads(store.get_meta("clock:waking")) == {}


def test_a_wake_woken_again_and_refused_before_it_ran_is_still_told_it_was_cut_short(tmp_path):
    """Released at the start to be woken again, the wake's frame says that a
    session for it was cut short; the budget refuses that wake before the
    model runs, and the one after the refusal still says so."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [REFUSED, SPOKE], AT7)
    store.create_task(None, "D-U1", "conversation", kind="dm", reply_thread=None)
    key = "clock:due:b6647b:2026-10-01T19:00:fan"
    # as a stop leaves a wake cut short while the model ran
    store.set_meta(key, "2026-10-02T02:03:00+00:00")
    store.set_meta("clock:waking", json.dumps({key: {"id": "b6647b", "by": "2026-10-01T19:00", "asked": "fan",
                                                      "task": 1, "last": 0, "before": ""}}))
    p.settle_wakes(datetime(2026, 10, 1, 19, 10, tzinfo=LA))
    for mm in (10, 15):
        now = datetime(2026, 10, 1, 19, mm, tzinfo=LA)
        wakes = asyncio.run(p._due_wakes(now))
        assert [w.key for w in wakes] == [key], mm
        asyncio.run(p._clock_session(wakes[0], now))
    assert [t[2].endswith(clock.AGAIN.format(speaker="fan")) for t in turns] == [True, True]
    assert store.get_meta(key) and json.loads(store.get_meta("clock:waking")) == {}
    assert store.get_meta("clock:lost") is None
    assert [a[1] for a in alerts] == ["the reminder trajectory:b6647b due 2026-10-01T19:00 was refused before "
                                      "it ran; it is tried again until two hours after that time"]


@pytest.mark.parametrize("step", ["place", "model", "post", "snapshot"])
def test_a_look_a_crash_cut_short_is_settled_at_the_next_start(tmp_path, monkeypatch, step):
    """fan's 08:00 look abandoned without unwinding at each step after it
    takes his conversation's lock, and started again at 08:30: settled before
    Slack connects, from the look's own run or none, so a reply of fan's that
    runs in his DM before the next look is never taken for the look's run.
    Its day is handed on unless its run reported and its answer reached him."""
    found = cut_short(tmp_path, monkeypatch, step, "crash", look=True)
    with sqlite3.connect(found) as db:
        trying = db.execute("SELECT value FROM meta WHERE key='clock:trying:U1'").fetchone()[0]
    assert trying == "2026-10-01 1 0 2026-09-30", trying
    d = tmp_path / "again"
    d.mkdir()
    store = Store(d / "p.db")
    with sqlite3.connect(found) as src:
        src.backup(store._db)
    p = Processor(settings(d, email_triage=False), store, asyncio.Queue(), FakeSlack(), RunnerService("/bin/true"))
    p.settle_wakes(datetime(2026, 10, 1, 8, 30, tzinfo=LA))
    assert not store.get_meta("clock:trying:U1")
    reported = step in ("post", "snapshot")
    assert store.get_meta("clock:listed:U1") == ("2026-10-01 1 2026-09-30" if reported else "2026-09-30")
    # fan writes in his DM later that morning, and that reply's run is recorded there
    store.record_run(kind="agent", task_id=1, session_id="r", started_at=utcnow(), exit_code=0,
                     cost_usd=0.0, status="ok", result_text="Sure.", notified=1)
    mems = []

    async def mem(now, *args):
        mems.append(args)
        return ""

    async def memory_turn(task, arrival, now, *, channel, reply_thread, owed, state=None):
        store.record_run(kind="agent", task_id=task["id"], session_id="s", started_at=utcnow(), exit_code=0,
                         cost_usd=0.0, status="ok", result_text="", notified=1)
    p._mem, p.memory_turn = mem, memory_turn
    nxt = datetime(2026, 10, 2, 8, 0, tzinfo=LA)
    asyncio.run(p._clock_session(clock.morning_wakes(nxt, {"U1": LOOKS["U1"]}, QUIET, lambda q: None,
                                                     NAMES.get)[0], nxt))
    # only a look whose answer was posted moves the start on; one recorded
    # and not yet posted hands its day on until delivery gets it through
    assert mems[0][3] == ("2026-10-01" if step == "snapshot" else "2026-09-30"), mems


def marks(store) -> list[tuple[str, str, str]]:
    """What the run store holds as marked still to come: each one's id, time
    and who asked, without when the look marked it."""
    return [(m["id"], m["by"], m["asked"]) for m in json.loads(store.get_meta("clock:marked") or "[]")]


def test_a_reminder_marked_still_to_come_and_taken_away_before_its_time_is_not_given(tmp_path):
    """fan's 08:00 look is handed the 17:00 plumber reminder mei asked for,
    marked as still to come. Closed or re-dated before the clock gives it, by
    the look or anything after it, it is kept and named as a reminder not
    given, the reason saying whether it was moved to another time that day,
    which the clock gives then, or closed or moved off the day's times; left
    open, it is held until it is woken, and nothing is kept."""
    listed = ("Come due for fan after 2026-09-30:\n"
              "`trajectory:a24e0d`  2026-10-01T17:00, today  I undertook to remind fan at 5 to call the plumber\n"
              "    involves: me; fan\n    asked by: mei\n"
              "    still to come: the clock gives it to mei at 17:00, but only while it is open and timed so\n"
              "`trajectory:9cdc0d`  2026-10-01T18:00, today  the meeting at 6pm\n    involves: fan\n")
    at5 = "`trajectory:a24e0d`  2026-10-01T17:00, today  I undertook to remind fan at 5 to call the plumber\n" \
          "    involves: me; fan\n    asked by: mei\n"
    off = ("it was closed, or re-dated to another day or to no time of day, before the clock gave it, after a "
           "morning look listed it as still to come")
    moved = ("it was re-dated to 18:00 the same day, after a morning look listed it as still to come; the clock "
             "gives it at that time instead")
    claim = "clock:due:a24e0d:2026-10-01T17:00:mei"
    for case, due in (("closed", ""), ("re-dated", at5.replace("T17:00", "T18:00")),
                      ("no time", at5.replace("T17:00", "")), ("open", at5), ("given", "")):
        d = tmp_path / case.replace(" ", "-")
        d.mkdir()
        slack = FakeSlack()
        p, store, turns, alerts, mems = processor(d, slack, [SILENT], listed)
        eight = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
        asyncio.run(p._clock_session(clock.morning_wakes(eight, {"U1": LOOKS["U1"]}, QUIET,
                                                         lambda q: None, NAMES.get)[0], eight))
        assert marks(store) == [("a24e0d", "2026-10-01T17:00", "mei")], case
        if case == "given":
            # woken and claimed, then closed by the session that gave it
            store.set_meta(claim, utcnow())

        async def mem(now, *args, due=due):
            return due
        p._mem = mem
        # before its time it is left alone, whatever the store says of it
        assert asyncio.run(p._due_wakes(datetime(2026, 10, 1, 16, 55, tzinfo=LA))) == []
        assert store.get_meta("clock:lost") is None and len(json.loads(store.get_meta("clock:marked"))) == 1
        wakes = asyncio.run(p._due_wakes(datetime(2026, 10, 1, 17, 2, tzinfo=LA)))
        lost = [(r["id"], r["by"], r["asked"], r["why"], r["moved"])
                for r in json.loads(store.get_meta("clock:lost") or "[]")]
        if case == "open":
            # its wake waits: the mark is held until the clock wakes for it
            assert lost == [] and [w.key for w in wakes] == [claim], case
            assert len(json.loads(store.get_meta("clock:marked"))) == 1, case
            store.set_meta(claim, utcnow())
            asyncio.run(p._due_wakes(datetime(2026, 10, 1, 17, 7, tzinfo=LA)))
        assert marks(store) == [], case
        if case in ("open", "given"):
            assert lost == [] and store.get_meta("clock:lost") is None, case
            continue
        why, to = (moved, "2026-10-01T18:00") if case == "re-dated" else (off, "")
        assert (wakes, lost) == ([], [("a24e0d", "2026-10-01T17:00", "mei", why, to)]), case
        asyncio.run(p._flush_lost())
        assert slack.alerts == ["1 timed reminder(s) not given at their time: trajectory:a24e0d due "
                                "2026-10-01T17:00. Who asked, why, and how to give one later are in doctor."]


PLUMBER = ("Come due for fan after 2026-09-30:\n"
           "`trajectory:a24e0d`  2026-10-01T17:00, today  I undertook to remind fan at 5 to call the plumber\n"
           "    involves: me; fan\n    asked by: mei\n"
           "    still to come: the clock gives it to mei at 17:00, but only while it is open and timed so\n")
OFF = ("it was closed, or re-dated to another day or to no time of day, before the clock gave it, after a "
       "morning look listed it as still to come")


def marked_at_eight(d):
    """fan's 08:00 look, handed mei's 17:00 plumber reminder marked."""
    d.mkdir()
    p, store, turns, alerts, mems = processor(d, FakeSlack(), [SILENT], PLUMBER)
    eight = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    asyncio.run(p._clock_session(clock.morning_wakes(eight, {"U1": LOOKS["U1"]}, QUIET, lambda q: None, NAMES.get)[0],
                                 eight))
    assert marks(store) == [("a24e0d", "2026-10-01T17:00", "mei")]
    return p, store


def lost_in(store):
    return [(r["id"], r["by"], r["asked"], r["why"], r["moved"])
            for r in json.loads(store.get_meta("clock:lost") or "[]")]


def test_a_marked_reminder_the_clock_gave_at_another_time_that_day_was_given(tmp_path):
    """Moved by the person to an earlier time that day, given there by the
    clock and closed, a marked reminder is not kept as one not given; one the
    clock gave before the look marked it, then re-dated to later that day and
    closed by the look, still is."""
    for case in ("moved earlier and given", "given before the look"):
        p, store = marked_at_eight(tmp_path / case.replace(" ", "-"))
        if case == "moved earlier and given":
            # moved to 10:00 in a DM after the look, and woken there
            store.set_meta("clock:due:a24e0d:2026-10-01T10:00:mei", utcnow())
        else:
            store.set_meta("clock:due:a24e0d:2026-10-01T07:00:mei", "2026-10-01T14:00:00+00:00")
        p._mem = lambda now, *args: asyncio.sleep(0, "")  # closed
        asyncio.run(p._due_wakes(datetime(2026, 10, 1, 17, 2, tzinfo=LA)))
        assert marks(store) == [], case
        assert lost_in(store) == ([] if case == "moved earlier and given" else
                                  [("a24e0d", "2026-10-01T17:00", "mei", OFF, "")]), case


def test_a_mark_held_for_its_wake_is_let_go_once_its_time_is_two_hours_gone(tmp_path):
    """Open at its time, a marked reminder is held while its wake waits; two
    hours after its time the due check keeps it as noticed too late, and the
    mark goes, the reminder kept once. Re-dated to another day at a time, it
    is not read as moved within its own day."""
    p, store = marked_at_eight(tmp_path / "held")
    open_at5 = "\n".join(PLUMBER.splitlines()[1:4]) + "\n"
    p._mem = lambda now, *args: asyncio.sleep(0, open_at5)
    for hh, mm in ((17, 2), (18, 59)):
        asyncio.run(p._due_wakes(datetime(2026, 10, 1, hh, mm, tzinfo=LA)))
        assert marks(store) == [("a24e0d", "2026-10-01T17:00", "mei")] and lost_in(store) == [], (hh, mm)
    asyncio.run(p._due_wakes(datetime(2026, 10, 1, 19, 1, tzinfo=LA)))
    assert marks(store) == []
    assert lost_in(store) == [("a24e0d", "2026-10-01T17:00", "mei",
                               "its time was more than 2 h gone when the clock saw it", "")]
    p, store = marked_at_eight(tmp_path / "another-day")
    p._check_marked(datetime(2026, 10, 1, 17, 2, tzinfo=LA), open_at5.replace("2026-10-01T17:00", "2026-09-30T17:00"))
    assert marks(store) == [] and lost_in(store) == [("a24e0d", "2026-10-01T17:00", "mei", OFF, "")]


def test_a_marked_reminder_moved_a_little_earlier_is_held_for_the_wake_that_check_finds(tmp_path):
    """Moved in a DM to 16:58 after the look marked it for 17:00, with no due
    check between the two times: the first check after 17:00 is the one that
    finds the 16:58 wake, and it runs before that wake starts. The mark is
    held for the wake's claim, which lets it go, and nothing is kept; never
    woken, it is kept once, at its new time, when that is two hours gone."""
    at458 = "\n".join(PLUMBER.splitlines()[1:4]).replace("T17:00", "T16:58") + "\n"
    key = "clock:due:a24e0d:2026-10-01T16:58:mei"
    for case in ("woken", "never woken"):
        p, store = marked_at_eight(tmp_path / case.replace(" ", "-"))
        p._mem = lambda now, *args: asyncio.sleep(0, at458)
        wakes = asyncio.run(p._due_wakes(datetime(2026, 10, 1, 17, 2, tzinfo=LA)))
        assert [w.key for w in wakes] == [key], case
        assert marks(store) == [("a24e0d", "2026-10-01T17:00", "mei")] and lost_in(store) == [], case
        if case == "woken":
            store.set_meta(key, utcnow())
            p._mem = lambda now, *args: asyncio.sleep(0, "")  # closed by the session that gave it
            asyncio.run(p._due_wakes(datetime(2026, 10, 1, 17, 7, tzinfo=LA)))
            assert marks(store) == [] and lost_in(store) == [], case
            continue
        asyncio.run(p._due_wakes(datetime(2026, 10, 1, 18, 57, tzinfo=LA)))
        assert marks(store) == [("a24e0d", "2026-10-01T17:00", "mei")] and lost_in(store) == [], case
        asyncio.run(p._due_wakes(datetime(2026, 10, 1, 18, 59, tzinfo=LA)))
        assert marks(store) == [] and lost_in(store) == [(
            "a24e0d", "2026-10-01T16:58", "mei", "its time was more than 2 h gone when the clock saw it", "")]


def test_a_look_refused_before_it_ran_leaves_no_marks(tmp_path):
    """Refused by the budget before the model ran, the look reached no one:
    the reminder it would have marked is not held, so closing it later is
    not a reminder not given; a mark an earlier look made stays, and the
    look that runs after the refusal marks it."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [REFUSED, SILENT], PLUMBER)
    earlier = {"id": "596f2d", "by": "2026-10-01T09:30", "asked": "fan", "at": "2026-10-01T14:00:00+00:00"}
    store.set_meta("clock:marked", json.dumps([earlier]))
    eight = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    w = clock.morning_wakes(eight, {"U1": LOOKS["U1"]}, QUIET, lambda q: None, NAMES.get)[0]
    asyncio.run(p._clock_session(w, eight))
    assert not store.get_meta("clock:morning:U1"), "released"
    assert marks(store) == [("596f2d", "2026-10-01T09:30", "fan")]
    asyncio.run(p._clock_session(w, eight + timedelta(minutes=1)))
    assert marks(store) == [("596f2d", "2026-10-01T09:30", "fan"), ("a24e0d", "2026-10-01T17:00", "mei")]


def test_a_look_after_a_reminders_time_marks_it_while_its_wake_waits(tmp_path):
    """fan is in a conversation in his DM from 07:57 to 08:06, so his 08:00
    look and the wake for mei's 08:00 reminder about him both wait for it, and
    the look starts first. Its list marks the reminder as still to come, as it
    does one later that day, since the clock is still to give it; one already
    woken, one more than two hours gone, one not hers and one at a time no
    clock shows (T24:00, written by hand) are not marked, and the look runs. The
    mark is held while the wake waits, through a due check that finds the
    reminder still open, so a look that closes it afterwards has it kept as
    a reminder not given; given by its wake, nothing is kept."""
    tablet = ("`trajectory:6ec742`  2026-10-01T08:00, today  I undertook to remind mei at 8 to give fan his tablet\n"
              "    involves: me; mei; fan\n    asked by: mei\n")
    listed = ("Come due for fan after 2026-09-30:\n"
              "`trajectory:596f2d`  2026-10-01T09:30, today  I undertook to remind fan at 9:30 to ring the bank\n"
              "    involves: me; fan\n    asked by: fan\n"
              "    still to come: the clock gives it to fan at 09:30, but only while it is open and timed so\n"
              + tablet +
              "`trajectory:41ab07`  2026-10-01T07:45, today  I undertook to remind fan at 7:45 to take his pill\n"
              "    involves: me; fan\n    asked by: fan\n"
              "`trajectory:0d2c11`  2026-10-01T05:30, today  I undertook to remind fan at 5:30 to catch the train\n"
              "    involves: me; fan\n    asked by: fan\n"
              "`trajectory:94f6cf`  2026-10-01T24:00, today  I undertook to remind fan at midnight to lock the shed\n"
              "    involves: me; fan\n    asked by: fan\n"
              "`trajectory:9cdc0d`  2026-10-01T07:00, today  breakfast meeting at 7\n    involves: fan\n")
    mark = "    still to come: the clock gives it to mei at 08:00, but only while it is open and timed so"
    # what due.rs gave, and the same with the clock's own mark
    six = datetime(2026, 10, 1, 8, 6, tzinfo=LA)
    fired = {"clock:due:41ab07:2026-10-01T07:45:fan"}
    got = clock.still_to_come(listed.splitlines(), six, ASKERS, lambda k: k in fired)
    assert got == listed.replace(tablet, tablet + mark + "\n").splitlines(), got
    assert clock.still_to_come(got, six, ASKERS, lambda k: k in fired) == got, "marked once"
    for case in ("closed", "given"):
        d = tmp_path / case
        d.mkdir()
        slack = FakeSlack()
        p, store, turns, alerts, mems = processor(d, slack, [SILENT], listed)
        store.set_meta("clock:due:41ab07:2026-10-01T07:45:fan", "2026-10-01T14:45:00+00:00")
        asyncio.run(p._clock_session(clock.morning_wakes(six, {"U1": LOOKS["U1"]}, QUIET,
                                                         lambda q: None, NAMES.get)[0], six))
        assert mems[0][-1] == "08:06" and mark in turns[0][2], turns[0][2]
        assert marks(store) == [("596f2d", "2026-10-01T09:30", "fan"),
                                ("6ec742", "2026-10-01T08:00", "mei")], case

        async def mem(now, *args, due=tablet):
            return due
        p._mem = mem
        # a due check while the look runs: still open, its wake waiting
        wakes = asyncio.run(p._due_wakes(datetime(2026, 10, 1, 8, 8, tzinfo=LA)))
        assert [w.key for w in wakes] == ["clock:due:6ec742:2026-10-01T08:00:mei"], case
        assert len(json.loads(store.get_meta("clock:marked"))) == 2 and store.get_meta("clock:lost") is None
        if case == "closed":
            # the look gave it to fan this morning and closed it, before its wake ran
            p._mem = lambda now, *args: asyncio.sleep(0, "")
        else:
            store.set_meta("clock:due:6ec742:2026-10-01T08:00:mei", utcnow())
        asyncio.run(p._due_wakes(datetime(2026, 10, 1, 8, 13, tzinfo=LA)))
        lost = [(r["id"], r["by"], r["asked"]) for r in json.loads(store.get_meta("clock:lost") or "[]")]
        assert marks(store) == [("596f2d", "2026-10-01T09:30", "fan")], case
        assert lost == ([("6ec742", "2026-10-01T08:00", "mei")] if case == "closed" else []), case


def test_a_look_marks_only_her_own_reminder_one_of_the_household_asked_for():
    """The clock's own mark, on a reminder whose time has come while its wake
    waits, takes `due_wakes`' two tests, each on its own: someone of the
    household asked for it, and her own node is among those it involves."""
    def item(tid, involves, asked=""):
        return (f"`trajectory:{tid}`  2026-10-01T08:00, today  x\n    involves: {involves}\n"
                + (f"    asked by: {asked}\n" if asked else ""))
    hers = item("aaaaaa", "me; fan", "fan")
    listed = ("Come due for fan after 2026-09-30:\n" + hers + item("bbbbbb", "me; fan", "jane")
              + item("cccccc", "me; fan") + item("dddddd", "fan", "fan"))
    mark = "    still to come: the clock gives it to fan at 08:00, but only while it is open and timed so\n"
    six = datetime(2026, 10, 1, 8, 6, tzinfo=LA)
    assert clock.still_to_come(listed.splitlines(), six, ASKERS, lambda k: False) == listed.replace(
        hers, hers + mark).splitlines()


def renamed(store: Store, uid: str, name: str) -> Household:
    """The run store once memory has taken `name` for `uid`, which sessions
    are then told; every name before it still leads to the id."""
    h = Household.load(store, list(NAMES))
    h.advance(uid, name, "s-names", datetime(2026, 9, 20, tzinfo=timezone.utc))
    h.save(store, uid)
    return h


def test_a_reminder_asked_under_an_earlier_name_says_so(tmp_path):
    """fan asked under his old name, and is Fan Zhu now: his wake is in his
    DM, and says what she knew him as, spelled as the item prints it. One he
    asked under the name he has now says nothing of it."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [SILENT, SILENT])
    p.household = renamed(store, "U1", "Fan Zhu")
    item = ("`trajectory:{tid}`  2026-10-01T17:00, today  I undertook to remind fan at 5 to call the plumber\n"
            "    involves: me; fan\n    asked by: {asked}\n")
    due = item.format(tid="a24e0d", asked="Fan") + item.format(tid="b35f1e", asked="fan zhu")

    async def mem(now, *args):
        return due
    p._mem = mem
    at5 = datetime(2026, 10, 1, 17, 0, tzinfo=LA)
    wakes = asyncio.run(p._due_wakes(at5))
    assert [(w.person, w.asked) for w in wakes] == [("U1", "fan"), ("U1", "fan zhu")]
    assert wakes[0].text.endswith("\n        asked by: Fan\n        " + clock.ASKED_THEN.format(now="Fan Zhu", asked="Fan"))
    assert wakes[0].arrival("Fan Zhu").endswith("    asked by: Fan\n        When this was asked, I knew Fan Zhu as "
                                                "Fan.")
    assert clock.ASKED_THEN.split("{")[0] not in wakes[1].text
    assert wakes[1].text.endswith("asked by: fan zhu")


def test_a_look_says_which_items_were_asked_under_an_earlier_name(tmp_path):
    """In fan's look, last under an item he asked as fan, and under one mei
    asked as mei since she became Mei Chen; not under one asked under its
    asker's name now."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [SILENT])
    renamed(store, "U1", "Fan Zhu")
    p.household = renamed(store, "U2", "Mei Chen")
    gift = ("`trajectory:596f2d`  2026-10-01T09:30, today  I undertook to remind fan at 9:30 to ring the bank\n"
            "    involves: me; fan\n    asked by: fan\n"
            "    still to come: the clock gives it to fan at 09:30, but only while it is open and timed so\n")
    tablet = ("`trajectory:6ec742`  2026-10-01T08:00, today  I undertook to remind mei at 8 to give fan his tablet\n"
              "    involves: me; mei; fan\n    asked by: mei\n")
    shed = ("`trajectory:94f6cf`  2026-10-01, today  I undertook to remind fan to lock the shed\n"
            "    involves: me; fan\n    asked by: Fan Zhu\n")
    listed = "Come due for Fan Zhu after 2026-09-30:\n" + gift + tablet + shed
    mark = "    still to come: the clock gives it to mei at 08:00, but only while it is open and timed so"
    six = datetime(2026, 10, 1, 8, 6, tzinfo=LA)
    got = clock.still_to_come(listed.splitlines(), six, p.household.askers(), lambda k: False,
                              p.household.told_names())
    assert got == (
        "Come due for Fan Zhu after 2026-09-30:\n" + gift + "    When this was asked, I knew Fan Zhu as fan.\n"
        + tablet + mark + "\n    When this was asked, I knew Mei Chen as mei.\n" + shed).splitlines(), got
    assert clock.still_to_come(got, six, p.household.askers(), lambda k: False, p.household.told_names()) == got
    # the look's session is handed it so
    p._mem = lambda now, *args: asyncio.sleep(0, listed)
    [look] = clock.morning_wakes(six, {"U1": LOOKS["U1"]}, QUIET, lambda q: None, p.household.told)
    asyncio.run(p._clock_session(look, six))
    assert "\n        When this was asked, I knew Fan Zhu as fan.\n" in turns[0][2]
    assert turns[0][2].endswith("    asked by: Fan Zhu")


def test_every_reminder_not_given_is_named_and_none_is_dropped(tmp_path):
    """Two in one UTC day, the second while the first's alert waits for
    Slack, are named together; one after the day's alert is named the next
    day; and none is kept twice, however often a check sees it."""
    slack = FakeSlack()
    p, store, turns, alerts, mems = processor(tmp_path, slack, [])
    up = []

    async def alert(text):
        if not up:
            raise RuntimeError("ratelimited")
        slack.alerts.append(text)
    slack.alert = alert
    p._lost("a24e0d", "2026-10-01T17:00", "mei", "its session failed")
    asyncio.run(p._flush_lost())
    p._lost("b6647b", "2026-10-01T19:00", "fan", "its time was more than 2 h gone when the clock saw it")
    p._lost("b6647b", "2026-10-01T19:00", "fan", "its time was more than 2 h gone when the clock saw it")
    up.append(1)
    asyncio.run(p._flush_lost())
    assert slack.alerts == ["2 timed reminder(s) not given at their time: trajectory:a24e0d due "
                            "2026-10-01T17:00; trajectory:b6647b due 2026-10-01T19:00. Who asked, why, "
                            "and how to give one later are in doctor."]
    p._lost("07f41d", "2026-10-01T21:45", "fan", "its session failed")
    asyncio.run(p._flush_lost())
    assert len(slack.alerts) == 1, "one a UTC day"
    store.set_meta("reminder_alert_date", "2026-01-01")  # the next day
    asyncio.run(p._flush_lost())
    assert slack.alerts[1].startswith("1 timed reminder(s) not given at their time: trajectory:07f41d due")
    assert all(r["named"] for r in json.loads(store.get_meta("clock:lost")))


def test_a_reminder_too_late_to_wake_for_is_kept_whenever_it_came_due(tmp_path):
    """The due check reaches back to the day before the last one that ran, so
    a reminder that came due while the daemon was down, past its two days, is
    still seen and kept as not given."""
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [], DUE)
    store.set_meta("clock:checked", "2026-09-27")
    later = datetime(2026, 10, 3, 9, 0, tzinfo=LA)
    asyncio.run(p._due_wakes(later))
    assert mems == [("due", "--after", "2026-09-26")]
    assert store.get_meta("clock:checked") == "2026-10-03"
    lost = json.loads(store.get_meta("clock:lost"))
    assert sorted((r["id"], r["asked"]) for r in lost) == [("07f41d", "fan"), ("a24e0d", "mei"),
                                                           ("b6647b", "fan")]
    assert {r["why"] for r in lost} == {"its time was more than 2 h gone when the clock saw it"}
    asyncio.run(p._due_wakes(later + timedelta(minutes=5)))
    assert mems[1] == ("due", "--after", "2026-10-01"), "two days back at least"
    assert len(json.loads(store.get_meta("clock:lost"))) == 3


def test_doctor_lists_the_reminders_not_given(tmp_path, capsys):
    """Each with who asked and why, then the command that shows the item as
    it stands and the one that reopens and re-dates it, noting that it was not
    given at its time; one the person moved to another time that day, which
    the clock gives then, with no command. The item itself is not printed:
    whoever runs doctor may be the person it is kept from."""
    cfg = settings(tmp_path, claude_bin="/usr/bin/true", email_triage=False)
    store = named(Store(cfg.db_path))
    p = Processor(cfg, store, asyncio.Queue(), FakeSlack(), RunnerService("/bin/true"))
    # fan has since become Fan Zhu, and memory has taken it
    p.household.advance("U1", "Fan Zhu", "s-1", datetime.now(timezone.utc))
    p.household.save(store, "U1")
    p._lost("b6647b", "2026-10-01T19:00", "fan", "its session failed")
    p._lost("a24e0d", "2026-10-01T17:00", "mei", "it was re-dated to 18:00 the same day, after a morning look "
            "listed it as still to come; the clock gives it at that time instead", moved="2026-10-01T18:00")
    store.close()
    asyncio.run(run_doctor(cfg, smoke=False))
    out = capsys.readouterr().out
    assert "✓ timed reminders not given, last 30 days — 2" in out, out
    failed = out.index("trajectory:b6647b due 2026-10-01T19:00, asked by fan (U1, now Fan Zhu): its session failed")
    assert out.index("        to see it as it stands: docker compose -f compose.wanda.yaml exec wanda mem show "
                     "trajectory:b6647b\n", failed) < out.index(
        "        to give it later: docker compose -f compose.wanda.yaml exec wanda mem advance trajectory:b6647b "
        f'--status open --by <date>T<HH:MM> --note "{REOPENED}"', failed), out
    assert ("trajectory:a24e0d due 2026-10-01T17:00, asked by mei (U2): it was re-dated to 18:00 the same day" in out
            and "trajectory:a24e0d --status open" not in out and "mem show trajectory:a24e0d" not in out), out
    assert "keep the gift" not in out and "I undertook" not in out, "no item's text"


@pytest.mark.parametrize("spec,quiet,said", [
    (["U108:00"], "21:30-07:00", "WANDA_MORNINGS=U108:00 is not a list of <member id>@HH:MM, as U0123456789@08:00"),
    (["U1@8am"], "21:30-07:00", "WANDA_MORNINGS=U1@8am is not a list of <member id>@HH:MM"),
    (["U1@25:00"], "21:30-07:00", "WANDA_MORNINGS=U1@25:00 is not a list of <member id>@HH:MM"),
    # a name, which an older .env may give
    (["fan@08:00"], "21:30-07:00", "WANDA_MORNINGS names fan, which is not in WANDA_SLACK_OWNER_USER_IDS: "
                                   "it takes member ids, as U0123456789@08:00"),
    (["U9@08:00"], "21:30-07:00", "WANDA_MORNINGS names U9, which is not in WANDA_SLACK_OWNER_USER_IDS"),
    (["u1@08:00"], "21:30-07:00", "WANDA_MORNINGS names u1, which is not in WANDA_SLACK_OWNER_USER_IDS"),
    (["@08:00"], "21:30-07:00", "WANDA_MORNINGS=@08:00 is not a list of <member id>@HH:MM"),
    (["U1@08:00", " @07:30"], "21:30-07:00", "WANDA_MORNINGS=U1@08:00, @07:30 is not a list of <member id>@HH:MM"),
    (["U1@08:00", " U1 @11:00"], "21:30-07:00",
     "WANDA_MORNINGS gives U1 more than one time, and a person has one look a day"),
    (["U1@12:30"], "21:30-07:00", "WANDA_MORNINGS puts U1 at or after 12:00"),
    (["U1@08:00"], "22-07", "WANDA_QUIET_HOURS=22-07 is not HH:MM-HH:MM"),
    (["U1@08:00", "U2@07:30"], "21:30-12:00",
     "WANDA_QUIET_HOURS=21:30-12:00 keeps U1 and U2 from having a look before 12:00"),
    (["U1@08:00", "U2@07:30"], "07:45-13:00", "WANDA_QUIET_HOURS=07:45-13:00 keeps U1 from having"),
    (["U1@08:00", "U2@07:30"], "", None),
    (["U1@08:00", "U2@07:30"], "21:30-11:59", None),
    (["U1@08:00", "U2@07:30"], "00:00-00:00", None),
])
def test_a_setting_the_clock_cannot_read_is_one_sentence(spec, quiet, said):
    got = clock.settings_problem(spec, quiet, ["U1", "U2"])
    assert (got is None) if said is None else got.startswith(said), got


@pytest.mark.parametrize("change,said", [
    (dict(mornings=["U1@8am"]), r"^WANDA_MORNINGS=U1@8am is not a list of <member id>@HH:MM"),
    (dict(mornings=["fan@08:00"]), r"^WANDA_MORNINGS names fan, which is not in WANDA_SLACK_OWNER_USER_IDS"),
    (dict(mornings=["U1@08:00", "U1@09:00"]), r"^WANDA_MORNINGS gives U1 more than one time"),
    (dict(mornings=["U9@08:00"]), r"^WANDA_MORNINGS names U9, which is not in WANDA_SLACK_OWNER_USER_IDS"),
])
def test_the_daemon_refuses_a_clock_setting_with_its_sentence(tmp_path, monkeypatch, change, said):
    def past_the_check(*args, **kw):
        raise AssertionError("the daemon went past its settings check, towards Slack")
    # the tokens are dummies, and a daemon that took the setting would connect next
    monkeypatch.setattr("wanda.main.SlackActions", past_the_check)
    monkeypatch.setattr("wanda.main.SlackWatcher", past_the_check)
    cfg = settings(tmp_path, slack_bot_token="xoxb-x", slack_app_token="xapp-x", alert_channel="C9",
                   email_triage=False).model_copy(update=change)
    with pytest.raises(SystemExit, match=said):
        asyncio.run(run_daemon(cfg))


def test_doctor_shows_each_look_by_its_member_id(tmp_path, capsys):
    cfg = settings(tmp_path, claude_bin="/usr/bin/true", email_triage=False)
    store = named(Store(cfg.db_path))
    store.set_meta("clock:outcome:U1", "2026-10-01 08:00 silent")
    store.close()
    asyncio.run(run_doctor(cfg, smoke=False))
    out = capsys.readouterr().out
    assert " last look for U1 (fan) — 2026-10-01 08:00 silent\n" in out, out
    assert " last look for U2 (mei) — none yet\n" in out, out


def test_doctor_reports_a_clock_setting_and_goes_on(tmp_path, capsys):
    for kw, line in ((dict(mornings=["U108:00"]), "✗ clock — WANDA_MORNINGS=U108:00 is not"),
                     (dict(tz="Mars/Olympus"), "✗ clock — WANDA_TZ=Mars/Olympus is not a time zone")):
        cfg = settings(tmp_path, claude_bin="/usr/bin/true", email_triage=False).model_copy(update=kw)
        asyncio.run(run_doctor(cfg, smoke=False))
        out = capsys.readouterr().out
        assert line in out and "store:" in out and "✓ sqlite" in out, out


def test_doctor_passes_a_look_only_when_one_ran():
    running = timedelta(minutes=30)
    eight = time(8, 0)
    at = datetime(2026, 10, 1, 8, 20, tzinfo=LA)
    for outcome, ok in (
            (None, True),                                # today's can still start
            ("2026-10-01 08:00 spoke", True), ("2026-10-01 08:00 silent", True),
            ("2026-10-01 08:00 spoke, then failed", True),
            ("2026-09-30 08:00 silent", True),
            ("2026-10-01 08:15 started", True),          # running now
            ("2026-10-01 07:45 started", False),         # a restart cut it short
            ("2026-10-01 08:00 failed", False), ("2026-10-01 08:00 not run", False),
            ("2026-10-01 08:19 could not start", False),
            ("2026-09-29 08:00 spoke", False)):
        assert clock.look_healthy(outcome, at, running, eight) is ok, outcome
    # once today's has had a look's length to start, and has not, it is late
    nine = at.replace(hour=9, minute=0)
    assert not clock.look_healthy("2026-09-30 08:00 silent", nine, running, eight), "not started today"
    assert not clock.look_healthy(None, nine, running, eight)
    # a look waiting for quiet hours to end is late only a look's length after
    seven = at.replace(hour=7, minute=20)
    assert clock.look_healthy("2026-09-30 07:00 silent", seven, running, clock.first_start(time(6, 15), QUIET))
    assert not clock.look_healthy("2026-09-30 07:00 silent", seven, running, time(6, 15))
    noon = at.replace(hour=12, minute=5)
    assert not clock.look_healthy("2026-10-01 12:00 skipped", noon, running, eight)


def test_a_day_with_no_look_is_said_at_noon(tmp_path, caplog):
    p, store, turns, alerts, mems = processor(tmp_path, FakeSlack(), [])
    store.set_meta("clock:morning:U1", "2026-10-01")
    store.set_meta("clock:outcome:U2", "2026-09-30 07:30 silent")
    noon = datetime(2026, 10, 1, 12, 0, tzinfo=LA)
    claimed = lambda q: store.get_meta(f"clock:morning:{q}")  # noqa: E731
    assert clock.missed(noon - timedelta(minutes=1), LOOKS, claimed) == []
    with caplog.at_level(logging.WARNING, logger="wanda"):
        for minute in (0, 1):
            for person in clock.missed(noon + timedelta(minutes=minute), LOOKS, claimed):
                p._skip_look(person, noon + timedelta(minutes=minute))
    assert store.get_meta("clock:outcome:U2") == "2026-10-01 12:00 skipped"
    assert store.get_meta("clock:outcome:U1") is None
    assert [r.getMessage() for r in caplog.records] == ["clock: no look for U2 ran before noon; none today"]


def test_what_mem_says_on_the_side_is_logged_once(tmp_path, monkeypatch, caplog):
    fake = tmp_path / "bin" / "mem"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'mem due: dd1106 has a date no calendar has (2026-09-31); "
                    "it never comes due' >&2\necho '(nothing open has come due by 2026-10-01)'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    store = named(Store(tmp_path / "p.db"))
    p = Processor(settings(tmp_path), store, asyncio.Queue(), FakeSlack(), RunnerService("/bin/true"))
    now = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    with caplog.at_level(logging.WARNING, logger="wanda"):
        for _ in range(2):
            assert asyncio.run(p._mem(now, "due", "--after", "2026-09-29")).startswith("(nothing open")
    assert [r.getMessage() for r in caplog.records] == [
        "mem due: dd1106 has a date no calendar has (2026-09-31); it never comes due"]


def test_the_clocks_own_mem_call_is_ended_when_it_runs_past_its_bound(tmp_path, monkeypatch):
    """A `mem` held up, as on the vault's lock, is ended after MEM_TIMEOUT_S
    and left running nowhere: the due check is skipped and the date of the
    last one kept, so the next reaches as far back; a look cannot start,
    claims nothing and says so."""
    fake = tmp_path / "bin" / "mem"
    fake.parent.mkdir()
    pids = tmp_path / "pids"
    fake.write_text(f"#!/bin/sh\necho $$ >> {pids}\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setattr("wanda.main.MEM_TIMEOUT_S", 0.5)
    store = named(Store(tmp_path / "p.db"))
    p = Processor(settings(tmp_path), store, asyncio.Queue(), FakeSlack(), RunnerService("/bin/true"))
    alerts = []

    async def alert_once(kind, text):
        alerts.append((kind, text))
    p._alert_once = alert_once
    store.set_meta("clock:checked", "2026-09-30")
    now = datetime(2026, 10, 1, 8, 0, tzinfo=LA)
    assert asyncio.run(p._due_wakes(now)) == []
    assert store.get_meta("clock:checked") == "2026-09-30"
    w = clock.morning_wakes(now, {"U1": LOOKS["U1"]}, QUIET, lambda q: None, NAMES.get)[0]
    asyncio.run(p._clock_session(w, now))
    assert store.get_meta("clock:morning:U1") is None
    assert store.get_meta("clock:outcome:U1") == "2026-10-01 08:00 could not start"
    assert alerts == [("clock", "a morning look could not start on 2026-10-01; doctor says whose")]
    started = pids.read_text().split()
    assert len(started) == 2
    for pid in started:
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


def test_the_clock_settings_reach_the_container(monkeypatch):
    compose = (ROOT / "compose.wanda.yaml").read_text()
    assert "      WANDA_MORNINGS: ${WANDA_MORNINGS:-}\n" in compose
    # one dash: an empty value in .env reaches the container as it is
    assert "      WANDA_QUIET_HOURS: ${WANDA_QUIET_HOURS-21:30-07:00}\n" in compose
    monkeypatch.setenv("WANDA_QUIET_HOURS", "")
    assert clock.quiet_hours(Config(_env_file=None).quiet_hours) is None


def transcript(dirpath: Path, sid: str, arrival: str) -> None:
    """One exchange's opening, in the shape memory/src/transcript.rs reads."""
    prompt = vault.prompt("2026-10-01", arrival)
    line = {"type": "user", "timestamp": "2026-10-01T23:58:00Z", "message": {"content": prompt}}
    (dirpath / f"{sid}.jsonl").write_text(json.dumps(line) + "\n")


@pytest.mark.skipif(not os.environ.get("TEST_MEM_BIN"), reason="TEST_MEM_BIN names a mem build")
def test_doctors_command_gives_a_reminder_its_session_closed_later(tmp_path, capsys, monkeypatch):
    """The command doctor prints for a reminder not given, run as printed on
    one its session closed: the clock wakes only for an open item, so the
    command reopens it as well as re-dating it, and the clock then wakes the
    person who asked at the new time. Doctor first gives the command that
    shows the item, which, run as printed, shows its note that it was given;
    the command that gives it later notes that it was not."""
    tx, v = tmp_path / "tx", tmp_path / "v"
    tx.mkdir()
    transcript(tx, "s-fan", vault.arrival_text("dm", "fan", "remind me at 7 to pick up the gift for mei", [], []))
    env = {**os.environ, "MEM_VAULT": str(v), "MEM_TRANSCRIPTS": str(tx), "MEM_DATE": "2026-10-01",
           "MEM_REAL_DATE": "2026-10-01", "MEM_TEMPLATES": str(ROOT / "memory/templates")}

    def mem(*args, **extra):
        return subprocess.run([os.environ["TEST_MEM_BIN"], *args], env=env | extra,
                              capture_output=True, text=True, check=True).stdout
    made = mem("trajectory", "--summary", "I undertook to remind fan at 7 to pick up the gift for mei",
               "--expect", "fan reminded", "--by", "2026-10-01T19:00", "--about", "me,fan", MEM_SESSION="s-fan")
    tid = re.search(r"trajectory:(\w+)", made).group(1)
    # its session wrote that it reminded him, and was cut short before anything was posted
    mem("advance", f"trajectory:{tid}", "--status", "closed", "--note", "I reminded fan at 19:02",
        MEM_SESSION="s-wake")
    cfg = settings(tmp_path, claude_bin="/usr/bin/true", email_triage=False, vault=str(v))
    store = Store(cfg.db_path)
    Processor(cfg, store, asyncio.Queue(), FakeSlack(), RunnerService("/bin/true"))._lost(
        tid, "2026-10-01T19:00", "fan", "its session was cut short, and it was not open at that time when "
        "the clock looked again")
    store.close()
    asyncio.run(run_doctor(cfg, smoke=False))
    out = capsys.readouterr().out
    shown = re.search(r"to see it as it stands: docker compose -f compose\.wanda\.yaml exec wanda mem (.*)", out)
    printed = re.search(r"to give it later: docker compose -f compose\.wanda\.yaml exec wanda mem (.*)", out)
    assert shown and printed and shown.start() < printed.start(), out
    assert "I reminded fan at 19:02" not in out, "the item is not printed"
    # run as printed, before the command, it shows the note that says it was given
    assert "I reminded fan at 19:02" in mem(*shlex.split(shown.group(1)))
    mem(*shlex.split(printed.group(1).replace("<date>T<HH:MM>", "2026-10-01T21:00")))
    due = mem("due", "--after", "2026-09-30")
    assert f"`trajectory:{tid}`  2026-10-01T21:00, today" in due and "    asked by: fan" in due, due
    assert REOPENED in mem("show", f"trajectory:{tid}")
    woke = clock.due_wakes(datetime(2026, 10, 1, 21, 0, tzinfo=LA), clock.items(due), ASKERS,
                           lambda k: False, set())
    assert [w.key for w in woke] == [f"clock:due:{tid}:2026-10-01T21:00:fan"]


@pytest.mark.skipif(not os.environ.get("TEST_MEM_BIN"), reason="TEST_MEM_BIN names a mem build")
def test_a_reminder_asked_in_a_turn_of_two_speakers_has_no_one_asker(tmp_path, caplog):
    """fan's request and mei's next line taken in one turn, in the product's own
    frame: the speaker is no one person, so `mem due` names no asker and the
    clock wakes no one; the morning look keeps it in its list."""
    tx, v = tmp_path / "tx", tmp_path / "v"
    tx.mkdir()
    transcript(tx, "s-both", vault.arrival_text(
        "group", "mei", "I'm out then anyway", ["fan", "mei"],
        [("16:58", "fan", "remind me at 5 tomorrow to call the shop")], also=["fan"]))
    transcript(tx, "s-fan", vault.arrival_text("dm", "fan", "remind me at 6 tomorrow to ring mum", [], []))
    env = {**os.environ, "MEM_VAULT": str(v), "MEM_TRANSCRIPTS": str(tx), "MEM_DATE": "2026-10-01",
           "MEM_REAL_DATE": "2026-10-01", "MEM_TEMPLATES": str(ROOT / "memory/templates")}

    def mem(*args, **extra):
        return subprocess.run([os.environ["TEST_MEM_BIN"], *args], env=env | extra,
                              capture_output=True, text=True, check=True).stdout
    mem("trajectory", "--summary", "I undertook to remind fan at 5 on 2 Oct to call the shop",
        "--expect", "fan reminded", "--by", "2026-10-02T17:00", "--about", "me,fan", MEM_SESSION="s-both")
    mem("trajectory", "--summary", "I undertook to remind fan at 6 on 2 Oct to ring his mum",
        "--expect", "fan reminded", "--by", "2026-10-02T18:00", "--about", "me,fan", MEM_SESSION="s-fan")
    due = mem("due", "--after", "2026-10-01", MEM_DATE="2026-10-02", MEM_REAL_DATE="2026-10-02")
    assert due.count("    asked by: ") == 1 and "    asked by: fan" in due, due
    with caplog.at_level(logging.WARNING, logger="wanda.clock"):
        woke = [clock.due_wakes(datetime(2026, 10, 2, h, 0, tzinfo=LA), clock.items(due), ASKERS,
                                lambda k: False, set()) for h in (17, 18)]
    assert [[w.person for w in ws] for ws in woke] == [[], ["U1"]]
    assert any("who asked is not known (no one person)" in r.getMessage() for r in caplog.records)
    # the look at 08:00 is handed both, and marks as still to come only the
    # one the clock will wake for at 6
    look = mem("due", "--for", "fan", "--after", "2026-10-01", "--at", "08:00",
               MEM_DATE="2026-10-02", MEM_REAL_DATE="2026-10-02").splitlines()
    mum = next(i for i, ln in enumerate(look) if "ring his mum" in ln)
    assert look[mum + 3] == ("    still to come: the clock gives it to fan at 18:00, but only while it is "
                             "open and timed so"), look
    assert sum("still to come" in ln for ln in look) == 1, look
