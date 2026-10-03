"""Each household member's name from Slack, and every name they were told
by, as the run store keeps them: which name Slack gives can be used, who is
let in, a change held until it is due, what memory's answer about a change
means, and what doctor shows. No Slack, no `mem`: a user record is what
users.info returns, a run is a row, and memory's answer is what `mem show`
printed."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from wanda.household import NAMES_EVERY_S, Found, Household, flaw, found, settle
from wanda.store import Store, utcnow

T0 = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
LA = ZoneInfo("America/Los_Angeles")
ROUND = timedelta(seconds=NAMES_EVERY_S)


def user(display=None, full=None, **kw) -> dict:
    return {"id": "U?", "profile": {"display_name": display or "", "real_name": full or ""}, **kw}


def seen(h: Household, uid: str, display=None, full=None, at=T0) -> str:
    return h.observe(uid, user(display, full), at)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "w.db")
    yield s
    s.close()


def ran(store, sid, status="ok"):
    store.record_run(kind="agent", task_id=None, session_id=sid, started_at=utcnow(), exit_code=0,
                     cost_usd=0.0, status=status)


# --- which name Slack gives can be used ---

@pytest.mark.parametrize("display,full,told", [
    ("fan", "", "fan"),
    ("", "Fan Zhu", "Fan Zhu"),
    ("  Fan   Zhu ", "", "Fan Zhu"),
    ("Wanda", "Wanda Li", "Wanda Li"),
    ("Mei now", "Mei Chen", "Mei Chen"),
    ("**", "Fan Zhu", "Fan Zhu"),
    ("mei~", "Mei Chen", "Mei Chen"),
])
def test_the_name_slack_gives(display, full, told):
    h = Household({}, ["U1"])
    seen(h, "U1", display, full)
    assert h.told("U1") == told


def test_the_full_name_may_be_the_top_level_one():
    h = Household({}, ["U1"])
    h.observe("U1", {"real_name": "Fan Zhu", "profile": {"display_name": ""}}, T0)
    assert h.told("U1") == "Fan Zhu"


def test_a_change_of_capitals_is_the_same_name():
    h = Household({}, ["U1"])
    seen(h, "U1", "fan")
    seen(h, "U1", "FAN", at=T0 + ROUND)
    assert h.told("U1") == "FAN" and len(h.rows["U1"]["told"]) == 1


def test_another_members_name_falls_to_the_full_name_or_to_none():
    h = Household({}, ["U1", "U2", "U3"])
    seen(h, "U2", "mei")
    seen(h, "U1", "mei", "Mei Chen")
    assert h.told("U1") == "Mei Chen", "U2 is told it"
    seen(h, "U2", "may", at=T0 + ROUND)
    assert h.awaiting("U2") == "may"
    seen(h, "U3", "may", "May Li")
    assert h.told("U3") == "May Li", "U2's Slack shows it awaiting"
    # a name another member had before
    h = Household({}, ["U1", "U2"])
    seen(h, "U1", "fan")
    h.advance("U1", "Fan Zhu", "s-1", T0)
    seen(h, "U2", "fan", "")
    assert h.told("U2") is None and h.told_names() == {"U1": "Fan Zhu"}
    assert h.rows["U2"]["out"]["why"] == "no usable name"
    assert h.rows["U2"]["slack"]["why"] == "display name fan is U1's; full name empty"


@pytest.mark.parametrize("bad", ["me", "ME", "wanda", "Wanda", "fan (after mei", "fan (then mei", "Mei now",
                                 "x" * 141, "a1b2c3", "a1b2c3 x", "person:fan", "2026-10-01-a1b2c3",
                                 "*", "**", "mei~", "al~~x"])
def test_each_name_mem_or_a_frame_would_misread_falls_to_the_full_name_then_to_none(bad):
    assert flaw(bad), bad
    h = Household({}, ["U1", "U2"])
    seen(h, "U1", bad, "Mei Chen")
    assert h.told("U1") == "Mei Chen"
    assert h.rows["U1"]["slack"]["field"] == "full name" and h.rows["U1"]["slack"]["why"].startswith(
        f"display name {bad} ")
    seen(h, "U2", bad, bad)
    assert h.told("U2") is None and h.rows["U2"]["slack"]["name"] is None


@pytest.mark.parametrize("fine", ["Fan Zhu", "-fan", "李梅", "fan says", "Now Mei", "fan_zhu", "fan/zhu",
                                  "~bo", "a~b", "x" * 140, "abcdef", "f4n"])
def test_a_name_mem_reads_back_as_itself_is_used(fine):
    assert flaw(fine) is None, fine


# --- who is let in ---

def test_a_first_sight_lets_an_id_in_by_the_name_slack_gives():
    h = Household({}, ["U1"])
    assert h.told_names() == {} and h.told("U1") is None
    assert seen(h, "U1", "fan", "Fan Zhu") == "names: U1 is let in as fan (display name)"
    assert h.told_names() == {"U1": "fan"} and h.rows["U1"]["told"] == [
        {"name": "fan", "since": "2026-10-04T15:00:00+00:00", "session": ""}]


def test_a_shared_name_goes_to_the_first_listed_within_a_round_and_the_first_shown_across_rounds():
    h = Household({}, ["U1", "U2"])
    seen(h, "U1", "fan", "Fan Zhu")
    seen(h, "U2", "fan", "Fan Li")
    assert h.told_names() == {"U1": "fan", "U2": "Fan Li"}
    # U1's read failed in the first round; U2 showed mei first
    h = Household({}, ["U1", "U2"])
    h.unread("U1", "timeout", T0)
    seen(h, "U2", "mei", "Mei Li")
    seen(h, "U1", "mei", "Mei Zhu", at=T0 + ROUND)
    assert h.told_names() == {"U1": "Mei Zhu", "U2": "mei"}


def test_of_two_changing_to_one_name_the_second_is_held():
    h = Household({}, ["U1", "U2"])
    seen(h, "U1", "fan")
    seen(h, "U2", "mei")
    seen(h, "U1", "dad", at=T0 + ROUND)
    said = seen(h, "U2", "dad", at=T0 + ROUND)
    assert h.awaiting("U1") == "dad" and h.awaiting("U2") is None and h.told("U2") == "mei"
    assert said == "names: U2 has no name in Slack sessions can use (display name dad is U1's; full name empty); "\
                   "sessions say mei"
    assert seen(h, "U2", "dad", at=T0 + 2 * ROUND) == "", "said once"


def test_a_name_a_member_shows_as_its_kept_change_is_held_for_anyone_else():
    h = Household({}, ["U1", "U2", "U3"])
    seen(h, "U1", "fan")
    seen(h, "U2", "mei")
    seen(h, "U1", "Fan Zhu", at=T0 + ROUND)
    h.keep("U1", "Fan Zhu", "s-1", "fan stays fan", True, T0 + ROUND)
    assert h.awaiting("U1") is None
    seen(h, "U2", "Fan Zhu", at=T0 + 2 * ROUND)
    assert h.awaiting("U2") is None and h.rows["U2"]["slack"]["name"] is None
    seen(h, "U3", "Fan Zhu")
    assert h.told("U3") is None, "at a first sight too"


def test_a_tried_name_is_held_after_slack_moves_on_and_the_try_still_advances(store):
    """U1's try for X stands beside its run recorded ok, as while memory's
    answer cannot be read, and its Slack then shows another name: X no
    longer awaits, and is still held from anyone else."""
    h = Household({}, ["U1", "U2"])
    seen(h, "U1", "fan")
    seen(h, "U1", "Fan Zhu", at=T0 + ROUND)
    seen(h, "U1", "Fan Zhu", at=T0 + 2 * ROUND)
    assert h.due(store, T0 + 2 * ROUND) == ("U1", "fan", "Fan Zhu")
    h.trying("U1", "Fan Zhu", "s-1", T0 + 2 * ROUND)
    ran(store, "s-1")
    h.failed("U1", "Fan Zhu", "mem show: the store stayed busy", T0 + 2 * ROUND)
    seen(h, "U1", "Fan Z", at=T0 + 3 * ROUND)
    assert h.awaiting("U1") == "Fan Z"
    seen(h, "U2", "Fan Zhu", "Fan Li", at=T0 + 3 * ROUND)
    assert h.told("U2") == "Fan Li"
    assert h.advance("U1", "Fan Zhu", "s-1", T0 + 3 * ROUND) is True and h.told("U1") == "Fan Zhu"


def test_a_removed_id_holds_only_the_names_it_was_told():
    h = Household({}, ["U1", "U9"])
    seen(h, "U9", "jane")
    seen(h, "U9", "Jane D", at=T0 + ROUND)
    h.trying("U9", "Jay", "s-9", T0 + ROUND)
    h.keep("U9", "Jane D", "s-9", "jane stays jane", True, T0 + ROUND)
    seen(h, "U9", "Jane D", at=T0 + 2 * ROUND)
    h.trying("U9", "Jay", "s-9", T0 + 2 * ROUND)
    held = Household(h.rows, ["U1", "U9"])
    assert held.refusal("U1", "Jane D") == "is U9's" and held.refusal("U1", "Jay") == "is U9's"
    removed = Household(h.rows, ["U1"])
    assert removed.refusal("U1", "Jane D") is None and removed.refusal("U1", "Jay") is None
    assert removed.refusal("U1", "JANE") == "is U9's"


def test_an_answer_that_shows_no_one_lets_in_no_one_new_and_leaves_a_member_in():
    h = Household({}, ["U1", "U2", "U3", "U4"])
    seen(h, "U1", "fan")
    assert h.unread("U1", "user_not_found", T0) == ("names: Slack no longer shows U1 (user_not_found); "
                                                    "sessions say fan, as before")
    assert h.observe("U2", user("mei", deleted=True), T0) == "names: U2 is not let in: Slack does not show them " \
                                                               "(deleted)"
    h.observe("U3", user("bot", is_bot=True), T0)
    seen(h, "U4", "", "")
    assert h.told_names() == {"U1": "fan"}
    assert {u: h.rows[u]["out"]["why"] for u in ("U1", "U2", "U3", "U4")} == {
        "U1": "user_not_found", "U2": "deleted", "U3": "bot", "U4": "no usable name"}
    assert h.rows["U1"]["slack"]["read"] == "2026-10-04T15:00:00+00:00", "the answer does not stand as a read"
    # a later usable read lets an id in
    assert seen(h, "U2", "mei", at=T0 + ROUND) == "names: U2 is let in as mei (display name)"
    seen(h, "U1", "fan", at=T0 + ROUND)
    assert h.rows["U1"]["out"] is None and h.rows["U2"]["out"] is None


def test_an_id_slack_has_no_member_for_is_said_to_be_one(store):
    """The allowlist takes member ids; a direct message's id is the one
    most often pasted in place of one."""
    h = Household({}, ["U1", "D0123456789"])
    assert h.unread("U1", "user_not_found", T0) == ("names: U1 is not let in: WANDA_SLACK_OWNER_USER_IDS lists U1, "
                                                    "which Slack has no member for")
    assert h.unread("D0123456789", "user_not_found", T0) == (
        "names: D0123456789 is not let in: WANDA_SLACK_OWNER_USER_IDS lists D0123456789, which Slack has no "
        "member for: a member id starts with U or W; a D… id is a direct message")
    assert shown(h, store, "D0123456789", T0) == (
        False, "not let in: WANDA_SLACK_OWNER_USER_IDS lists D0123456789, which Slack has no member for: a member "
               "id starts with U or W; a D… id is a direct message")


def test_an_id_slack_goes_on_not_showing_is_alerted_once():
    """Slack's same answer on a later day is not a new event."""
    h = Household({}, ["U1"])
    seen(h, "U1", "fan")
    h.unread("U1", "user_not_found", T0)
    assert h.unalerted(T0) == ["U1"]
    h.alerted("U1", h.rows["U1"]["out"], T0)
    later = T0 + timedelta(days=1)
    assert h.unread("U1", "user_not_found", later) == ""
    assert h.unalerted(later) == []


