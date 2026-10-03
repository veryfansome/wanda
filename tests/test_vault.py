"""A memory session as the product sets it up: the frames and the text in
them, the copies of the lab's prompt, schema, tools and date paragraph, the
parser reading every frame back, who may be in a conversation, the
environment, the report, and the vault's setup, check and snapshots (against
a stand-in `mem`)."""

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wanda import clock, vault
from wanda.config import Config
from wanda.household import Household, flaw
from wanda.transcript import plain

ROOT = Path(__file__).resolve().parent.parent
LAB = ROOT / "lab" / "harness" / "src"
LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 1, 16, 40, tzinfo=LA)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("WANDA_"):
            monkeypatch.delenv(key, raising=False)


def rust_str(lit: str) -> str:
    """A Rust string literal's text: a backslash at a line's end joins the
    next line without its indent."""
    return re.sub(r"\\\n\s*", "", lit).replace("\\n", "\n").replace('\\"', '"')


def lab_source(name: str) -> str:
    path = LAB / name
    if not path.exists():
        pytest.skip("no lab in this checkout")
    return path.read_text()


# --- the copies of the lab's texts ---

def test_the_prompt_is_the_labs():
    lit = re.search(r'pub const PROMPT: &str = "(.*?)";', lab_source("arrival.rs"), re.DOTALL)
    assert rust_str(lit.group(1)).replace("{mem}", "mem") == vault.PROMPT


def test_the_dm_and_thread_frames_are_the_labs():
    """The lab's frames, except that each earlier line says when it was sent."""
    src = lab_source("arrival.rs")
    block = re.search(r"const ARRIVALS_TO_ME.*?= \[(.*?)\];", src, re.DOTALL).group(1)
    probes = [rust_str(s) for s in re.findall(r'\("\w+", "(.*?)"\)', block, re.DOTALL)]
    said = "one line\nand a second"
    assert vault.arrival_text("dm", "probe", said, [], []) == probes[0]
    assert vault.arrival_text("thread", "probe", said, ["probe", "other"], []) == probes[2]
    assert vault.arrival_text("thread", "probe", said, ["probe", "other"],
                              [("09:10", "probe", "earlier"), ("09:11", "me", "reply")]) == probes[3].replace(
        "    probe: earlier\n    me: reply", "    09:10 probe: earlier\n    09:11 me: reply")


def test_tools_schema_placeholders_and_date_paragraph_are_the_labs():
    src = lab_source("session.rs")
    assert f'"--tools", "{vault.TOOLS}"' in src and f'"--allowedTools", "{vault.TOOLS}"' in src
    for field, spec in vault.SCHEMA["properties"].items():
        assert f'"{field}": {{' in src and f'"description": "{spec["description"]}"' in src
    lab_words = re.search(r"const PLACEHOLDER: \[&str; \d+\] = \[(.*?)\];", src, re.DOTALL).group(1)
    assert tuple(re.findall(r'"(.*?)"', lab_words)) == vault.PLACEHOLDER
    system = rust_str(re.search(r'let system = format!\(\s*"(.*?)"\);', src, re.DOTALL).group(1))
    assert system.startswith("{ANCHOR}\n\nToday is {day}, {date}. ")
    paragraph = vault.date_paragraph(NOW)
    first = "Today is Thursday, 2026-10-01, and it is 16:40 here (PDT) as this session begins. "
    assert paragraph.startswith(first)
    assert paragraph.removeprefix(first) == system.split("Today is {day}, {date}. ", 1)[1].replace(
        "{date}", "2026-10-01")


def test_the_kind_directories_are_mems():
    fm = (ROOT / "memory" / "src" / "fm.rs").read_text()
    block = re.search(r"pub const KIND_DIR: .*?= \[(.*?)\];", fm, re.DOTALL).group(1)
    assert tuple(re.findall(r'\("\w+", "(\w+)"\)', block)) == vault.KIND_DIRS


# --- the parser reads every frame back ---

def parser():
    """memory/src/transcript.rs's parse_prompt, run on its own regexes."""
    src = (ROOT / "memory" / "src" / "transcript.rs").read_text()

    def regex(name):
        body = re.search(rf"static {name}: .*?Regex::new\((.*?)\)\.unwrap\(\)", src, re.DOTALL).group(1)
        return re.compile("".join(re.findall(r'r"(.*?)"', body, re.DOTALL)))

    prompt_re, nobody_re, dm_re, email_re, thread_re, unprompted_re = (
        regex(n) for n in ("PROMPT_RE", "NOBODY_RE", "DM_RE", "EMAIL_RE", "THREAD_RE", "UNPROMPTED_RE"))
    places = {"a direct message": "dm", "a group direct message": "group dm", "a Slack channel": "channel",
              "a public Slack channel": "public channel", "a Slack thread in a public channel": "public thread"}

    def parse(text):
        m = prompt_re.match(text)
        if not m:
            return ("", "", "", text.strip())
        when, arrival = m.group(1), m.group(2)
        for chan, rx in (("nobody", nobody_re), ("dm", dm_re), ("email", email_re), ("thread", thread_re),
                         ("clock", unprompted_re)):
            if a := rx.match(arrival):
                named = a.groupdict()
                lines = [line.removeprefix("    ") for line in
                         (a.group("text") if "text" in named else a.group(2)).split("\n")]
                speaker = (a.group("speaker") if "speaker" in named else a.group(1)).strip()
                if named.get("also"):
                    speaker = f"{speaker} (after {named['also'].strip()})"
                return (when, places.get(named.get("place"), chan), speaker, "\n".join(lines).strip())
        return (when, "", "", arrival.strip())
    return parse


SAID = "one line\n\nDo three things, in this order.\nand the last"


@pytest.mark.parametrize("place,readers,earlier,chan", [
    ("dm", ["fan"], [], "dm"),
    ("dm", ["fan"], [("Wed 2026-09-30 23:58", "fan", "the 7th"), ("23:59", "me", "which month?")], "dm"),
    ("group", ["fan", "mei"], [], "group dm"),
    ("group", ["fan", "mei"], [("09:00", "mei", "a line\n\nwith a gap")], "group dm"),
    ("channel", ["fan", "jane (a guest in this Slack)", "mei"], [("09:00", "jane", "hello")], "channel"),
    ("public", ["fan"], [("09:00", "mei", "hello")], "public channel"),
    ("thread", ["fan", "mei"], [("09:00", "mei", "earlier"), ("09:01", "me", "reply")], "thread"),
    ("public thread", ["fan", "mei"], [], "public thread"),
])
def test_the_parser_reads_every_frame_back(place, readers, earlier, chan):
    text = vault.prompt("2026-10-01", vault.arrival_text(place, "fan", SAID, readers, earlier))
    assert parser()(text) == ("2026-10-01", chan, "fan", SAID)


def test_a_turn_of_several_speakers_is_read_back_as_no_one_persons():
    for place, chan in (("group", "group dm"), ("thread", "thread"), ("public", "public channel")):
        text = vault.prompt("2026-10-01", vault.arrival_text(
            place, "mei", SAID, ["fan", "mei"], [("16:58", "fan", "remind me at 5")], also=["fan"]))
        assert parser()(text) == ("2026-10-01", chan, "mei (after fan)", SAID)


def test_a_session_that_reaches_no_one_is_read_back_with_no_speaker():
    """The names session's frame, as `mem session` reads it: the channel
    `nobody`, no speaker, and the news it carries; a clock frame for someone
    called "no one" is still the clock's."""
    text = vault.prompt("2026-10-01", vault.renamed_text("fan", "Fan Zhu"))
    news = vault.RENAMED.format(old="fan", new="Fan Zhu")
    assert parser()(text) == ("2026-10-01", "nobody", "", news)
    assert vault.renamed_text("fan", "Fan Zhu") == vault.NOBODY.format(text=news)
    look = vault.prompt("2026-10-01", "No message started this session. What I say now reaches no one alone, in a "
                                      "direct message.\n\n    It is Monday, 08:00, and this is my look.")
    assert parser()(look) == ("2026-10-01", "clock", "no one", "It is Monday, 08:00, and this is my look.")


