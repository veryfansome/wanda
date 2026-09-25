"""wanda's own texts are written in her voice, the first person: what opens
and continues her sessions, the paragraph in her system prompt, her triage
rules, her skills, the help `wanda slack` prints, and the vault's standing
texts. A text here that says "you", or names her from outside as "wanda",
"she" or "the bot", fails."""

import argparse
import re
from pathlib import Path

import pytest

from wanda import slack_cli
from wanda.main import (
    ANCHOR,
    addressed_to_me,
    agent_seed_prompt,
    conversation_seed_prompt,
    triage_system_prompt,
)
from wanda.triage import build_batch_prompt

ROOT = Path(__file__).resolve().parent.parent
SECOND_PERSON = re.compile(r"\byou(?:rs?|rself|rselves|['’](?:d|ll|re|ve))?\b", re.IGNORECASE)
THIRD_PERSON = re.compile(r"\b(?:wanda|she|her|hers|herself|the bot)\b", re.IGNORECASE)
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


def texts() -> list[tuple[str, str]]:
    found = [
        ("email seed", agent_seed_prompt(EMAIL, "summarize it")),
        ("conversation seed", conversation_seed_prompt({"kind": "dm", "text": "hi"}, "(none)", "alice")),
        ("later turn", addressed_to_me("alice", "hi")),
        ("anchor", ANCHOR),
        ("triage system prompt", triage_system_prompt()),
        ("triage batch", build_batch_prompt([EMAIL])[0]),
        ("wanda slack help", slack_help()),
    ]
    for path in sorted(ROOT.glob("skills/*/SKILL.md")) + sorted(ROOT.glob("memory/templates/*.md")):
        found.append((str(path.relative_to(ROOT)), path.read_text()))
    return found


TEXTS = texts()


@pytest.mark.parametrize("text", [t for _, t in TEXTS], ids=[n for n, _ in TEXTS])
def test_her_texts_are_in_the_first_person(text):
    prose = NOT_VOICE.sub(" ", text)
    for pattern, what in ((SECOND_PERSON, "says you"), (THIRD_PERSON, "names her from outside")):
        hit = pattern.search(prose)
        assert hit is None, (
            f"{what}: {hit.group(0)!r} in {prose[max(0, hit.start() - 60):hit.end() + 60]!r}. "
            "This is one of wanda's own texts, written as I / me / my; a person's words go in quotes"
        )


def test_every_text_is_read():
    names = [n for n, _ in TEXTS]
    assert sum(n.startswith("skills/") for n in names) == 2
    assert sum(n.startswith("memory/templates/") for n in names) == 3
    assert slack_help().count("-h, --help") == 8, "the slack parser and each of its seven verbs"


def test_seeds_and_triage_say_who_she_is():
    assert agent_seed_prompt(EMAIL, "x").startswith("I am wanda, ")
    assert conversation_seed_prompt({"kind": "dm", "text": "hi"}, "", "alice").startswith("I am wanda, ")
    assert triage_system_prompt().startswith(ANCHOR + "\n\n")


def test_the_lab_hands_its_sessions_the_same_anchor():
    session = ROOT / "lab" / "harness" / "src" / "session.rs"
    if not session.exists():
        pytest.skip("no lab in this checkout")
    lab = re.search(r'const ANCHOR: &str = r#"(.*?)"#;', session.read_text(), re.DOTALL)
    assert lab, "the lab's ANCHOR is gone from session.rs"
    assert lab.group(1) == ANCHOR, "the lab's anchor and the product's have drifted apart"