def test_a_read_that_fails_leaves_the_name_and_is_said_once_a_day():
    h = Household({}, ["U1"])
    seen(h, "U1", "fan")
    assert h.unread("U1", "timeout", T0 + ROUND) == "names: could not read U1 from Slack: timeout; sessions say fan"
    assert h.unread("U1", "timeout", T0 + 2 * ROUND) == ""
    assert h.unread("U1", "timeout", T0 + timedelta(days=1)) != ""
    assert h.told("U1") == "fan" and h.rows["U1"]["out"] is None


# --- a change, held until it is due ---

def test_a_change_awaits_and_is_said_once_per_name():
    h = Household({}, ["U1", "U2"])
    seen(h, "U1", "fan")
    seen(h, "U2", "FanZhu")
    assert seen(h, "U1", "FAN", at=T0 + ROUND) == ("names: U1 is FAN to sessions from now on, the same name in "
                                                   "other capitals")
    said = [seen(h, "U2", "Fan Zhu", at=T0 + n * ROUND) for n in (1, 2)]
    assert said == ["names: U2 is Fan Zhu in Slack now; sessions say FanZhu until memory has been told", ""]
    assert h.awaiting("U2") == "Fan Zhu" and h.told("U2") == "FanZhu"
    assert seen(h, "U2", "Fan Zhu Jr", at=T0 + 3 * ROUND).startswith("names: U2 is Fan Zhu Jr in Slack now")


