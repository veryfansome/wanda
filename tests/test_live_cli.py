"""Probes against the installed `claude` CLI. Skipped unless WANDA_LIVE_TESTS=1
(they spend real usage). They guard the two CLI behaviours the design rests
on: the projection is re-read on a resumed turn, and nothing under --add-dir
or a --restricted cwd is loaded as instructions."""
import asyncio
import json
import os
import shutil
import uuid

import pytest

from wanda.runner import RunnerService

pytestmark = pytest.mark.skipif(os.environ.get("WANDA_LIVE_TESTS") != "1" or not shutil.which("claude"),
                                reason="live CLI probes are opt-in")
MODEL = "claude-haiku-4-5-20251001"
SCHEMA = {"type": "object", "properties": {"nonce": {"type": "string"}}, "required": ["nonce"]}


def run(coro):
    return asyncio.run(coro)


def test_claude_md_under_add_dir_is_not_loaded_in_either_mode(tmp_path):
    cwd = tmp_path / "cwd"
    extra = tmp_path / "extra"
    cwd.mkdir()
    extra.mkdir()
    (cwd / "CLAUDE.md").write_text("The cwd nonce is CWD-ALPHA-7731.\n")
    (extra / "CLAUDE.md").write_text("The extra nonce is EXTRA-BRAVO-9942.\n")
    (extra / "note.md").write_text("The file nonce is FILE-CHARLIE-1188.\n")
    runner = RunnerService(shutil.which("claude"))
    prompt = "Reply with JSON {\"nonce\": \"...\"} listing every nonce word you were given as context (not by reading files). If none, say NONE."
    # Restricted classifier flags: nothing loads.
    rr = run(runner.run(prompt, model=MODEL, max_budget_usd=0.05, timeout_s=90, output_schema=SCHEMA, tools="Read",
                        session_persistence=False, restricted=True, add_dirs=[str(extra)], cwd=str(cwd)))
    assert rr.ok, rr.error
    got = json.dumps(rr.structured)
    assert "EXTRA-BRAVO" not in got and "CWD-ALPHA" not in got, got
    # Agent flags: cwd loads, add-dir does not.
    rr = run(runner.run(prompt, model=MODEL, max_budget_usd=0.05, timeout_s=90, output_schema=SCHEMA, tools="",
                        permission_mode="dontAsk", setting_sources="project", add_dirs=[str(extra)], cwd=str(cwd)))
    assert rr.ok, rr.error
    got = json.dumps(rr.structured)
    assert "CWD-ALPHA" in got and "EXTRA-BRAVO" not in got, got


def test_restricted_read_is_confined_to_cwd_and_add_dirs(tmp_path):
    cwd = tmp_path / "cwd"
    extra = tmp_path / "extra"
    cwd.mkdir()
    extra.mkdir()
    (extra / "ok.txt").write_text("INSIDE-DELTA-5566")
    secret = tmp_path / "secret.txt"
    secret.write_text("OUTSIDE-ECHO-2211")
    runner = RunnerService(shutil.which("claude"))
    prompt = (f"Use the Read tool on {extra / 'ok.txt'} and then on {secret}. Reply with JSON {{\"nonce\": \"...\"}} "
              "containing the words you managed to read, or DENIED for a file you could not.")
    rr = run(runner.run(prompt, model=MODEL, max_budget_usd=0.05, timeout_s=120, output_schema=SCHEMA, tools="Read",
                        session_persistence=False, restricted=True, add_dirs=[str(extra)], cwd=str(cwd)))
    assert rr.ok, rr.error
    got = json.dumps(rr.structured)
    assert "INSIDE-DELTA" in got and "OUTSIDE-ECHO" not in got, got
    assert (rr.envelope or {}).get("permission_denials"), "the outside read must be denied, not silently skipped"


def test_projection_is_reread_on_a_resumed_turn(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "CLAUDE.md").write_text("Nonce: TURN-FOXTROT-1\n")
    runner = RunnerService(shutil.which("claude"))
    sid = str(uuid.uuid4())
    ask = "Reply with JSON {\"nonce\": \"...\"} giving the Nonce from your instructions."
    rr = run(runner.run(ask, model=MODEL, max_budget_usd=0.05, timeout_s=90, output_schema=SCHEMA, tools="",
                        permission_mode="dontAsk", setting_sources="project", session_id=sid, cwd=str(ws)))
    assert rr.ok and "FOXTROT-1" in json.dumps(rr.structured), rr.error
    (ws / "CLAUDE.md").write_text("Nonce: TURN-GOLF-2\n")
    rr = run(runner.run(ask, model=MODEL, max_budget_usd=0.05, timeout_s=90, output_schema=SCHEMA, tools="",
                        permission_mode="dontAsk", setting_sources="project", resume=sid, cwd=str(ws)))
    assert rr.ok and "GOLF-2" in json.dumps(rr.structured), rr.error