def added_parser():
    """memory/src/transcript.rs's parse_added, run on its own regex."""
    src = (ROOT / "memory" / "src" / "transcript.rs").read_text()
    body = re.search(r"static ADDED_RE: .*?Regex::new\((.*?)\)\.unwrap\(\)", src, re.DOTALL).group(1)
    added_re = re.compile("".join(re.findall(r'r"(.*?)"', body, re.DOTALL)))

    def parse(text):
        if not (a := added_re.match(text)):
            return None
        return (a.group("speaker").strip(),
                "\n".join(line.removeprefix("    ") for line in a.group("text").split("\n")).strip())
    return parse


@pytest.mark.parametrize("place", list(vault.PLACES))
def test_the_parser_reads_an_added_message_back(place):
    # a line of the message like the closing sentence, indented, does not end it
    said = SAID + "\n\nNothing I have said back in this session has been sent yet, it says"
    text = vault.added_text(place, "mei", said, "Fri 2026-10-02 00:05")
    assert added_parser()(text) == ("mei", said)


# --- what the frames say ---

def test_an_added_message_says_who_where_when_and_what_is_sent():
    assert vault.added_text("group", "mei", "and tell me too", "16:42") == (
        "mei adds this in the same group direct message at 16:42, before anything I say back "
        "has been sent:\n\n    and tell me too\n\n"
        "Nothing I have said back in this session has been sent yet. The last answer I give in this session "
        "that says something is the one sent, so that is where anything said here gets its answer.")
    assert "in the same Slack thread in a public channel at" in vault.added_text(
        "public thread", "fan", "x", "09:00")