def test_a_change_is_due_once_two_reads_half_a_round_apart_show_it(store):
    h = Household({}, ["U1"])
    seen(h, "U1", "fan", at=T0 - ROUND)
    # the start's read, and a round's a few seconds after it
    seen(h, "U1", "Fan Zhu", at=T0)
    seen(h, "U1", "Fan Zhu", at=T0 + timedelta(seconds=5))
    assert h.due(store, T0 + timedelta(seconds=5)) is None
    seen(h, "U1", "Fan Zhu", at=T0 + ROUND)
    assert h.due(store, T0 + ROUND) == ("U1", "fan", "Fan Zhu")
    # a further change starts again
    seen(h, "U1", "Fan Z", at=T0 + 2 * ROUND)
    assert h.due(store, T0 + 2 * ROUND) is None
    seen(h, "U1", "Fan Z", at=T0 + 3 * ROUND)
    assert h.due(store, T0 + 3 * ROUND) == ("U1", "fan", "Fan Z")
    # a change back is no change
    seen(h, "U1", "fan", at=T0 + 4 * ROUND)
    seen(h, "U1", "fan", at=T0 + 5 * ROUND)
    assert h.awaiting("U1") is None and h.due(store, T0 + 5 * ROUND) is None


def test_slack_moving_on_during_a_try_awaits_a_second(store):
    # told fan -> Fan Zh while Slack moves on to Fan Zhu
    h = Household({}, ["U1"])
    seen(h, "U1", "fan", at=T0 - ROUND)
    seen(h, "U1", "Fan Zh", at=T0)
    seen(h, "U1", "Fan Zh", at=T0 + ROUND)
    h.trying("U1", "Fan Zh", "s-1", T0 + ROUND)
    seen(h, "U1", "Fan Zhu", at=T0 + ROUND + timedelta(seconds=30))
    ran(store, "s-1")
    assert h.advance("U1", "Fan Zh", "s-1", T0 + ROUND + timedelta(seconds=60))
    assert h.told("U1") == "Fan Zh" and h.awaiting("U1") == "Fan Zhu"
    seen(h, "U1", "Fan Zhu", at=T0 + 2 * ROUND)
    assert h.due(store, T0 + 2 * ROUND) == ("U1", "Fan Zh", "Fan Zhu")
    # told fan -> Fan Zhu while Slack goes back to fan
    h = Household({}, ["U1"])
    seen(h, "U1", "fan", at=T0 - ROUND)
    seen(h, "U1", "Fan Zhu", at=T0)
    seen(h, "U1", "Fan Zhu", at=T0 + ROUND)
    h.trying("U1", "Fan Zhu", "s-2", T0 + ROUND)
    seen(h, "U1", "fan", at=T0 + ROUND + timedelta(seconds=30))
    assert h.advance("U1", "Fan Zhu", "s-2", T0 + ROUND + timedelta(seconds=60))
    assert h.awaiting("U1") == "fan", "its own earlier name is still its own"
    seen(h, "U1", "fan", at=T0 + 2 * ROUND)
    assert h.due(store, T0 + 2 * ROUND) == ("U1", "Fan Zhu", "fan")


