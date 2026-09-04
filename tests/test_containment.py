"""A1/A2: the sender's prose reaches only the sandboxed triage classifier.
Neither the Slack task post nor the agent seed carries the body."""
import json

import pytest

from wanda.actions.slack import SlackActions
from wanda.config import Config
from wanda.main import agent_seed_prompt
from wanda.store import Store
from wanda.triage import Verdict


def cfg(**kw) -> Config:
    return Config(_env_file=None, email_triage_slack_channel_id="C1", **kw)


def a_verdict() -> Verdict:
    return Verdict(id="e1", action="attention", summary="Bank statement is ready",
                   reason="looks like a routine account notice", urgency="low", confidence=0.9)


def test_post_task_carries_no_body(monkeypatch, tmp_path):
    store = Store(tmp_path / "d.db")
    actions = SlackActions(cfg(slack_bot_token="xoxb-test"), store)
    captured = {}

    async def fake_call(method, /, **kwargs):
        captured["method"] = method
        captured["text"] = kwargs.get("text", "")
        return {"ts": "1.1"}

    monkeypatch.setattr(actions, "_call", fake_call)
    row = {"from_addr": "bank@example.com", "subject": "Statement", "dedupe_key": "k1"}
    import asyncio
    ts = asyncio.run(actions.post_task(row, a_verdict()))
    assert ts == "1.1"
    text = captured["text"]
    assert "Bank statement is ready" in text          # verdict summary
    assert "bank@example.com" in text and "Statement" in text  # headers
    assert "```" not in text                           # no body fence
    store.close()


def test_agent_seed_carries_verdict_not_body():
    row = {
        "from_addr": "bank@example.com", "subject": "Statement", "date_hdr": "Mon",
        "verdict_json": json.dumps({"summary": "Bank statement ready",
                                    "reason": "routine notice", "urgency": "low"}),
    }
    seed = agent_seed_prompt(row, "summarise this for me", memory="", memory_on=False)
    assert "Bank statement ready" in seed and "routine notice" in seed
    assert "do not have the message body" in seed


def test_agent_seed_handles_missing_verdict():
    row = {"from_addr": "a@b.c", "subject": "s", "date_hdr": "d", "verdict_json": ""}
    seed = agent_seed_prompt(row, "do it", memory="", memory_on=False)
    assert "none recorded" in seed
