"""The tool-call hook and the memory digest."""
import asyncio
import io
import json
from datetime import datetime, timezone

from wanda.config import Config
from wanda.memory import audit
from wanda.memory.digest import post_digest
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
    def __init__(self):
        self.posts = []

    async def _call(self, method, **kw):
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
