"""The shapes of Claude Code's output and transcripts that the harness reads,
checked in the code of a Claude Code binary, without starting a session: run
with TEST_CLAUDE_BIN naming the binary of the version CLAUDE_VERSION in
wanda.Dockerfile would move to, before it moves. A message added while a
session works rests on shapes the pinned version (2.1.268) showed in
sessions run against it with their input open, and that its code reads,
none of them documented. A failure here means the stand-in
(tests/claude_standin.py) no longer stands for that version: the version
stays where it is until sessions run against it show those shapes again.
Passing, the move still waits for live sessions in the scratch project
compose.foldin-check.yaml sets up: sessions handed a message mid-turn, one
after their last step, and two at once at a further turn's start."""

import os
import re
from pathlib import Path

import pytest

from wanda.runner import STREAM_EVENTS

BIN = os.environ.get("TEST_CLAUDE_BIN")
pytestmark = pytest.mark.skipif(not BIN, reason="TEST_CLAUDE_BIN names no Claude Code binary to read")
NAME = rb"[\w$]+"


@pytest.fixture(scope="module")
def code() -> bytes:
    return Path(BIN).read_bytes()


def find(code: bytes, literal: bytes, then: bytes = b"", before: bytes = b""):
    """Each place `literal` is in the code with `before` just ahead of it and
    `then` just after it, both patterns, as (where, before's match, then's
    match); the binary is too large to search with a pattern alone."""
    at = code.find(literal)
    while at != -1:
        head = re.search(before + rb"\Z", code[max(0, at - 2000):at]) if before else True
        tail = re.match(then, code[at + len(literal):at + len(literal) + 12000], re.S)
        if head and tail:
            yield at, head, tail
        at = code.find(literal, at + 1)


def types_of(code: bytes, name: bytes, near: int, depth: int = 0) -> set[str]:
    """The `type` of every message a schema of the CLI's admits, following
    unions and extensions to the schemas they name. Its minified names are
    reused from one module to the next, so a name is read where it is
    defined nearest to where it is named."""
    assert depth < 8, name
    for at, _, m in sorted(find(code, name + b"=", NAME + rb"\(\(\)=>(.{0,2000})", rb"(?<![\w$])"),
                           key=lambda f: abs(f[0] - near)):
        body = m[1]
        if t := re.match(NAME + rb"\(\{type:" + NAME + rb'\("([a-z_]+)"\)', body):
            return {t[1].decode()}
        if u := re.match(NAME + rb"\(\[([^\]]*)\]\)", body):
            return set().union(*(types_of(code, x.strip().removesuffix(b"()"), at, depth + 1)
                                 for x in u[1].split(b",")))
        if x := re.match(rb"(" + NAME + rb")\(\)\.extend\(", body):
            return types_of(code, x[1], at, depth + 1)
    raise AssertionError(f"no schema {name!r} in a shape this reads")


def test_every_event_its_output_can_hold_is_one_the_runner_reads_past(code):
    """The runner fails a session on an event of another type."""
    union = next(find(code, b'.describe("Everything the CLI writes to its output stream',
                      before=NAME + rb"\(\[([^\]]*)\]\)"), None)
    assert union, "the list of what the CLI writes to its output was not found"
    at, members, _ = union
    found = set().union(*(types_of(code, x.strip().removesuffix(b"()"), at) for x in members[1].split(b",")))
    assert found <= STREAM_EVENTS, sorted(found - STREAM_EVENTS)


def test_a_result_carries_what_the_runner_reads(code):
    for subtype, fields in ((rb'"success"\)', (b"is_error:", b"total_cost_usd:", b"result:", b"structured_output:")),
                            (rb'\["error_during_execution"', (b"is_error:", b"total_cost_usd:"))):
        # each schema's fields, to the end of its object
        missing = [[f for f in fields if f not in m[1].split(b"}))")[0]]
                   for _, _, m in find(code, b'("result"),subtype:', NAME + rb"\(" + subtype + rb"(.*)")]
        assert missing and min(missing, key=len) == [], (subtype, missing)


def test_a_message_handed_mid_turn_is_a_queued_command_of_its_mode(code):
    """What both readers take as an added message, and the words around it
    the README quotes."""
    m = next(find(code, b'type:"queued_command",prompt:', rb"[^}]{0,400}?commandMode:[^}]{0,200}?isMeta:"), None)
    assert m, "no queued_command attachment with its mode and meta flag"
    assert b"The user sent a new message while you were working:" in code
    assert b"Address the message above as you continue this turn." in code


def test_a_notice_of_a_background_commands_end_has_a_mode_and_an_origin_of_its_own(code):
    assert b'mode:"task-notification"' in code and b'{kind:"task-notification"}' in code


def test_each_turns_answer_is_kept_in_the_transcript(code):
    """What `mem session` reads, and a clock session's answers after its last
    result alone was printed."""
    assert b'type:"structured_output",data:' in code


def test_a_turn_opens_with_its_init_event(code):
    """Nothing is written to a session's input before it, so an added message
    is not taken into the opening one."""
    assert b'type:"system",subtype:"init"' in code