def changed(h: Household, uid="U1", old="fan", new="Fan Zhu"):
    """`old`, then `new` in two reads a round apart: a change that is due."""
    seen(h, uid, old, at=T0 - ROUND)
    seen(h, uid, new, at=T0)
    seen(h, uid, new, at=T0 + ROUND)


def test_a_try_is_written_counted_and_backed_off_before_its_session(store):
    h = Household({}, ["U1"])
    changed(h)
    now = T0 + ROUND
    h.trying("U1", "Fan Zhu", "s-1", now)
    assert h.rows["U1"]["tried"] == {"name": "Fan Zhu", "session": "s-1", "at": "2026-10-04T15:10:00+00:00",
                                     "error": "did not end", "count": 1, "next": "2026-10-04T16:10:00+00:00"}
    waits = []
    for n in range(7):
        h.failed("U1", "Fan Zhu", "claude reported an error", now)
        h.trying("U1", "Fan Zhu", f"s-{n + 2}", now)
        waits.append(datetime.fromisoformat(h.rows["U1"]["tried"]["next"]) - now)
    assert [w / timedelta(hours=1) for w in waits] == [2, 4, 8, 16, 24, 24, 24]
    # a failure for another name starts the count again
    h.failed("U1", "Fan Z", "claude reported an error", now)
    assert h.rows["U1"]["tried"]["count"] == 1 and h.rows["U1"]["tried"]["name"] == "Fan Z"


def test_a_refusal_writes_its_verdict_takes_the_count_back_and_waits_an_hour(store):
    h = Household({}, ["U1"])
    changed(h)
    now = T0 + ROUND
    h.trying("U1", "Fan Zhu", "s-1", now)
    h.failed("U1", "Fan Zhu", "busy", now, counted=False)
    t = h.rows["U1"]["tried"]
    assert (t["error"], t["count"], t["next"]) == ("busy", 0, "2026-10-04T16:10:00+00:00")
    assert h.due(store, now + timedelta(minutes=59)) is None
    assert h.due(store, now + timedelta(hours=1)) == ("U1", "fan", "Fan Zhu")


def test_a_refusal_after_failures_waits_one_hour():
    h = Household({}, ["U1"])
    changed(h)
    now = T0 + ROUND
    for n in range(3):
        h.trying("U1", "Fan Zhu", f"s-{n}", now)
        h.failed("U1", "Fan Zhu", "claude reported an error", now)
    h.trying("U1", "Fan Zhu", "s-3", now)
    h.failed("U1", "Fan Zhu", "busy", now, counted=False)
    t = h.rows["U1"]["tried"]
    assert (t["count"], datetime.fromisoformat(t["next"])) == (3, now + timedelta(hours=1))


def test_a_stop_is_due_at_once_and_one_mid_run_waits_for_memory(store):
    h = Household({}, ["U1"])
    changed(h)
    now = T0 + ROUND
    h.trying("U1", "Fan Zhu", "s-1", now)
    h.stopped("U1", now)
    assert (h.rows["U1"]["tried"]["error"], h.rows["U1"]["tried"]["count"]) == ("stopped", 0)
    assert h.due(store, now) == ("U1", "fan", "Fan Zhu")
    h.trying("U1", "Fan Zhu", "s-2", now)
    ran(store, "s-2", "cancelled")
    h.stopped("U1", now, mid_run=True)
    assert (h.rows["U1"]["tried"]["error"], h.rows["U1"]["tried"]["count"]) == ("stopped mid-run", 0)
    assert h.due(store, now + timedelta(days=2)) is None
    # whatever Slack shows meanwhile
    seen(h, "U1", "Fan Z", at=now + ROUND)
    seen(h, "U1", "Fan Z", at=now + 2 * ROUND)
    assert h.due(store, now + 2 * ROUND) is None


def test_a_stop_mid_run_takes_the_count_back_once(store):
    """However often the re-look reads it again."""
    h = Household({}, ["U1"])
    changed(h)
    now = T0 + ROUND
    for n in range(2):
        h.trying("U1", "Fan Zhu", f"s-{n}", now)
        h.failed("U1", "Fan Zhu", "claude reported an error", now)
    h.trying("U1", "Fan Zhu", "s-2", now)
    ran(store, "s-2", "cancelled")
    h.stopped("U1", now, mid_run=True)
    assert h.rows["U1"]["tried"]["count"] == 2
    # the re-look's good read, which does not advance
    h.stopped("U1", now)
    assert h.rows["U1"]["tried"]["count"] == 2


