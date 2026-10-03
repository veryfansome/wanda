"""Nothing wanda reads as her own addresses her or names her from outside:
what opens and continues her sessions, the paragraph in her system prompt,
her triage rules, her skills, the help `wanda slack` prints, and the vault's
standing texts. Who she is, how she conducts herself and what she says are
in the first person. A procedure, a tool to use or a step to take, is a bare
imperative with no pronoun, and a paragraph or list item holding one carries
no first-person word, so that nobody else can be read as giving her the
order. A text here that says "you", or names her as "wanda", "she" or "the
bot", fails."""

import argparse
import re
from pathlib import Path

import pytest

from datetime import datetime
from zoneinfo import ZoneInfo

from wanda import clock, slack_cli, vault
from wanda.main import (
    ANCHOR,
    HOW_TO_REPLY,
    REOPENED,
    addressed_to_me,
    agent_seed_prompt,
    triage_system_prompt,
)
from wanda.triage import build_batch_prompt

ROOT = Path(__file__).resolve().parent.parent
SECOND_PERSON = re.compile(r"\byou(?:rs?|rself|rselves|['’](?:d|ll|re|ve))?\b", re.IGNORECASE)
THIRD_PERSON = re.compile(r"\b(?:wanda|she|her|hers|herself|the bot)\b", re.IGNORECASE)
FIRST_PERSON = re.compile(r"\bI\b|\b(?i:me|my|mine|myself)\b")
# Not her voice: code, which is commands and flags; words in quotation marks,
# which are someone else's; her own "I am wanda"; and the program she runs.
NOT_VOICE = re.compile(
    r"^(?P<fence>`{3,})[^\n]*\n[\s\S]*?^(?P=fence)"   # a fenced block
    r"|(?P<tick>`+)(?!`)[^\n]*?(?<!`)(?P=tick)(?!`)"    # a code span, however many backticks
    r"|\"[^\"\n]*\"|“[^”\n]*”"
    r"|\bI am wanda\b|\bthe wanda CLI\b",
    re.MULTILINE | re.IGNORECASE,
)

EMAIL = {"from_addr": "a@b.test", "subject": "invoice", "date_hdr": "Mon", "snippet": "due Friday",
         "dedupe_key": "k1"}


def slack_help() -> str:
    """What `wanda slack --help` and each verb's --help print. The seed points
    her at it. Its usage lines and variable names spell the program, not her."""
    top = argparse.ArgumentParser(prog="wanda")
    slack_cli.add_parser(top.add_subparsers(dest="cmd"))
    pending, pages = [top], []
    while pending:
        parser = pending.pop()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                pending.extend(action.choices.values())
        if parser is not top:
            pages.append(parser.format_help())
    text = "\n".join(pages)
    text = re.sub(r"^usage:.*?(?=\n\n|\Z)", " ", text, flags=re.MULTILINE | re.DOTALL)
    return re.sub(r"\$?WANDA_\w+", " ", text)


def rust_str(path: str, name: str) -> str:
    """A `pub const NAME: &str = "...";` as the program holds it."""
    lit = re.search(rf'pub const {name}: &str =\s*"(.*?)";', (ROOT / path).read_text(), re.DOTALL)
    assert lit, f"{name} is gone from {path}"
    return lit.group(1)