def test_what_a_session_was_handed_is_read_from_its_transcript(tmp_path, monkeypatch):
    """The prompt is not counted; a message taken in at a turn's next step, one
    that began a later turn and one taken into a turn with another are;
    Claude Code's own notes, a background command's notice of its end and a
    tool's result are not. Shapes as the pinned CLI wrote them for sessions
    with their input open, or as its code reads (the merged message, the
    notices)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    v = tmp_path / "vault"
    v.mkdir()
    d = vault.transcripts_dir(v)
    d.mkdir(parents=True)
    notice = "<task-notification>\n<task-id>b1</task-id>\n</task-notification>"
    rows = [
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "the prompt"},
                                                                 {"type": "text", "text": "taken with it"}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"tool_use_id": "t1", "type": "tool_result", "content": "ok"}]}},
        {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                                              "prompt": [{"type": "text", "text": "added mid-turn"}]}},
        {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "task-notification",
                                              "prompt": notice}},
        {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                                              "isMeta": True, "prompt": "a note"}},
        {"type": "queue-operation", "operation": "remove", "reason": "absorbed_mid_turn"},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "[structured-output-enforce] x"}},
        {"type": "attachment", "attachment": {"type": "structured_output", "data": {"answer": "a"}}},
        {"type": "user", "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": notice}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "a later turn"},
                                                                 {"type": "text", "text": "and another"}]}},
    ]
    (d / "s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    assert vault.handed(v, "s1") == ["taken with it", "added mid-turn", "a later turn", "and another"]
    assert vault.handed(v, "s2") is None


def test_frames_name_who_reads_and_what_came_before():
    assert vault.arrival_text("group", "fan", "hi", ["fan", "mei"], []) == (
        "In a group direct message that fan, mei and I read. Everyone in it sees what I say "
        "there.\n\nfan says:\n\n    hi")
    assert vault.arrival_text("dm", "mei", "yes", ["mei"], [("Wed 2026-09-30 23:58", "me", "Shall I tell fan?")]) == (
        "In a direct message that mei and I read.\n\nThe conversation so far:\n\n"
        "    Wed 2026-09-30 23:58 me: Shall I tell fan?\n\nmei now says:\n\n    yes")
    assert vault.arrival_text("public", "fan", "hi", ["fan"], []) == (
        "In a public Slack channel that anyone in this Slack can read; fan and I are in it."
        "\n\nfan says:\n\n    hi")
    # a turn that takes messages from both names both
    assert vault.arrival_text("group", "mei", "ok", ["fan", "mei"], [("16:58", "fan", "remind me at 5")],
                              also=["fan"]).endswith(
        "The conversation so far:\n\n    16:58 fan: remind me at 5\n\nmei now says, after fan:\n\n    ok")


def test_plain_turns_slack_markup_into_what_was_written():
    names = {"U1": "fan", "U2": "mei", "UBOT": "wanda"}
    assert plain("<@UBOT> tell <@U2|mei.z> the plumber is Tue &amp; Wed", names) == \
        "@wanda tell @mei the plumber is Tue & Wed"
    assert plain("see <https://x.example/a|the form> or <https://x.example/b>", names) == \
        "see the form (https://x.example/a) or https://x.example/b"
    assert plain("<!here> in <#C1|kitchen>, mail <mailto:a@b.example|a@b.example>", names) == \
        "@here in #kitchen, mail a@b.example"
    assert plain("a &lt;b&gt; and &amp;copy;", names) == "a <b> and &copy;"


def test_readers():
    users = {"U1": {}, "U2": {"is_restricted": True}, "U3": {"is_restricted": True}, "B1": {"is_bot": True},
             "U4": {"deleted": True}}
    told = {"U1": "fan", "U2": "mei"}
    named = told | {"U3": "jane", "U4": "old"}
    # mei's account being a guest one does not make her a guest in her own conversations
    got = vault.readers(["U3", "U2", "U1", "B1", "U4", "UBOT"], users, named, frozenset({"UBOT"}), told)
    assert got == ["fan", "jane (a guest in this Slack)", "mei"]
    crowd = {f"X{i}": f"p{i:02d}" for i in range(20)}
    got = vault.readers(list(crowd) + ["U1"], {u: {} for u in crowd}, crowd | named, frozenset(), told)
    assert got == ["fan", "20 others"]
    with pytest.raises(LookupError, match="U9"):
        vault.readers(["U1", "U9"], users, named, frozenset(), told)
    # a member is named by the name sessions are told, with no Slack record
    # needed, and is never a guest
    assert vault.readers(["U2"], {}, told, frozenset(), told) == ["mei"]


AT = datetime(2026, 10, 1, 16, 40, tzinfo=timezone.utc)


def household() -> Household:
    """fan (U1), who was fzhu before; mei (U2), whose change to Mei Chen
    memory kept the earlier name for, and whose Slack still shows it; and U3,
    allowed, whom Slack has given no name sessions can use."""
    h = Household({}, ["U1", "U2", "U3"])
    h.observe("U1", {"profile": {"display_name": "fzhu"}}, AT)
    h.advance("U1", "fan", "s-1", AT)
    h.observe("U1", {"profile": {"display_name": "fan"}}, AT)
    h.observe("U2", {"profile": {"display_name": "mei"}}, AT)
    h.observe("U2", {"profile": {"display_name": "Mei Chen"}}, AT)
    h.keep("U2", "Mei Chen", "s-2", "mei stays mei", True, AT)
    h.observe("U3", {"profile": {}}, AT)
    return h


def test_a_member_is_called_by_the_name_sessions_are_told():
    h = household()
    users = {"U1": {"profile": {"display_name": "fzhu"}}, "U2": {"profile": {"display_name": "Mei Chen"}}}
    named = vault.names(users, h.told_names(), h.namesakes())
    assert named == {"U1": "fan", "U2": "mei"}, "whatever Slack shows for them now"


def test_anyone_else_called_by_a_members_name_is_marked():
    """Earlier names included, and a change memory kept the earlier name for
    while the member's Slack shows it; an allowed id not let in is anyone
    else. Her own bot user is never marked: no member is told her name."""
    h = household()
    users = {"U7": {"profile": {"display_name": "FZHU"}}, "U8": {"profile": {"display_name": "Mei Chen"}},
             "U3": {"profile": {"real_name": "fan"}}, "U9": {"profile": {"display_name": "jane"}},
             "UBOT": {"is_bot": True, "profile": {"display_name": "wanda"}}}
    named = vault.names(users, h.told_names(), h.namesakes())
    assert named == {"U7": "FZHU" + vault.NAMESAKE, "U8": "Mei Chen" + vault.NAMESAKE,
                     "U3": "fan" + vault.NAMESAKE, "U9": "jane", "UBOT": "wanda", "U1": "fan", "U2": "mei"}
    # once mei's Slack no longer shows the kept name, it marks no one
    h.observe("U2", {"profile": {"display_name": "mei"}}, AT)
    assert "Mei Chen" not in h.namesakes() and "mei chen" not in h.namesakes()
    assert vault.names({"U8": users["U8"]}, h.told_names(), h.namesakes()) == {
        "U8": "Mei Chen", "U1": "fan", "U2": "mei"}
    # with spaces `mem` would collapse
    assert vault.names({"U7": {"profile": {"display_name": " fan  "}}}, h.told_names(), h.namesakes())["U7"] == (
        " fan  " + vault.NAMESAKE)


def test_a_removed_member_is_not_marked_for_its_own_name():
    """Its names hold for no one else, and are its own."""
    h = Household({}, ["U1", "U9"])
    h.observe("U1", {"profile": {"display_name": "fan"}}, AT)
    h.observe("U9", {"profile": {"display_name": "jane"}}, AT)
    removed = Household(h.rows, ["U1"])
    named = vault.names({"U9": {"profile": {"display_name": "jane"}}}, removed.told_names(), removed.namesakes())
    assert named["U9"] == "jane"


def test_her_mention_reads_as_her_name_in_every_frame():
    h = household()
    users = {"U1": {}, "UBOT": {"is_bot": True, "profile": {"display_name": "wanda"}}}
    named = vault.names(users, h.told_names(), h.namesakes())
    said = vault.message_text("<@UBOT> the plumber is Tuesday", None, named)
    assert said == "@wanda the plumber is Tuesday"
    assert "fan says to me, in a direct message:\n\n    @wanda the plumber" in vault.arrival_text(
        "dm", named["U1"], said, ["fan"], [])
    assert vault.added_text("dm", named["U1"], said, "16:41").startswith(
        "fan adds this in the same direct message at 16:41, before anything I say back has been sent:\n\n"
        "    @wanda the plumber is Tuesday")


# Names a person may give themselves in Slack, each rendered into every frame
# a session is handed and read back by the copies of mem's own parser.
AWKWARD = ["Fan Zhu", "-fan", "李梅", "fan says", "Now Mei", "Mei now", "fan_zhu", "fan/zhu",
           "fan (after mei", "fan (then mei"]


def frames(name: str) -> dict[str, str]:
    """Every shape a frame names its speaker in, by where it is."""
    out = {}
    for place in vault.PLACES:
        for earlier in ([], [("16:38", "mei", "earlier")]):
            if place == "dm" and not earlier:
                continue
            out[f"{place}, {len(earlier)} earlier"] = vault.arrival_text(place, name, SAID, [name, "mei"], earlier)
    out["dm alone"] = vault.arrival_text("dm", name, SAID, [], [])
    out["clock"] = clock.Wake("clock:morning:U1", "U1", "It is Thursday, 08:00.").arrival(name)
    return out


def test_a_usable_name_reads_back_whole_from_every_frame():
    parse = parser()
    for name in AWKWARD:
        if flaw(name):
            continue
        for shape, arrival in frames(name).items():
            got = parse(vault.prompt("2026-10-01", arrival))
            assert got[2] == name, (name, shape, got)
        assert added_parser()(vault.added_text("group", name, SAID, "16:41")) == (name, SAID), name


def test_a_name_the_parser_misreads_is_not_used():
    """A final " now" is cut off where a frame has no earlier lines, and
    " (after " or " (then " reads as more than one speaker."""
    parse = parser()
    assert [n for n in AWKWARD if flaw(n)] == ["Mei now", "fan (after mei", "fan (then mei"]
    shapes = frames("Mei now")
    misread = sorted(shape for shape, arrival in shapes.items()
                     if parse(vault.prompt("2026-10-01", arrival))[2] != "Mei now")
    assert "group, 0 earlier" in misread and "thread, 0 earlier" in misread
    assert all(shape.endswith("0 earlier") for shape in misread), misread
    src = (ROOT / "memory" / "src" / "transcript.rs").read_text()
    body = re.search(r"pub fn one_speaker\(.*?\{(.*?)\n\}", src, re.DOTALL).group(1)
    several = re.findall(r'contains\("(.*?)"\)', body)
    assert sorted(several) == [" (after ", " (then "]
    for name in ("fan (after mei", "fan (then mei"):
        assert any(s in name for s in several)


def test_who_may_be_in_a_conversation():
    own = frozenset({"UBOT", "BBOT"})
    assert vault.outsiders(["U1", "U2", "UBOT"], own, ["U1", "U2"]) == []
    assert vault.outsiders(["U1", "U3", "UBOT"], own, ["U1", "U2"]) == ["U3"]
    people = [{"id": "U1"}, {"id": "U2"}, {"id": "UBOT", "is_bot": True}, {"id": "USLACKBOT"},
              {"id": "U5", "deleted": True}, {"id": "U6", "is_restricted": True}, {"id": "U7"}]
    assert vault.full_members(people) == ["U1", "U2", "U7"]


def test_where():
    assert [vault.where({"channel_type": t, "in_thread": th}) for t, th in [
        ("im", False), ("mpim", False), ("group", False), ("channel", False), ("group", True),
        ("channel", True), ("im", True)]] == [
        "dm", "group", "channel", "public", "thread", "public thread", "thread"]


def test_earlier_lines_say_when_and_reach_back_twelve_hours():
    own = frozenset({"UBOT"})
    named = {"U1": "fan"}
    at = NOW.timestamp()
    msgs = [{"ts": f"{at - 13 * 3600}", "user": "U1", "text": "this morning"},
            {"ts": f"{at - 18 * 3600}", "user": "U1", "text": "last night"},
            {"ts": f"{at - 120}", "user": "U1", "text": "remind me <@UBOT>"},
            {"ts": f"{at - 60}", "user": "UBOT", "bot_id": "B", "text": "which day?"},
            {"ts": f"{at - 30}", "subtype": "channel_join", "user": "U1", "text": "joined"},
            {"ts": f"{at}", "user": "U1", "text": "this one"},
            {"ts": f"{at + 60}", "user": "U1", "text": "a later one"}]
    msgs.sort(key=lambda m: float(m["ts"]))
    assert vault.earlier(msgs, f"{at}", "dm", named, own, NOW) == [
        ("16:38", "fan", "remind me @UBOT"), ("16:39", "me", "which day?")]
    late = NOW.replace(hour=0, minute=5)
    got = vault.earlier(msgs, f"{at}", "dm", named, own, late.replace(day=2))
    assert got[0][0] == "Thu 2026-10-01 16:38", "another day carries its weekday and date"
    assert [w for _, w, _ in vault.earlier(msgs, f"{at}", "thread", named, own, NOW)] == ["fan", "fan", "fan", "me"]
    many = [{"ts": f"{at - 100 + i}", "user": "U1", "text": str(i)} for i in range(30)]
    assert len(vault.earlier(many, f"{at}", "dm", named, own, NOW)) == vault.EARLIER


def test_the_harness_alerts_and_failure_notes_are_never_earlier_lines():
    """An alert is for people, and a failure note carries Claude Code's
    words: posted with the harness's marks, neither is shown to a session as
    something she said, in a thread or out of one, whoever posted it and
    whether or not her own ids are known."""
    at = NOW.timestamp()
    alert = {"event_type": vault.ALERT_EVENT, "event_payload": {}}
    note = {"event_type": vault.NOTE_EVENT, "event_payload": {}}
    msgs = [{"ts": f"{at - 90}", "user": "UBOT", "bot_id": "BBOT", "text": "⚠️ a vault snapshot failed",
             "metadata": alert},
            {"ts": f"{at - 80}", "user": "UBOT", "bot_id": "BBOT", "text": "⚠️ my run failed: You've hit your limit",
             "metadata": note},
            {"ts": f"{at - 60}", "user": "UBOT", "bot_id": "BBOT", "text": "Which one?"},
            {"ts": f"{at - 30}", "user": "U9", "text": "an app's post", "metadata": alert}]
    for own in (frozenset({"UBOT", "BBOT"}), frozenset()):
        for place in ("dm", "thread"):
            assert [t for _, _, t in vault.earlier(msgs, f"{at}", place, {"U9": "jane"}, own, NOW)] == [
                "Which one?"]


def test_her_answers_after_the_message_are_what_came_before_it():
    """A line added while a session ran is framed after that session's answer
    was posted: the answer stays, in its place, and every later line of
    anyone else's is left for the next turn."""
    own = frozenset({"UBOT", "BBOT"})
    at = NOW.timestamp()
    msgs = [{"ts": f"{at - 30}", "user": "U1", "text": "what time is the dentist?"},
            {"ts": f"{at}", "user": "U1", "text": "and remind me the day before"},
            {"ts": f"{at + 5}", "user": "UBOT", "bot_id": "BBOT", "text": "10:30."},
            {"ts": f"{at + 6}", "user": "UBOT", "bot_id": "BBOT", "text": "⚠️ my run failed: x",
             "metadata": {"event_type": vault.NOTE_EVENT, "event_payload": {}}},
            {"ts": f"{at + 8}", "user": "U2", "text": "a later line of mei's"}]
    for place in ("dm", "thread"):
        assert vault.earlier(msgs, f"{at}", place, {"U1": "fan", "U2": "mei"}, own, NOW) == [
            ("16:39", "fan", "what time is the dentist?"), ("16:40", "me", "10:30.")]


