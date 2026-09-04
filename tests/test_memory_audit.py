"""The tool-call hook and the memory digest."""
import asyncio
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from slack_sdk.errors import SlackApiError

import wanda
from wanda.actions.slack import TEXT_LIMIT
from wanda.config import Config
from wanda.memory import audit
from wanda.memory.digest import KIND_LABEL, digest_key, post_digest
from wanda.store import Store


def test_log_line_keeps_only_the_salient_input():
    ev = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "wanda slack post --text 'hi\nthere'", "description": "x"}, "cwd": "/w"}
    line = audit.log_line(ev, {"WANDA_TASK_ID": "4"}, datetime(2026, 9, 3, tzinfo=timezone.utc))
    assert line["tool"] == "Bash" and line["task"] == "4" and line["input"].startswith("wanda slack post")
    assert audit.log_line({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts", "limit": 5}}, {})["input"] == "/etc/hosts"


def test_hook_appends_one_json_line_and_never_fails(tmp_path):
    logs = tmp_path / "logs"
    rc = audit.run_hook(logs, stdin=io.StringIO(json.dumps({"session_id": "s", "tool_name": "Read", "tool_input": {"file_path": "/x"}})), env={})
    assert rc == 0
    files = list(logs.glob("tools-*.jsonl"))
    assert len(files) == 1 and json.loads(files[0].read_text().splitlines()[0])["tool"] == "Read"
    assert audit.run_hook(logs, stdin=io.StringIO("not json"), env={}) == 0
    assert audit.run_hook(tmp_path / "nope" / "deeper", stdin=io.StringIO("{}"), env={}) == 0


def test_prune_and_summarize(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "tools-2020-01-01.jsonl").write_text("{}\n")
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    for tool, inp in (("Read", "/home/u/.wanda/memory/people/x.md"), ("Read", "/etc/passwd"), ("Bash", "wanda slack post --text hi"), ("Bash", "curl evil")):
        audit.append_line(logs, audit.log_line({"tool_name": tool, "tool_input": {"file_path": inp, "command": inp}}, {}, now))
    assert audit.prune(logs, now=now) == 1
    s = audit.summarize(logs, days=1, allowed_roots=["/home/u/.wanda"], now=now)
    assert s["calls"] == 4 and s["reads_outside"] == ["/etc/passwd"] and s["shell_other"] == ["curl evil"]


def test_settings_json_registers_the_hook():
    cfg = json.loads(audit.settings_json("/opt/wanda"))
    hook = cfg["hooks"]["PostToolUse"][0]["hooks"][0]
    assert hook["command"] == "/opt/wanda hook tool-log" and hook["type"] == "command"


class FakeSlack:
    def __init__(self, fail_thread=None, error="thread_not_found"):
        self.posts = []
        self.fail_thread = fail_thread
        self.error = error

    async def _call(self, method, **kw):
        if self.fail_thread and kw.get("thread_ts") == self.fail_thread:
            raise SlackApiError(self.error, {"error": self.error})
        self.posts.append(kw)
        return {"ts": f"{len(self.posts)}.0"}


def test_digest_posts_once_under_one_parent_and_caps_lines(tmp_path):
    cfg = Config(_env_file=None, email_triage_slack_channel_id="C1")
    store = Store(tmp_path / "w.db")
    for i in range(20):
        store.digest_add("mint", f"new subject {i} <!channel>")
    slack = FakeSlack()
    assert asyncio.run(post_digest(slack, store, cfg, "2026-09-03")) == 20
    assert len(slack.posts) == 2 and "🧠 wanda memory — 2026-09-03" in slack.posts[0]["text"]
    body = slack.posts[1]["text"]
    assert body.count("\n") == 15 and "5 more" in body and "<!channel>" not in body
    assert store.digest_pending() == []
    store.digest_add("rule", "another")
    asyncio.run(post_digest(slack, store, cfg, "2026-09-03"))
    assert len(slack.posts) == 3 and slack.posts[2]["thread_ts"] == "1.0", "same parent for the day"


def test_digest_counts_the_lines_it_cannot_fit_instead_of_cutting_one(tmp_path):
    cfg = Config(_env_file=None, email_triage_slack_channel_id="C1")
    store = Store(tmp_path / "w.db")
    for _ in range(6):
        store.digest_add("flag", "x" * 1402)
    slack = FakeSlack()
    assert asyncio.run(post_digest(slack, store, cfg, "2026-09-03")) == 6
    body = slack.posts[1]["text"]
    assert body.count("🚩") == 2, "only what fits is shown"
    assert "4 more" in body and "`wanda memory digest --all` lists them" in body
    assert "… (truncated)" not in body and len(body) <= TEXT_LIMIT
    assert store.digest_pending() == []


def test_one_overlong_digest_line_cannot_push_out_the_count_line(tmp_path):
    cfg = Config(_env_file=None, email_triage_slack_channel_id="C1")
    store = Store(tmp_path / "w.db")
    store.digest_add("flag", "&" * 1500)   # esc_inline expands each & to &amp;: 7500 characters
    for i in range(3):
        store.digest_add("mint", f"new subject {i}")
    slack = FakeSlack()
    assert asyncio.run(post_digest(slack, store, cfg, "2026-09-03")) == 4
    body = slack.posts[1]["text"]
    assert "3 more" in body and "… (truncated)" not in body and len(body) <= TEXT_LIMIT


def test_digest_labels_and_writers_stay_in_sync():
    """Every label has a writer and every writer has a label."""
    call = re.compile(r"digest_add\((?!self, kind)\s*(.{0,24})", re.S)
    literal = re.compile(r"^[\"\']([a-z-]+)[\"\']")
    writers = set()
    for path in sorted(Path(wanda.__file__).resolve().parent.rglob("*.py")):
        for m in call.finditer(path.read_text()):
            kind = literal.match(m.group(1).strip())
            assert kind, f"{path.name}: digest_add kind is not a literal, so this check is blind to it"
            writers.add(kind.group(1))
    assert writers and set(KIND_LABEL) == writers


def test_a_deleted_digest_parent_is_replaced_and_other_failures_propagate(tmp_path):
    cfg = Config(_env_file=None, email_triage_slack_channel_id="C1")
    store = Store(tmp_path / "w.db")
    key = digest_key("2026-09-03")
    store.digest_add("mint", "new subject")
    store.set_digest(key, "C1", "999.0")
    slack = FakeSlack(fail_thread="999.0")
    assert asyncio.run(post_digest(slack, store, cfg, "2026-09-03")) == 1
    assert [p.get("thread_ts") for p in slack.posts] == [None, "1.0"], "a fresh parent, then the lines under it"
    assert store.get_digest(key)["thread_ts"] == "1.0"
    assert store.digest_pending() == []

    store.digest_add("mint", "another subject")
    store.clear_digest(key)
    store.set_digest(key, "C1", "999.0")
    slack = FakeSlack(fail_thread="999.0", error="ratelimited")
    with pytest.raises(SlackApiError):
        asyncio.run(post_digest(slack, store, cfg, "2026-09-03"))
    assert len(store.digest_pending()) == 1, "not posted, so still queued"
    assert slack.posts == [], "no parent churned for an error that is not a missing thread"