@pytest.mark.parametrize("status,error,held", [
    ("ok", "did not end", True),
    ("ok", "mem show: the store stayed busy", True),
    ("error", "did not end", True),
    ("timeout", "did not end", True),
    ("cancelled", "stopped mid-run", True),
    ("error", "claude reported an error", False),
    ("cancelled", "stopped", False),
    (None, "did not end", False),
])
def test_a_try_whose_own_run_awaits_memory_holds_the_id_for_any_name(store, status, error, held):
    h = Household({}, ["U1"])
    changed(h)
    h.trying("U1", "Fan Zhu", "s-1", T0 + ROUND)
    h.rows["U1"]["tried"]["error"] = error
    if status:
        ran(store, "s-1", status)
    # another session's run, on the same task or not, is not this try's
    ran(store, "s-0")
    seen(h, "U1", "Fan Z", at=T0 + 2 * ROUND)
    seen(h, "U1", "Fan Z", at=T0 + 3 * ROUND)
    assert (h.due(store, T0 + 3 * ROUND) is None) == held


def test_a_session_run_is_the_first_recorded_under_that_session(store):
    ran(store, "s-0")
    ran(store, "s-1", "cancelled")
    ran(store, "s-1", "ok")
    assert store.session_run("s-1")["status"] == "cancelled"
    assert store.session_run("s-2") is None


def test_a_name_another_id_tries_is_not_due(store):
    h = Household({}, ["U1", "U2"])
    changed(h)
    seen(h, "U2", "mei", at=T0)
    h.trying("U2", "Fan Zhu", "s-2", T0 + ROUND)
    assert h.due(store, T0 + ROUND) is None


def test_an_advance_to_a_name_another_id_has_been_told_is_held(store):
    h = Household({}, ["U1", "U2"])
    changed(h)
    h.trying("U1", "Fan Zhu", "s-1", T0 + ROUND)
    h.rows["U2"] = Household({}, ["U2"])._row("U2")
    h.rows["U2"]["told"] = [{"name": "fan zhu", "since": "2026-10-04T15:00:00+00:00", "session": ""}]
    assert h.advance("U1", "Fan Zhu", "s-1", T0 + ROUND) is False
    assert h.told("U1") == "fan" and h.rows["U1"]["tried"] is None
    assert h.rows["U1"]["slack"]["why"] == "Fan Zhu is U2's"


def test_what_ends_a_try_and_a_keep():
    h = Household({}, ["U1"])
    changed(h)
    now = T0 + ROUND
    for outcome in ("advance", "keep"):
        g = Household({}, ["U1"])
        changed(g)
        g.trying("U1", "Fan Zhu", "s-1", now)
        if outcome == "advance":
            g.advance("U1", "Fan Zhu", "s-1", now)
        else:
            g.keep("U1", "Fan Zhu", "s-1", "fan stays fan", True, now)
        assert g.rows["U1"]["tried"] is None, outcome
    h.trying("U1", "Fan Zhu", "s-1", now)
    h.keep("U1", "Fan Zhu", "s-1", "fan stays fan", True, now)
    # a failed try leaves the keep
    h.trying("U1", "Fan Z", "s-2", now)
    h.failed("U1", "Fan Z", "claude reported an error", now)
    assert h.rows["U1"]["kept"]["name"] == "Fan Zhu"
    # an outcome for another name replaces it, an advance ends it
    h.keep("U1", "Fan Z", "s-3", "fan stays fan", True, now)
    assert h.rows["U1"]["kept"]["name"] == "Fan Z"
    h.advance("U1", "Fan Z", "s-4", now)
    assert h.rows["U1"]["kept"] is None


@pytest.mark.parametrize("plain", [True, False])
def test_a_keep_ends_once_slack_shows_the_told_name_in_two_reads(store, plain):
    """And the kept name, seen again in two rounds, is then due."""
    h = Household({}, ["U1"])
    changed(h)
    now = T0 + ROUND
    h.trying("U1", "Fan Zhu", "s-1", now)
    h.keep("U1", "Fan Zhu", "s-1", "a second Fan Zhu" if not plain else "fan stays fan", plain, now)
    assert h.awaiting("U1") is None and h.due(store, now) is None
    assert h.state(store, "U1", now, LA)[0] is plain
    seen(h, "U1", "fan", at=now + ROUND)
    assert h.rows["U1"]["kept"] is not None, "one read is not enough"
    assert h.state(store, "U1", now + ROUND, LA)[0] is plain
    seen(h, "U1", "fan", at=now + ROUND + timedelta(seconds=NAMES_EVERY_S / 2))
    assert h.rows["U1"]["kept"] is None
    assert h.state(store, "U1", now + 2 * ROUND, LA)[0] is True
    seen(h, "U1", "Fan Zhu", at=now + 3 * ROUND)
    seen(h, "U1", "Fan Zhu", at=now + 4 * ROUND)
    assert h.due(store, now + 4 * ROUND) == ("U1", "fan", "Fan Zhu")