def test_files_are_named():
    assert vault.message_text("", ["receipt.pdf"], {}) == "[attached: receipt.pdf]"
    assert vault.message_text("here", [{"name": "a.png"}], {}) == "here [attached: a.png]"


# --- environment and report ---

def cfg(tmp_path, **kw) -> Config:
    return Config(_env_file=None, data_dir=tmp_path / "home.d", tz="America/Los_Angeles", **kw)


def test_session_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WANDA_SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    c = cfg(tmp_path)
    # 17:20 in Los Angeles is already the 2nd in UTC
    env = vault.session_env(c, "sid-1", NOW.replace(hour=17, minute=20))
    assert not [k for k in env if k.startswith("WANDA_")]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    # background tasks on, as the lab ran them: the Bash tool a session is
    # given is the one the lab measured
    assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" not in env
    assert env["MEM_DATE"] == env["MEM_REAL_DATE"] == "2026-10-01"
    assert env["MEM_UTC_OFFSET"] == "-25200" and env["TZ"] == "America/Los_Angeles"
    assert env["MEM_SESSION"] == "sid-1" and env["MEM_VAULT"] == str(c.vault_dir)
    # Claude Code's spelling of the directory, dots and all
    assert env["MEM_TRANSCRIPTS"].endswith("-home-d-vault")
    assert "." not in Path(env["MEM_TRANSCRIPTS"]).name


def test_refusals_for_a_busy_vault_are_counted_from_the_transcripts(tmp_path, monkeypatch):
    """doctor counts each refused call once since a start, in the tool result
    of the command that made it, and not again in a later look at it."""
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    c = cfg(tmp_path)
    d = vault.transcripts_dir(c.vault_dir)
    d.mkdir(parents=True)
    since = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    refused = f"Exit code 1\n({vault.BUSY} for 90 s; nothing was read or written)"

    def line(at, kind, content):
        return json.dumps({"type": kind, "timestamp": at, "message": {"role": kind, "content": content}})
    (d / "a.jsonl").write_text("\n".join([
        line("2026-10-02T11:59:59.000Z", "user", [{"type": "tool_result", "content": refused}]),
        # two calls in one command
        line("2026-10-02T12:00:01.000Z", "user", [{"type": "tool_result", "content": f"{refused}\n{refused}"}]),
        line("2026-10-02T12:00:02.000Z", "user", [{"type": "tool_result", "content": [{"type": "text", "text": refused}]}]),
        # words that quote it are no call
        line("2026-10-02T12:00:03.000Z", "assistant", [{"type": "text", "text": refused}]),
        # nor is a look at a refused exchange, as `mem session` shows one
        line("2026-10-02T12:00:04.000Z", "user", [{"type": "tool_result", "content": (
            "20:56:39  I ran: mem entity --kind person --name \"Bob\" 2>&1\n"
            f"          → ({vault.BUSY} for 90 s; nothing was read or written)")}]),
        "not json " + vault.BUSY,
    ]) + "\n")
    before = d / "b.jsonl"
    before.write_text(line("2026-10-02T12:00:05.000Z", "user", [{"type": "tool_result", "content": refused}]))
    os.utime(before, (since.timestamp() - 60,) * 2)  # untouched since the start
    assert vault.refused_for_a_busy_vault(c, since) == 3