def texts() -> list[tuple[str, str]]:
    found = [
        ("email seed", agent_seed_prompt(EMAIL, "summarize it")),
        ("later turn", addressed_to_me("alice", "hi")),
        ("anchor", ANCHOR),
        ("memory system prompt", f"{ANCHOR}\n\n" + vault.date_paragraph(
            datetime(2026, 10, 1, 9, 5, tzinfo=ZoneInfo("America/Los_Angeles")))),
        ("memory prompt", vault.prompt("2026-10-01", "(the arrival)")),
        ("memory frame, dm alone", vault.arrival_text("dm", "alice", "hi", [], [])),
    ]
    for place in vault.PLACES:
        found.append((f"memory frame, {place}", vault.arrival_text(
            place, "alice", "hi", ["alice", "bob" + vault.GUEST, "Alice" + vault.NAMESAKE],
            [("Wed 2026-09-30 21:40", "bob", "earlier"), ("09:05", vault.ME, "reply")])))
    found.append(("memory frame, a turn of two speakers", vault.arrival_text(
        "group", "alice", "hi", ["alice", "bob"], [("09:05", "bob", "earlier")], also=["bob"])))
    for place in vault.PLACES:
        found.append((f"a message added while the session works, {place}",
                      vault.added_text(place, "alice", "hi", "09:06")))
    found += [
        ("triage system prompt", triage_system_prompt()),
        ("triage batch", build_batch_prompt([EMAIL])[0]),
        ("wanda slack help", slack_help()),
        ("clock, morning look", clock.Wake("k", "U2", clock.MORNING.format(
            weekday="Thursday", time="08:00", speaker="mei")).arrival("mei",
            # the heading and the mark as `mem due --for` prints them
            # (memory/src/due.rs, LOOK_HEAD and STILL_TO_COME)
            ["Come due for mei after 2026-09-30:",
             "`trajectory:aaaaaa`  2026-10-01, today  the letter", "    asked by: mei",
             "`trajectory:bbbbbb`  2026-10-01T17:00, today  I undertook to remind mei at 5",
             "    asked by: mei", "    " + rust_str("memory/src/due.rs", "STILL_TO_COME").replace(
                 "{asker}", "mei").replace("{time}", "17:00")])),
        ("clock, timed undertaking", clock.Wake("k", "U1", "\n    ".join((
            clock.COME_DUE.format(weekday="Thursday", time="17:00"),
            "`trajectory:aaaaaa`  2026-10-01T17:00, today  I undertook to remind fan at 5",
            "    involves: me; fan", "    asked by: fan"))).arrival("fan")),
        # woken again after a session for it was cut short
        ("clock, timed undertaking woken again", clock.Wake("k", "U1", "\n    ".join((
            clock.COME_DUE.format(weekday="Thursday", time="17:10"),
            "`trajectory:aaaaaa`  2026-10-01T17:00, today  I undertook to remind fan at 5",
            "    involves: me; fan", "    asked by: fan", clock.AGAIN.format(speaker="fan")))).arrival("fan")),
        # the note doctor's command leaves on a reminder it reopens, which the
        # session woken for it reads among the item's notes
        ("doctor's note on a reminder reopened", REOPENED),
    ]
    for path in sorted(ROOT.glob("skills/*/SKILL.md")) + sorted(ROOT.glob("memory/templates/*.md")):
        found.append((str(path.relative_to(ROOT)), path.read_text()))
    return found


TEXTS = texts()


@pytest.mark.parametrize("text", [t for _, t in TEXTS], ids=[n for n, _ in TEXTS])
def test_no_text_addresses_her_or_names_her_from_outside(text):
    prose = NOT_VOICE.sub(" ", text)
    for pattern, what in ((SECOND_PERSON, "says you"), (THIRD_PERSON, "names her from outside")):
        hit = pattern.search(prose)
        assert hit is None, (
            f"{what}: {hit.group(0)!r} in {prose[max(0, hit.start() - 60):hit.end() + 60]!r}. "
            "This is one of wanda's own texts: who she is and how she acts say I / me / my, "
            "a procedure is a bare imperative, and a person's words go in quotes"
        )


def section(path: str, heading: str) -> str:
    text = (ROOT / path).read_text()
    found = re.search(rf"^{re.escape(heading)}\n(.*?)(?=^#{{1,6}} |\Z)", text, re.MULTILINE | re.DOTALL)
    assert found and found.group(1).strip(), f"{path} has no {heading!r} section"
    return found.group(1)


def steps(path: str, heading: str) -> str:
    found = re.findall(r"^\d+\. .*$", section(path, heading), re.MULTILINE)
    assert found, f"{path} has no numbered steps under {heading!r}"
    return "\n".join(found)


def description(path: str) -> str:
    found = re.search(r"\A---\n.*?^description: (.+?)$.*?^---$", (ROOT / path).read_text(),
                      re.MULTILINE | re.DOTALL)
    assert found, f"{path} has no description"
    return found.group(1)