def test_due_is_for_allowed_ids_that_are_let_in(store):
    h = Household({}, ["U1", "U2"])
    changed(h, "U1")
    changed(h, "U9", "jane", "Jane D")
    seen(h, "U2", "", "")
    assert h.due(store, T0 + ROUND) == ("U1", "fan", "Fan Zhu")
    assert Household(h.rows, ["U2", "U9"]).due(store, T0 + ROUND) == ("U9", "jane", "Jane D")
    assert Household(h.rows, ["U2"]).due(store, T0 + ROUND) is None


def test_who_is_let_in_and_every_name_leads_to_them(store):
    h = Household({}, ["U1", "U2", "U3"])
    seen(h, "U1", "fzhu")
    h.advance("U1", "Fan", "s-1", T0)
    seen(h, "U2", "mei")
    h.unread("U2", "deleted", T0 + ROUND)
    seen(h, "U3", "", "")
    seen(h, "U9", "jane")
    assert h.told_names() == {"U1": "Fan", "U2": "mei"}, "out or not; U9 is not allowed"
    assert h.askers() == {"fzhu": "U1", "fan": "U1", "mei": "U2"}
    assert h.asker("fzhu") == "fzhu (U1, now Fan)" and h.asker("fan") == "Fan (U1)" and h.asker("jo") == "jo"
    for uid in ("U1", "U2", "U3", "U9"):
        h.save(store, uid)
    before = store.meta_starting("")
    again = Household.load(store, ["U1", "U2", "U3"])
    assert again.rows == h.rows and store.meta_starting("") == before
    assert Household.load(store, ["U4"]).told_names() == {}


# --- what doctor shows, from the store alone ---

def shown(h, store, uid, at):
    return h.state(store, uid, at, LA)


def test_doctor_shows_each_form_of_a_members_name(store):
    now = T0 + ROUND
    h = Household({}, ["U0", "U1", "U2", "U3", "U4", "U5"])
    assert shown(h, store, "U0", now) == (True, "no name yet; the first start reads Slack")
    seen(h, "U1", "fan", "Fan Zhu", at=now)
    assert shown(h, store, "U1", now) == (True, "sessions say fan, since 08:10; Slack shows fan (display name fan, "
                                                 "full name Fan Zhu), read 08:10")
    h.unread("U1", "timeout", now + ROUND)
    assert shown(h, store, "U1", now + ROUND)[1].endswith("; Slack could not be read: timeout (last read 08:10)")
    assert shown(h, store, "U1", now + ROUND)[0] is True
    h.unread("U1", "timeout", now + timedelta(days=1, minutes=1))
    assert shown(h, store, "U1", now + timedelta(days=1, minutes=1))[0] is False, "a day old"
    seen(h, "U2", "mei", at=T0)
    seen(h, "U2", "Mei Chen", at=now)
    assert shown(h, store, "U2", now) == (True, "sessions say mei, since 08:00; Slack shows Mei Chen (display name "
                                                "Mei Chen, full name empty) since 08:10, awaiting handoff")
    h.unread("U3", "user_not_visible", now)
    assert shown(h, store, "U3", now) == (False, "not let in: Slack does not show them (user_not_visible)")
    seen(h, "U4", "fan", "", at=now)
    assert shown(h, store, "U4", now) == (False, "not let in: no name Slack gives can be used (display name fan is "
                                                 "U1's; full name empty)")
    h.unread("U5", "invalid_auth", now)
    assert shown(h, store, "U5", now) == (False, "not let in: Slack could not be read: invalid_auth (last read "
                                                 "never)")
    seen(h, "U5", "jo", at=now)
    h.observe("U5", user("jo", deleted=True), now)
    assert shown(h, store, "U5", now) == (False, "Slack no longer shows them (deleted); sessions say jo, as before")
    seen(h, "U2", "dad", at=now)
    seen(h, "U4", "", "ann", at=now)
    seen(h, "U4", "dad", at=now)
    assert shown(h, store, "U4", now) == (False, "sessions say ann, since 08:10; no name Slack gives can be used "
                                                 "(display name dad is U2's; full name empty)")
    h.observe("U0", user("Wanda", "Wanda Li"), now)
    assert shown(h, store, "U0", now)[1].endswith(
        "(display name Wanda, full name Wanda Li), read 08:10; display name Wanda is the name memory has for "
        "wanda's own node")