def bash(id, at, command):
    """A transcript line of a Bash call."""
    return json.dumps({"type": "assistant", "timestamp": at, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": id, "name": "Bash", "input": {"command": command}}]}})


def result(id, at):
    """A transcript line of a Bash call's result."""
    return json.dumps({"type": "user", "timestamp": at, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": id, "content": "16:38  fan said: ..."}]}})


def test_looks_back_through_every_transcript_are_counted(tmp_path, monkeypatch):
    """The `mem session` calls that read every transcript kept, for the
    per-session log line; one naming a session reads one."""
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    c = cfg(tmp_path)
    d = vault.transcripts_dir(c.vault_dir)
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text("\n".join([
        bash("t1", "2026-10-02T12:00:00.000Z", "mem session --last 10"),
        result("t1", "2026-10-02T12:00:02.500Z"),
        bash("t2", "2026-10-02T12:00:03.000Z",
             "mem session --with mei --last 5 2>&1 | head -40; echo ---; mem session --day 2026-10-01"),
        result("t2", "2026-10-02T12:00:04.000Z"),
        # naming a session reads that one alone, and its time is not counted
        bash("t3", "2026-10-02T12:00:05.000Z", "mem session 1c7fb736"),
        result("t3", "2026-10-02T12:00:09.000Z"),
        bash("t4", "2026-10-02T12:00:10.000Z", "mem recall mei && mem session abc123 --with-nothing"),
        result("t4", "2026-10-02T12:00:20.000Z"),
        json.dumps({"type": "user", "message": {"role": "user", "content": "mem session --last 3"}}),
    ]) + "\n")
    assert vault.looks_back(c, "s1") == (3, 3.5)
    assert vault.looks_back(c, "no-such-session") == (0, 0.0)


def test_a_line_whose_time_cannot_be_paired_still_has_its_calls_counted(tmp_path, monkeypatch):
    """A session's Bash can write to its own transcript. A time with no
    zone, an id that is not a string, a result naming no call or a time that
    will not read is left out of the commands' time, and never ends the
    count, which the session's log line and its snapshot come after."""
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    c = cfg(tmp_path)
    d = vault.transcripts_dir(c.vault_dir)
    d.mkdir(parents=True)
    odd_id = json.loads(bash("x", "2026-10-02T12:00:05.000Z", "mem session --last 1"))
    odd_id["message"]["content"][0]["id"] = ["x"]
    no_id = json.loads(result("x", "2026-10-02T12:00:06.000Z"))
    del no_id["message"]["content"][0]["tool_use_id"]
    (d / "s1.jsonl").write_text("\n".join([
        bash("t1", "2026-10-02T12:00:00.000Z", "mem session --last 10"),
        result("t1", "2026-10-02T12:00:02.000Z"),
        bash("t2", "2026-10-02T12:00:03", "mem session --last 2"),
        result("t2", "2026-10-02T12:00:04.000Z"),
        json.dumps(odd_id),
        json.dumps(no_id),
        bash("t5", "yesterday", "mem session --with mei"),
        result("t5", "2026-10-02T12:00:09.000Z"),
    ]) + "\n")
    assert vault.looks_back(c, "s1") == (4, 2.0)


def test_the_answers_a_session_gave_are_read_from_its_transcript(tmp_path, monkeypatch):
    """Each turn's structured output, in order, as Claude Code keeps it; the
    one the product posts is the last that says something. A line that will
    not read, the prompt and an output that is no report are passed over,
    and a session with no transcript gave none."""
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    c = cfg(tmp_path)
    d = vault.transcripts_dir(c.vault_dir)
    d.mkdir(parents=True)

    def given(answer):
        return json.dumps({"type": "attachment", "attachment": {"type": "structured_output", "data": {
            "recalled": [], "answer": answer, "recorded": []}}})
    (d / "s1.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "the prompt"}}),
        given("It is 7: the gift."),
        "not a line Claude Code writes",
        json.dumps({"type": "attachment", "attachment": {"type": "structured_output", "data": {"no": "answer"}}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "<task-notification>"}}),
        given(""),
    ]) + "\n")
    answers = vault.transcript_answers(c.vault_dir, "s1")
    assert answers == ["It is 7: the gift.", ""]
    assert vault.last_said(answers) == "It is 7: the gift."
    assert vault.transcript_answers(c.vault_dir, "no-such-session") == [] and vault.last_said([]) == ""


def test_the_busy_vault_refusal_is_mems():
    """The sentence `mem` prints when it gave up waiting for the vault, which
    doctor counts the refusals by."""
    mem = (ROOT / "memory" / "src" / "bin" / "mem.rs").read_text()
    said, why = vault.BUSY.split(": ", 1)
    assert f"({said}: {{e}};" in mem and why in (ROOT / "memory" / "src" / "vault.rs").read_text()


def test_report_reads_structured_output_or_the_result_text():
    out = {"recalled": [], "answer": "Will do.", "recorded": []}
    assert vault.report(out, None) == out
    assert vault.report(None, json.dumps(out)) == out
    assert vault.report(None, "I filed it.") is None
    assert vault.report({"ok": True}, None) is None
    assert vault.answer({"answer": "  Will do.  "}) == "Will do."
    assert vault.answer({"answer": "Test"}) == ""
    assert vault.answer({"answer": ""}) == ""


def test_settings_problem():
    assert "anyone" in vault.settings_problem(Config(_env_file=None))
    c = Config(_env_file=None, slack_owner_user_ids="U1,U2")
    assert "WANDA_TZ is not set" in vault.settings_problem(c)
    c = Config(_env_file=None, slack_owner_user_ids="U1,U2", tz="Mars/Olympus")
    assert "not a time zone" in vault.settings_problem(c)
    c = Config(_env_file=None, slack_owner_user_ids="U1,U2", tz="America/Los_Angeles")
    assert vault.settings_problem(c) is None
    for n, ok in ((1, True), (2, True), (0, False), (3, False)):
        c = Config(_env_file=None, slack_owner_user_ids="U1,U2", tz="America/Los_Angeles", memory_sessions=n)
        assert (vault.settings_problem(c) is None) == ok


def test_an_env_that_still_names_people_starts(monkeypatch):
    """Names come from Slack: an .env from before, which still gives
    WANDA_SLACK_NAMES, is read as it always was apart from that line."""
    monkeypatch.setenv("WANDA_SLACK_NAMES", "U1:fan,U2:mei")
    c = Config(_env_file=None, slack_owner_user_ids="U1,U2", tz="America/Los_Angeles")
    assert vault.settings_problem(c) is None and not hasattr(c, "slack_names")


def test_sessions_at_once_are_one_unless_set(monkeypatch):
    """One at a time by default; compose passes a setting .env leaves empty
    as an empty string, which is the default too."""
    assert Config(_env_file=None).memory_sessions == 1
    for empty in ("", " "):
        monkeypatch.setenv("WANDA_MEMORY_SESSIONS", empty)
        assert Config(_env_file=None).memory_sessions == 1
    monkeypatch.setenv("WANDA_MEMORY_SESSIONS", "2")
    assert Config(_env_file=None).memory_sessions == 2


def test_triage_can_be_off_and_alerts_go_where_set():
    assert Config(_env_file=None, email_triage="off").email_triage is False
    assert Config(_env_file=None).email_triage is True
    assert Config(_env_file=None, email_triage_slack_channel_id="C1").alerts_to == "C1"
    assert Config(_env_file=None, email_triage_slack_channel_id="C1", alert_channel="G2").alerts_to == "G2"


# --- the vault, with a stand-in mem ---

STAND_IN = """#!/bin/sh
# writes the root CLAUDE.md as mem does: the root template, then a map
here=$(cd "$(dirname "$0")" && pwd)
echo "$*" >> "$here/calls"
case "$1" in
  entity) { cat "$here/templates/root.md"; printf '\\n## What is here\\n'; } > "$MEM_VAULT/CLAUDE.md"
          mkdir -p "$MEM_VAULT/people"; printf -- '---\\nname: "me"\\n---\\n' > "$MEM_VAULT/people/aaaaaa.md"
          echo x > "$MEM_VAULT/.index.db"; echo "ok person:aaaaaa" ;;
  recall) echo "expanded from 1: person:aaaaaa" ;;
esac
"""
# util-linux flock, which macOS lacks: drops the options and the lock file and
# runs the command. The lock itself is not exercised here.
FLOCK = """#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -s|-x|-n|-o) shift ;; -w|-E) shift 2 ;; *) shift; break ;; esac; done
exec "$@"
"""


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    (bin_dir / "templates").mkdir(parents=True)
    for name in ("root", "enrich", "retract"):
        (bin_dir / "templates" / f"{name}.md").write_text((ROOT / "memory" / "templates" / f"{name}.md").read_text())
    for name, text in (("mem", STAND_IN), ("flock", FLOCK)):
        f = bin_dir / name
        f.write_text(text)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


def log_of(c: Config) -> list[str]:
    out = subprocess.run(["git", "--git-dir", str(c.snapshots_dir), "log", "--format=%s"],
                         capture_output=True, text=True)
    return out.stdout.splitlines()


def tracked(c: Config) -> list[str]:
    return subprocess.run(["git", "--git-dir", str(c.snapshots_dir), "ls-tree", "-r", "--name-only", "HEAD"],
                          capture_output=True, text=True).stdout.split()


def test_prepare_sets_up_a_vault_and_checks_it(tmp_path, stand_in):
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    v = c.vault_dir
    assert (v / ".git").is_dir() and not list((v / ".git").glob("refs/heads/*"))
    assert (v / ".claude/skills/enrich/SKILL.md").read_text() == (ROOT / "memory/templates/enrich.md").read_text()
    assert json.loads((v / ".claude/settings.json").read_text()) == {"cleanupPeriodDays": 30}
    assert (v / "CLAUDE.md").read_text().startswith("# My memory")
    # a current vault is not written to again
    calls = (stand_in / "calls").read_text().splitlines()
    assert vault.prepare(c, NOW) is None
    assert (stand_in / "calls").read_text().splitlines()[len(calls):] == ["recall me"]


def test_a_prepared_vault_shows_sessions_a_clean_status_and_hides_nothing(tmp_path, stand_in):
    """Claude Code puts the vault's git status in every session's context,
    where every lab session read "(clean)"; and its Grep tool honours git's
    ignore files, so nothing in the vault may be ignored."""
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    (c.vault_dir / "people" / "bbbbbb.md").write_text('---\nname: "Lena"\n---\n\ndentist on Tuesdays\n')
    git = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    status = subprocess.run(["git", "--no-optional-locks", "status", "--short", "--ignore-submodules=dirty"],
                            cwd=c.vault_dir, env=git, capture_output=True, text=True)
    assert status.returncode == 0 and status.stdout == ""
    for rel in ("people/bbbbbb.md", "CLAUDE.md", ".claude/settings.json"):
        ignored = subprocess.run(["git", "check-ignore", "-q", rel], cwd=c.vault_dir, env=git)
        assert ignored.returncode == 1, rel


def test_the_status_setting_is_written_only_where_it_is_missing(tmp_path, stand_in):
    """A config.lock a stopped git left fails a write of the repository's
    config: a vault that has the setting is not written to, and one that
    lacks it fails the start with git's own words."""
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    lock = c.vault_dir / ".git" / "config.lock"
    lock.write_text("")
    assert vault.prepare(c, NOW) is None, "the setting is there, so nothing is written"
    lock.unlink()
    subprocess.run(["git", "config", "--unset", "status.showUntrackedFiles"], cwd=c.vault_dir, check=True)
    lock.write_text("")
    problem = vault.prepare(c, NOW)
    assert problem.startswith(
        f"setting up {c.vault_dir}: git config --replace-all status.showUntrackedFiles no: exit ")
    assert "could not lock config file" in problem and problem.endswith(" (README, State)"), problem
    lock.unlink()
    assert vault.prepare(c, NOW) is None


def test_the_status_setting_is_the_vaults_own_and_set_once(tmp_path, stand_in, monkeypatch):
    """Read from the vault's own repository, so that the session user's
    ~/.gitconfig, which a session's Bash can write, never stands in for it;
    and written over every value it has, since git refuses a plain write to
    a key set twice, as a session's own `git config --add` can set it, and
    every later start would fail on that."""
    shared = tmp_path / "gitconfig"
    shared.write_text("[status]\n\tshowUntrackedFiles = no\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(shared))
    c = cfg(tmp_path)

    def own():
        return subprocess.run(["git", "config", "--local", "--get-all", "status.showUntrackedFiles"],
                              cwd=c.vault_dir, capture_output=True, text=True).stdout.split()
    assert vault.prepare(c, NOW) is None and own() == ["no"]
    subprocess.run(["git", "config", "--add", "status.showUntrackedFiles", "normal"], cwd=c.vault_dir, check=True)
    assert own() == ["no", "normal"]
    assert vault.prepare(c, NOW) is None and own() == ["no"]


def test_a_git_the_setup_runs_that_fails_says_why(tmp_path, stand_in):
    """git's own words reach the start's alert: for the vault's repository,
    and for snapshots.git, which a start fails to make while the container's
    view of a directory the Mac has just made is not yet current."""
    c = cfg(tmp_path, vault=str(tmp_path / "v"))
    c.vault_dir.mkdir()
    c.vault_dir.chmod(0o555)
    try:
        problem = vault.prepare(c, NOW)
    finally:
        c.vault_dir.chmod(0o755)
    assert problem.startswith(f"setting up {c.vault_dir}: git init -q: exit "), problem
    assert len(problem.split(": exit ", 1)[1].split(": ", 1)[1]) > 0, "git's words"
    c.expanded_data_dir.write_text("a file where the home directory should be")
    problem = vault.prepare(c, NOW)
    assert problem.startswith(f"making {c.snapshots_dir}: git init -q --bare {c.snapshots_dir}: exit 128: fatal: ")
    assert problem.endswith("; the container's view of a directory the Mac has just moved or made can be 20 s "
                            "old, and Docker starts wanda again by itself (README, State)"), problem


# ripgrep as the Grep tool runs it: TEST_RG names one, or an rg on PATH
RG = os.environ.get("TEST_RG") or shutil.which("rg")


@pytest.mark.skipif(RG is None, reason="the Grep tool's search was not checked: no rg on PATH, and TEST_RG is unset")
def test_the_grep_tools_search_finds_a_node_in_a_prepared_vault(tmp_path, stand_in):
    """The Grep tool runs ripgrep with these flags, and ripgrep honours its
    own ignore files besides git's, so git ignoring nothing is not enough."""
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    (c.vault_dir / "people" / "bbbbbb.md").write_text('---\nname: "Lena"\n---\n\ndentist on Tuesdays\n')
    # as a session's: no git or ripgrep settings of the host's own
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    found = subprocess.run([RG, "--hidden", "--glob", "!.git", "--max-columns", "500", "-l", "dentist", "."],
                           cwd=c.vault_dir, env=env, capture_output=True, text=True)
    assert found.returncode == 0 and found.stdout.split() == ["./people/bbbbbb.md"]


def test_a_snapshot_holds_the_vault_and_its_settings_but_no_index_or_half_writes(tmp_path, stand_in):
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    (c.vault_dir / ".people.part").write_text("half")
    (c.vault_dir / "people" / ".aaaaaa.md.part").write_text("half")
    assert vault.snapshot(c, "startup") is None
    assert log_of(c) == ["startup"]
    assert tracked(c) == [".claude/settings.json", ".claude/skills/enrich/SKILL.md",
                          ".claude/skills/retract/SKILL.md", "CLAUDE.md", "people/aaaaaa.md"]
    assert vault.snapshot(c, "after s1") is None
    assert vault.snapshot(c, "startup") is None
    assert log_of(c) == ["startup"], "nothing changed, nothing committed"
    (c.vault_dir / "people" / "aaaaaa.md").write_text('---\nname: "me"\n---\n\na line\n')
    assert vault.snapshot(c, "after s2") is None
    assert log_of(c) == ["after s2", "startup"]


def test_lock_files_a_stopped_git_left_are_removed_and_said(tmp_path, stand_in):
    """A git stopped part way leaves its lock files, and every later
    snapshot, and the restore, would fail on them."""
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None and vault.snapshot(c, "startup") is None
    (c.snapshots_dir / "index.lock").write_text("")
    (c.snapshots_dir / "refs" / "heads" / "main.lock").write_text("")
    (c.vault_dir / "people" / "aaaaaa.md").write_text('---\nname: "me"\n---\n\na line\n')
    said = vault.snapshot(c, "after s1")
    assert said.startswith("snapshot 'after s1': removed ") and "index.lock" in said and "main.lock" in said
    assert "failed" not in said and log_of(c) == ["after s1", "startup"]
    assert vault.snapshot(c, "after s2") is None


GIT_THAT_HANGS = """#!/bin/sh
# a git that never ends and will not stop when asked, nor will what it started
trap '' TERM
echo $$ >> "$PIDS"
sleep 30 &
echo $! >> "$PIDS"
wait
"""


def test_a_snapshot_past_its_time_is_stopped_whole(tmp_path, stand_in, monkeypatch):
    """Nothing it started goes on holding the snapshots repository, or the
    vault, after the daemon has given up on it."""
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    git = stand_in / "git"
    git.write_text(GIT_THAT_HANGS)
    git.chmod(0o755)
    pid_file = tmp_path / "pids"
    monkeypatch.setenv("PIDS", str(pid_file))
    monkeypatch.setattr(vault, "SNAPSHOT_TIMEOUT_S", 1)
    monkeypatch.setattr(vault, "STOP_GRACE_S", 0.5)
    communicate = subprocess.Popen.communicate

    def once_running(self, *args, **kw):
        # the time limit runs from when the stand-in has written both its
        # pids, not from when the snapshot started it, whose start alone can
        # take longer than the limit on a loaded machine
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(pid_file.read_text().split() if pid_file.exists() else []) < 2:
            time.sleep(0.02)
        return communicate(self, *args, **kw)
    monkeypatch.setattr(subprocess.Popen, "communicate", once_running)
    said = vault.snapshot(c, "after s1")
    monkeypatch.setattr(subprocess.Popen, "communicate", communicate)
    assert said == "snapshot 'after s1': failed: it ran past 1 s and was stopped"
    pids = [int(n) for n in pid_file.read_text().split()]
    assert len(pids) == 2

    def gone(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False
    deadline = time.monotonic() + 5
    while not all(gone(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert all(gone(pid) for pid in pids)


def test_a_group_the_system_will_not_signal_counts_as_gone(monkeypatch, caplog):
    """macOS refuses, with EPERM, a signal to a process group whose members
    have all ended and are not yet reaped: the stop goes on past each refusal
    and takes the group as gone, rather than raising out of the snapshot or
    waiting on it as still running. The system's refusal is stood in for
    here, where it comes only some of the time."""
    monkeypatch.setattr(vault, "STOP_GRACE_S", 0.3)
    real, refused = os.killpg, []

    def refusing(pid, sig):
        refused.append((pid, sig))
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(vault.os, "killpg", refusing)
    try:
        with caplog.at_level(logging.WARNING, logger="wanda.vault"), pytest.raises(subprocess.TimeoutExpired):
            vault._run_group(["sleep", "30"], timeout=0.2)
    finally:
        for pid in {pid for pid, _ in refused}:
            with contextlib.suppress(ProcessLookupError):
                real(pid, signal.SIGKILL)
    assert [sig for _, sig in refused] == [signal.SIGTERM, signal.SIGKILL, 0]
    assert "still running" not in caplog.text


def test_snapshots_take_turns(tmp_path, stand_in, monkeypatch):
    """Two gits in one repository at once fail on its index.lock."""
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    git = stand_in / "git"
    git.write_text('#!/bin/sh\necho start >> "$TURNS"\nsleep 0.2\necho end >> "$TURNS"\n')
    git.chmod(0o755)
    monkeypatch.setenv("TURNS", str(tmp_path / "turns"))
    both = [threading.Thread(target=vault.snapshot, args=(c, f"after s{i}")) for i in range(2)]
    for t in both:
        t.start()
    for t in both:
        t.join()
    turns = (tmp_path / "turns").read_text().split()
    assert turns == ["start", "end"] * (len(turns) // 2) and len(turns) == 8


def objects(c: Config, which: str) -> int:
    """git's count of loose objects ("count") or of packed ones ("in-pack")."""
    out = subprocess.run(["git", "--git-dir", str(c.snapshots_dir), "count-objects", "-v"],
                         capture_output=True, text=True).stdout
    return int(re.search(rf"^{which}: (\d+)", out, re.MULTILINE).group(1))


def test_housekeeping_is_left_out_of_snapshots_and_done_apart(tmp_path, stand_in):
    """git's own housekeeping would hold the repository past the snapshot,
    and, run from it, past its time limit; it runs on its own instead."""
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None and vault.snapshot(c, "startup") is None
    git = ["git", "--git-dir", str(c.snapshots_dir)]
    subprocess.run(git + ["config", "gc.auto", "1"], check=True)
    subprocess.run(git + ["config", "gc.autoDetach", "false"], check=True)
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    batch = 0
    # enough loose objects that git's estimate, from objects/17, is past gc.auto
    while len(list((c.snapshots_dir / "objects" / "17").glob("*"))) < 2:
        paths = []
        for i in range(500):
            (f := blobs / f"{batch}-{i}").write_text(f"{batch} {i}\n")
            paths.append(str(f))
        subprocess.run(git + ["hash-object", "-w", "--stdin-paths"], input="\n".join(paths),
                       text=True, capture_output=True, check=True)
        batch += 1
    before = objects(c, "count")
    (c.vault_dir / "people" / "aaaaaa.md").write_text('---\nname: "me"\n---\n\na line\n')
    assert vault.snapshot(c, "after s1") is None and log_of(c)[0] == "after s1"
    assert objects(c, "count") > before and objects(c, "in-pack") == 0, "the commit started no housekeeping"
    assert vault.housekeep(c) is None
    assert objects(c, "in-pack") > 0


def test_the_vault_and_the_run_store_can_live_apart_from_the_data_directory(tmp_path):
    c = cfg(tmp_path)
    assert c.vault_dir == tmp_path / "home.d" / "vault" and c.db_path == tmp_path / "home.d" / "wanda.db"
    c = cfg(tmp_path, vault="/srv/wanda/vault", run_store="/srv/wanda/store")
    assert c.vault_dir == Path("/srv/wanda/vault") and c.snapshots_dir == tmp_path / "home.d" / "snapshots.git"
    assert [c.db_path, c.lock_path, c.dryrun_db_path] == [
        Path("/srv/wanda/store") / n for n in ("wanda.db", "wanda.lock", "dryrun.db")]


def test_a_lost_vault_is_not_replaced_by_an_empty_one(tmp_path, stand_in):
    """In Docker a lost vault comes back as a new volume, an empty directory,
    while the run store and the snapshots, on the Mac, say it had one."""
    c = cfg(tmp_path, vault=str(tmp_path / "volume"))
    assert vault.prepare(c, NOW) is None and vault.snapshot(c, "startup") is None
    subprocess.run(["rm", "-rf", str(c.vault_dir)], check=True)
    c.vault_dir.mkdir()
    problem = vault.prepare(c, NOW, known_since="2026-09-01")
    assert "is empty" in problem and "2026-09-01" in problem and "startup" in problem
    assert not any(c.vault_dir.iterdir()), "nothing is written into it"
    problem = vault.prepare(c, NOW)
    assert "is empty" in problem and "snapshots.git holds it" in problem, "the run store moved aside"
    # a `mem` read by hand on the empty vault leaves its kind directories
    for kind in vault.KIND_DIRS:
        (c.vault_dir / kind).mkdir()
    problem = vault.prepare(c, NOW, known_since="2026-09-01")
    assert "is empty" in problem, "directories alone are no vault"
    assert sorted(p.name for p in c.vault_dir.iterdir()) == sorted(vault.KIND_DIRS), "nothing is written into it"
    subprocess.run(["rm", "-rf", str(c.snapshots_dir)], check=True)
    assert vault.prepare(c, NOW) is None, "with neither, an empty vault is started"


def test_a_lost_snapshot_repository_is_not_replaced_by_an_empty_one(tmp_path, stand_in):
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    # the directory as a stale view of the Mac's mount still shows it, its HEAD gone
    (c.snapshots_dir / "HEAD").unlink()
    problem = vault.prepare(c, NOW, known_since="2026-09-01")
    assert "snapshots.git missing" in problem and not (c.snapshots_dir / "HEAD").exists()
    subprocess.run(["rm", "-rf", str(c.snapshots_dir)], check=True)
    problem = vault.prepare(c, NOW, known_since="2026-09-01")
    assert "snapshots.git missing" in problem and not c.snapshots_dir.exists()


def test_snapshots_that_cannot_be_read_count_as_a_record(tmp_path, stand_in, monkeypatch):
    """A repository that cannot say what it holds is not one that holds
    nothing, and an empty vault beside it is not started."""
    c = cfg(tmp_path, vault=str(tmp_path / "volume"))
    assert vault.prepare(c, NOW) is None and vault.snapshot(c, "startup") is None
    subprocess.run(["rm", "-rf", str(c.vault_dir)], check=True)
    c.vault_dir.mkdir()
    branch = (c.snapshots_dir / "HEAD").read_text().split("ref: ", 1)[1].strip()
    good = (c.snapshots_dir / branch).read_text()
    (c.snapshots_dir / branch).write_text("not a commit\n")
    problem = vault.prepare(c, NOW)
    assert "is empty" in problem and "could not be read" in problem and "broken" in problem, problem
    (c.snapshots_dir / branch).write_text(good)
    run = vault._run
    monkeypatch.setattr(vault, "_run", lambda argv, cwd=None, env=None, timeout=120: (
        run(["sleep", "5"], cwd, env, 0.2) if "log" in argv else run(argv, cwd, env, timeout)))
    problem = vault.prepare(c, NOW)
    assert "is empty" in problem and "could not be read" in problem and "timed out" in problem, problem
    monkeypatch.setattr(vault, "_run", run)
    # a repository with no commit yet holds nothing
    subprocess.run(["rm", "-rf", str(c.snapshots_dir)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(c.snapshots_dir)], check=True)
    assert vault.last_snapshot(c) == "none" and vault.prepare(c, NOW) is None


def test_whether_there_are_snapshots_is_read_from_their_head(tmp_path):
    """A look at the directory can be answered from a view of the Mac's
    mount up to 20 s old; an open of its HEAD goes to the Mac."""
    c = cfg(tmp_path)
    assert not vault.has_snapshots(c)
    c.snapshots_dir.mkdir(parents=True)
    assert not vault.has_snapshots(c), "a directory without its HEAD"
    (c.snapshots_dir / "HEAD").write_text("ref: refs/heads/master\n")
    assert vault.has_snapshots(c)
    (c.snapshots_dir / "HEAD").chmod(0)
    try:
        assert vault.has_snapshots(c), "there, though it cannot be read"
    finally:
        (c.snapshots_dir / "HEAD").chmod(0o644)


def test_prepare_writes_the_vault_only_under_its_lock(tmp_path, stand_in, monkeypatch):
    import fcntl
    c = cfg(tmp_path)
    c.vault_dir.mkdir(parents=True)
    monkeypatch.setattr(vault, "LOCK_WAIT_S", 0.3)
    fd = os.open(c.vault_dir, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH)  # as a `mem` read holds it
    try:
        assert "the vault stayed locked for 0.3 s" in vault.prepare(c, NOW)
        assert not (c.vault_dir / ".claude").exists()
    finally:
        os.close(fd)
    assert vault.prepare(c, NOW) is None


def test_stale_standing_texts_are_regenerated(tmp_path, stand_in):
    c = cfg(tmp_path)
    vault.prepare(c, NOW)
    (c.vault_dir / "CLAUDE.md").write_text("# an older text\n")
    assert vault.prepare(c, NOW) is None
    assert (c.vault_dir / "CLAUDE.md").read_text().startswith("# My memory")


def test_check_names_what_is_wrong(tmp_path, stand_in, monkeypatch):
    c = cfg(tmp_path)
    assert "No such file" in vault.check(c, NOW)
    c.vault_dir.mkdir(parents=True)
    assert "CLAUDE.md" in vault.check(c, NOW)
    assert vault.prepare(c, NOW) is None and vault.snapshot(c, "startup") is None
    (c.vault_dir / "people" / "bbbbbb.md").write_text("---\nname: \"Le")
    (c.vault_dir / "events").mkdir()
    (c.vault_dir / "events" / "2026-10-01-cccccc.md").write_text('---\nsummary: "lunch"\n---\n\nbody\n')
    problem = vault.check(c, NOW)
    assert "1 node file(s)" in problem and "people/bbbbbb.md" in problem and "startup" in problem
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert vault.prepare(c, NOW) == "mem is not on PATH"


def test_check_reads_the_vault_under_its_lock_and_writes_under_it_alone(tmp_path, stand_in, monkeypatch):
    import fcntl
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    monkeypatch.setattr(vault, "LOCK_WAIT_S", 0.3)
    fd = os.open(c.vault_dir, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # as a `mem` write holds it
        assert vault.check(c, NOW) == f"reading {c.vault_dir}: the vault stayed locked for 0.3 s"
        fcntl.flock(fd, fcntl.LOCK_SH)  # as a `mem` read holds it: the reads go on, the write waits
        assert vault.check(c, NOW) == f"writing to {c.vault_dir}: the vault stayed locked for 0.3 s"
    finally:
        os.close(fd)
    assert vault.check(c, NOW) is None


def test_check_proves_the_vault_takes_a_write(tmp_path, stand_in):
    """A vault that stops taking writes (its disk full, say) is otherwise
    seen only by sessions, which mostly answer nothing."""
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    assert not (c.vault_dir / vault.WRITE_PROBE).exists()
    c.vault_dir.chmod(0o555)
    try:
        problem = vault.check(c, NOW)
    finally:
        c.vault_dir.chmod(0o755)
    assert problem.startswith(f"writing to {c.vault_dir}: ") and "Permission denied" in problem


def test_a_mem_call_that_never_answers_is_reported_not_raised(tmp_path, stand_in, monkeypatch):
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    (stand_in / "mem").write_text("#!/bin/sh\nexec sleep 5\n")
    run = vault._run
    monkeypatch.setattr(vault, "_run", lambda argv, cwd=None, env=None, timeout=120: run(argv, cwd, env, 0.5))
    problem = vault.check(c, NOW)
    assert problem.startswith("mem recall me:") and "timed out" in problem
    (c.vault_dir / "CLAUDE.md").write_text("# an older text\n")
    problem = vault.prepare(c, NOW)
    assert problem.startswith("mem entity:") and "timed out" in problem


def test_a_write_mem_cannot_make_fails_the_setup(tmp_path, stand_in):
    """`mem` reports a write it cannot make with a non-zero exit and one
    sentence; the setup's own write passes the sentence on."""
    c = cfg(tmp_path)
    (stand_in / "mem").write_text('#!/bin/sh\necho "(a write that could not be made)"; exit 1\n')
    problem = vault.prepare(c, NOW)
    assert problem.startswith("mem entity: exit 1:") and "(a write that could not be made)" in problem


# --- `mem` as the image's PATH finds it ---

@pytest.fixture
def wrapped(tmp_path):
    """docker/mem as the image lays it out, in front of a stand-in for the
    real `mem` that prints its dates."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "libexec").mkdir()
    shutil.copy(ROOT / "docker" / "mem", tmp_path / "bin" / "mem")
    (tmp_path / "libexec" / "mem").write_text('#!/bin/sh\necho "$MEM_DATE $MEM_REAL_DATE $MEM_UTC_OFFSET $*"\n')
    for f in (tmp_path / "bin" / "mem", tmp_path / "libexec" / "mem"):
        f.chmod(0o755)
    return tmp_path / "bin" / "mem"


def by_hand(mem: Path, **env) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("MEM_", "WANDA_"))}
    return subprocess.run([str(mem), "recall", "me"], env=base | env, capture_output=True, text=True)


def test_a_call_by_hand_is_dated_in_the_households_zone(wrapped):
    """In a zone whose date is not UTC's now, as the household's is of an
    evening, the date is the zone's."""
    zone = "Etc/GMT+12" if datetime.now(timezone.utc).hour < 12 else "Etc/GMT-14"
    done = by_hand(wrapped, WANDA_TZ=zone)
    here = datetime.now(ZoneInfo(zone))
    assert here.date() != datetime.now(timezone.utc).date()
    offset = int(here.utcoffset().total_seconds())
    assert done.stdout == f"{here.date()} {here.date()} {offset} recall me\n", done.stderr
    # half hours, and hours that read as octal with their leading zero
    for zone, offset in (("Asia/Kolkata", 19800), ("Asia/Shanghai", 28800), ("Pacific/Chatham", None)):
        offset = offset or int(datetime.now(ZoneInfo(zone)).utcoffset().total_seconds())
        assert by_hand(wrapped, WANDA_TZ=zone).stdout.split()[2] == str(offset)


def test_a_sessions_dates_pass_through(wrapped):
    done = by_hand(wrapped, MEM_DATE="2030-01-02", MEM_REAL_DATE="2030-01-02", MEM_UTC_OFFSET="3600")
    assert done.stdout == "2030-01-02 2030-01-02 3600 recall me\n"


def test_no_date_without_a_zone(wrapped):
    zones = Path("/usr/share/zoneinfo")
    assert (zones / "America").is_dir() and (zones / "zone.tab").is_file()
    for env in ({}, {"WANDA_TZ": "Mars/Olympus"}, {"WANDA_TZ": "America"}, {"WANDA_TZ": "zone.tab"}):
        done = by_hand(wrapped, **env)
        assert done.returncode == 1 and done.stdout == "" and "WANDA_TZ" in done.stderr


@pytest.mark.skipif(not os.environ.get("TEST_MEM_BIN"), reason="TEST_MEM_BIN names a mem build with its templates")
def test_prepare_with_the_real_mem(tmp_path, monkeypatch):
    mem = Path(os.environ["TEST_MEM_BIN"])
    monkeypatch.setenv("PATH", f"{mem.parent}:{os.environ['PATH']}")
    c = cfg(tmp_path)
    assert vault.prepare(c, NOW) is None
    assert (c.vault_dir / "CLAUDE.md").read_text().startswith("# My memory")
    assert vault.check(c, NOW) is None