def procedures() -> list[tuple[str, str]]:
    """The texts whose role is fixed by where they sit: how to reply, the triage
    batch's instruction, when to use each skill, slack-reply's Sending and
    Reading more context sections, and the follow-up's numbered steps. The
    steps are checked without the rest of their section, because a paragraph
    of conduct follows them there. Which sentence elsewhere is a procedure is
    a reading of it, and is not checked here."""
    found = [
        ("how to reply", HOW_TO_REPLY),
        ("triage batch instruction", build_batch_prompt([EMAIL])[0].split("\n\n")[0]),
    ]
    for path in sorted(ROOT.glob("skills/*/SKILL.md")):
        rel = str(path.relative_to(ROOT))
        found.append((f"{rel} description", description(rel)))
    for rel, heading in (("skills/slack-reply/SKILL.md", "## Sending"),
                         ("skills/slack-reply/SKILL.md", "## Reading more context")):
        found.append((f"{rel} {heading}", section(rel, heading)))
    rel, heading = "skills/slack-triage-followup/SKILL.md", "## Working the task"
    found.append((f"{rel} {heading} steps", steps(rel, heading)))
    found.append(("retract steps", steps("memory/templates/retract.md", "# Unsaying something")))
    enrich = steps("memory/templates/enrich.md", "# Before finishing").splitlines()
    found.append(("enrich steps 1-4", "\n".join(s for s in enrich if not s.startswith("5. "))))
    found.append(("root.md Before finishing", section("memory/templates/root.md", "## Before finishing")))
    prompt = lab_prompt_steps()
    if prompt is not None:
        found.append(("the lab prompt's steps", prompt))
    return found


def lab_prompt_steps() -> str | None:
    """The lab prompt after the arrival: its steps, as a session reads them."""
    arrival = ROOT / "lab" / "harness" / "src" / "arrival.rs"
    if not arrival.exists():
        return None
    lit = re.search(r'pub const PROMPT: &str = "(.*?)";', arrival.read_text(), re.DOTALL)
    assert lit, "the lab's PROMPT is gone from arrival.rs"
    text = re.sub(r"\\\n\s*", "", lit.group(1)).replace("\\n", "\n")
    return text.split("{arrival}", 1)[1]


PROCEDURES = procedures()


@pytest.mark.parametrize("text", [t for _, t in PROCEDURES], ids=[n for n, _ in PROCEDURES])
def test_procedures_carry_no_first_person_word(text):
    prose = NOT_VOICE.sub(" ", text)
    hit = FIRST_PERSON.search(prose)
    assert hit is None, (
        f"{hit.group(0)!r} in {prose[max(0, hit.start() - 60):hit.end() + 60]!r}. A procedure is a "
        "bare imperative; it names what it acts on as the answer, the prompt, this session, "
        "or in the passive"
    )


def test_the_email_seed_keeps_the_procedure_in_its_own_paragraph():
    assert f"\n\n{HOW_TO_REPLY}\n" in agent_seed_prompt(EMAIL, "x")


def test_every_text_is_read():
    names = [n for n, _ in TEXTS]
    assert sum(n.startswith("skills/") for n in names) == 2
    assert sum(n.startswith("memory/templates/") for n in names) == 3
    assert slack_help().count("-h, --help") == 8, "the slack parser and each of its seven verbs"


def test_seeds_and_triage_say_who_she_is():
    assert agent_seed_prompt(EMAIL, "x").startswith("I am wanda, ")
    assert vault.prompt("2026-10-01", "(the arrival)").startswith("I am wanda.\n\n")
    assert triage_system_prompt().startswith(ANCHOR + "\n\n")


def test_the_lab_hands_its_sessions_the_same_anchor():
    session = ROOT / "lab" / "harness" / "src" / "session.rs"
    if not session.exists():
        pytest.skip("no lab in this checkout")
    lab = re.search(r'const ANCHOR: &str = r#"(.*?)"#;', session.read_text(), re.DOTALL)
    assert lab, "the lab's ANCHOR is gone from session.rs"
    assert lab.group(1) == ANCHOR, "the lab's anchor and the product's have drifted apart"