@pytest.mark.parametrize("status,error,ok,said", [
    (None, "did not end", False, ": did not end (running now, or the daemon has not started since)"),
    # its run recorded, and memory not yet read
    ("ok", "did not end", False, ": did not end (running now, or the daemon has not started since)"),
    ("timeout", "did not end", False, ": did not end (running now, or the daemon has not started since)"),
    (None, "cut short", False, ": cut short; next after 09:10"),
    (None, "stopped", True, ": the last try was stopped; next after 08:10"),
    (None, "busy", True, ": the last try was refused (busy); next after 09:10"),
    ("error", "claude reported an error", False, ": 1 try, the last at 08:10: claude reported an error; "
                                                 "next after 09:10"),
    ("ok", "mem show: the store stayed busy", False, ": memory's answer could not be read (session s-1): "
                                                    "mem show: the store stayed busy; read again at each refresh"),
    ("cancelled", "stopped mid-run", False, ": stopped mid-run (session s-1); memory is read again at each "
                                            "refresh"),
])
def test_doctor_shows_each_form_of_a_try(store, status, error, ok, said):
    h = Household({}, ["U1"])
    changed(h)
    now = T0 + ROUND
    h.trying("U1", "Fan Zhu", "s-1", now)
    if error == "busy":
        h.failed("U1", "Fan Zhu", "busy", now, counted=False)
    elif error in ("stopped", "stopped mid-run"):
        h.stopped("U1", now, mid_run=error == "stopped mid-run")
    elif error != "did not end":
        h.failed("U1", "Fan Zhu", error, now)
    if status:
        ran(store, "s-1", status)
    tasks = store._query("SELECT COUNT(*) AS n FROM tasks")[0]["n"]
    assert shown(h, store, "U1", now) == (ok, "sessions say fan, since 07:50; Slack shows Fan Zhu (display name "
                                              "Fan Zhu, full name empty) since 08:00, awaiting handoff" + said)
    assert store._query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == tasks == 0, "doctor writes no task"
    # a try for a name Slack has since moved on from is still shown
    seen(h, "U1", "Fan Z", at=now)
    assert f"awaiting handoff; a handoff of Fan Zhu{said}" in shown(h, store, "U1", now)[1]


def test_doctor_shows_a_keep(store):
    h = Household({}, ["U1"])
    changed(h)
    now = T0 + ROUND
    h.keep("U1", "Fan Zhu", "s-1aaaaaaaa", "Fan Zhu is a cousin; fan stays fan", True, now)
    assert shown(h, store, "U1", now) == (True, "sessions say fan, since 07:50; Slack shows Fan Zhu (display name "
                                                "Fan Zhu, full name empty), which memory keeps as fan (session "
                                                "s-1aaaaa): Fan Zhu is a cousin; fan stays fan")
    h.keep("U1", "Fan Zhu", "s-1aaaaaaaa", "two people are Fan Zhu now", False, now)
    assert shown(h, store, "U1", now) == (False, "sessions say fan, since 07:50; Slack shows Fan Zhu (display name "
                                                 "Fan Zhu, full name empty): memory did not take it as theirs alone "
                                                 "(session s-1aaaaa): two people are Fan Zhu now")


# --- what memory says about a name, and what it means ---

def person(pid, name, made='"s-one"', body=""):
    """`mem show` of one person, as the build printed it."""
    return (f"{pid}\n---\nname: {name}\nsummary: \"the member\"\ncreated: \"2026-11-20\"\n"
            + (f"made: {made}\n" if made else "")
            + f"last_seen: \"2026-11-20\"\naliases: [{name}]\ntags: [\"person\"]\n---\n\n{body}\n")


RENAMED = person("person:d1690d", '"Fan Zhu"', body="~~was named: fan~~ (renamed 2026-11-20)\n")
# summaries printed as written: "; ", " (", another node's id, odd brackets
TWO = ("('fan' is more than one node: person:44ef1b (a cousin; person:7e9623 (mei) is his aunt); person:854568 "
       "(the member). An id says which.)")
THREE = ("('mei' is more than one node: person:3a74c8 (dad :) of two); person:4eb1b4 (z) w); person:7e20eb (x (y). "
         "An id says which.)")
UNSUMMED = "('jo' is more than one node: person:047f7c (jo); person:b4e870 (the neighbour). An id says which.)"
EVENTS = ("('Mei' is more than one node: event:2026-11-20-5554f0 (Mei); event:2026-11-20-5557ce (Mei). An id "
          "says which.)")
# a name's first word read as a mistyped id, in an empty vault and beside a person
MISTYPED = ["fan_zhu", "fan/zhu", "Mei, Chen", "dad2", "fan,zhu", "ab12", "{fan}", "<fan>", "fan*", "$fan",
            "2026-10 fan", ":", "/", "a1b2c3 x"]
NOBODY_YET = "there are no people yet, and `mem search` finds by other words)"
LISTED = "people/CLAUDE.md lists the people, and `mem search` finds by other words)"


def test_one_person_is_read_with_its_name_and_the_session_that_made_it():
    assert found(0, person("person:17b218", '"fan"')) == Found(("person:17b218",), "fan", "s-one",
                                                                 "person:17b218 (fan)")
    assert found(0, RENAMED) == Found(("person:d1690d",), "Fan Zhu", "s-one", "person:d1690d (Fan Zhu)")
    # an older file's unquoted values, and one made by hand, with no session
    assert found(0, person("person:5e6f70", "Mei Chen", made="s-old")).made == "s-old"
    assert found(0, person("person:5e6f70", "Mei Chen")).label == "Mei Chen"
    assert found(0, person("person:5e6f70", '"mei"', made="")).made == ""
    assert found(0, person("person:5e6f70", '"Mei \\"M\\" Chen"')).label == 'Mei "M" Chen'


@pytest.mark.parametrize("out,ids", [
    (TWO, ("person:44ef1b", "person:854568")),
    (THREE, ("person:3a74c8", "person:4eb1b4", "person:7e20eb")),
    (UNSUMMED, ("person:047f7c", "person:b4e870")),
])
def test_several_people_are_read_to_their_own_ids_alone(out, ids):
    assert found(1, out).ids == ids


@pytest.mark.parametrize("code,out", [
    (1, EVENTS),
    (1, "('Pip' is more than one node: thing:4d5e6f (Pip); thing:5e6f70 (Pip). An id says which.)"),
    (0, "thing:bae83f\n---\nname: \"dryer\"\n---\n"),
    (1, f"(no person is named 'Fan Zhu'; {NOBODY_YET}"),
    (1, f"(no person is named 'Fan Zhu'; {LISTED}"),
    (1, "('person' is a kind, with no id or name after it; there are no people yet)"),
    (1, "('person' is a kind, with no id or name after it; people/CLAUDE.md lists them)"),
    (1, "(no node for '')"),
] + [(1, f"(no person 'person:{n}'; {tail}") for n in MISTYPED for tail in (NOBODY_YET, LISTED)])
def test_no_person_is_read_from_every_answer_that_finds_none(code, out):
    assert found(code, out).ids == ()


@pytest.mark.parametrize("code,out", [
    (1, "(the store could not be held for this call: it stayed busy for 90 s; nothing was read or written)"),
    (2, "error: unexpected argument '--x' found"),
    (0, "ok person:17b218"),
    (1, "person:17b218\n---\n"),
])
def test_a_busy_vault_or_an_unknown_answer_raises(code, out):
    with pytest.raises(ValueError):
        found(code, out)


def one(pid, label, made="s-x"):
    return Found((pid,), label, made)


NONE = Found()


@pytest.mark.parametrize("by_old,by_new,ran_ok,outcome", [
    # fan renamed Fan Zhu, a cousin also fan or not
    (one("person:aaaaaa", "Fan Zhu"), one("person:aaaaaa", "Fan Zhu"), True, "advance"),
    (Found(("person:aaaaaa", "person:cccccc")), one("person:aaaaaa", "fan zhu"), True, "advance"),
    # memory never knew him, and this session made him; after a run ok, neither name finds anyone
    (NONE, one("person:bbbbbb", "Fan Zhu", "s-1"), False, "advance"),
    (NONE, NONE, True, "advance"),
    (NONE, NONE, False, "keep"),
    # made by another session while memory never knew him: a namesake already there
    (NONE, one("person:bbbbbb", "Fan Zhu", "s-0"), True, "keep, alerted"),
    # fan left as he was; fan and a cousin both fan; relabelled Dad
    (one("person:aaaaaa", "fan"), NONE, True, "keep"),
    (Found(("person:aaaaaa", "person:cccccc")), NONE, True, "keep"),
    (one("person:aaaaaa", "Dad"), NONE, True, "keep"),
    # a second Fan Zhu beside fan, or Fan Zhu already someone else
    (one("person:aaaaaa", "fan"), one("person:bbbbbb", "Fan Zhu"), True, "keep, alerted"),
])
def test_what_memorys_answer_means(by_old, by_new, ran_ok, outcome):
    assert settle("fan", "Fan Zhu", by_old, by_new, "s-1", ran_ok) == outcome


def test_a_change_back_is_read_the_same_way():
    member = one("person:aaaaaa", "Fan Zhu")
    # declined: the member keeps Fan Zhu, and fan finds him by his struck line
    assert settle("Fan Zhu", "fan", member, member, "s-2", True) == "keep"
    # beside a cousin fan, kept as Fan Zhu or renamed back
    both = Found(("person:aaaaaa", "person:cccccc"))
    assert settle("Fan Zhu", "fan", member, both, "s-2", True) == "keep, alerted"
    assert settle("Fan Zhu", "fan", one("person:aaaaaa", "fan"), both, "s-2", True) == "keep, alerted"
    # renamed back, alone
    assert settle("Fan Zhu", "fan", one("person:aaaaaa", "fan"), one("person:aaaaaa", "fan"), "s-2", True) == "advance"


def test_an_ambiguitys_first_candidate_counts():
    """The old name finds the member and a cousin; the new one finds a stray,
    listed first, and the member. Dropping the first candidate would make it
    a plain keep."""
    by_old = found(1, "('fan' is more than one node: person:854568 (the member); person:44ef1b (a cousin). An id "
                      "says which.)")
    by_new = found(1, "('Fan Zhu' is more than one node: person:9b1e0a (made in a reply); person:854568 (the "
                      "member). An id says which.)")
    assert by_new.ids == ("person:9b1e0a", "person:854568")
    assert settle("fan", "Fan Zhu", by_old, by_new, "s-1", True) == "keep, alerted"
    assert settle("fan", "Fan Zhu", by_old, Found(by_new.ids[1:]), "s-1", True) == "keep"


def test_a_cousin_once_fan_relabelled_with_the_new_name_is_advanced_onto():
    """Today's answer, pinned so that a change of rule is deliberate: a
    person once called by the member's old name, and relabelled with exactly
    the new one before the change, is among the old name's people."""
    by_old = found(1, "('fan' is more than one node: person:854568 (the member); person:44ef1b (Fan Zhu). An id "
                      "says which.)")
    by_new = found(0, person("person:44ef1b", '"Fan Zhu"', body="~~was named: fan~~ (renamed 2026-11-01)\n"))
    assert settle("fan", "Fan Zhu", by_old, by_new, "s-1", True) == "advance"
