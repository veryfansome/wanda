"""Mention/DM triggering, context rendering, and task anchoring."""

import asyncio
import os
from types import SimpleNamespace

import pytest

from wanda.config import Config
from wanda.store import Store
from wanda.transcript import humanize, render, trim_thread, user_ids_in
from wanda.watchers.slack_watcher import SlackWatcher


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("WANDA_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "w.db")
    yield s
    s.close()


def cfg(**kw) -> Config:
    return Config(_env_file=None, email_triage_slack_channel_id="C_TRIAGE", **kw)


class FakeReq:
    def __init__(self, event, event_id="ev1"):
        self.type = "events_api"
        self.envelope_id = "env1"
        self.payload = {"event": event, "event_id": event_id}


def watcher(store, **kw):
    loop = asyncio.new_event_loop()
    q = asyncio.Queue()
    w = SlackWatcher(cfg(**kw), store, loop, q)
    w.bot_user_id = "UBOT"
    w.client = SimpleNamespace(send_socket_mode_response=lambda r: None)
    return w, q, loop


def Store_fresh(store):
    """A second store so a dedupe key from one probe doesn't hide the other."""
    import tempfile
    from pathlib import Path as _P
    return Store(_P(tempfile.mkdtemp()) / "w2.db")


def fire(store, event, **kw):
    w, q, loop = watcher(store, **kw)
    w._handle(w.client, FakeReq(event))
    loop.run_until_complete(asyncio.sleep(0))  # let call_soon_threadsafe land
    loop.close()
    return None if q.empty() else q.get_nowait()


def test_channel_mention_triggers(store):
    ev = fire(store, {"type": "message", "user": "U1", "channel": "C9",
                      "channel_type": "channel", "ts": "100.1", "text": "<@UBOT> hi"})
    assert ev is not None
    assert ev.payload["kind"] == "mention"
    # A top-level mention anchors its task and its replies to its own ts.
    assert ev.payload["task_key"] == "100.1" and ev.payload["reply_thread"] == "100.1"
    assert ev.payload["in_thread"] is False


def test_threaded_mention_is_a_guest(store):
    """A mention inside someone else's thread joins as a guest: wanda answers
    it, but must not then treat the whole human conversation as its own."""
    ev = fire(store, {"type": "message", "user": "U1", "channel": "C9", "channel_type": "channel",
                      "ts": "100.9", "thread_ts": "100.1", "text": "<@UBOT> and this?"})
    assert ev.payload["kind"] == "mention_guest"
    assert ev.payload["task_key"] == "100.1" and ev.payload["reply_thread"] == "100.1"
    assert ev.payload["in_thread"] is True


def test_guest_thread_does_not_capture_later_messages(store):
    """One @wanda in a human thread used to make wanda answer every later
    message there, forever, with no way to disengage."""
    store.create_task(None, "C9", "100.1", kind="mention_guest")
    assert fire(store, {"type": "message", "user": "U2", "channel": "C9", "channel_type": "channel",
                        "ts": "100.9", "thread_ts": "100.1", "text": "yeah agreed"}) is None
    # An explicit mention still gets an answer.
    assert fire(store, {"type": "message", "user": "U2", "channel": "C9", "channel_type": "channel",
                        "ts": "101.0", "thread_ts": "100.1", "text": "<@UBOT> thoughts?"}) is not None


def test_dm_with_a_mention_is_still_a_dm(store):
    """Conversation type wins over the presence of a mention, so a DM stays one
    resumable conversation however the user phrases it."""
    ev = fire(store, {"type": "message", "user": "U1", "channel": "D5", "channel_type": "im",
                      "ts": "7.7", "text": "<@UBOT> hi"})
    assert ev.payload["kind"] == "dm"
    assert ev.payload["task_key"] == "conversation" and ev.payload["reply_thread"] is None


@pytest.mark.parametrize("ctype", ["im", "mpim"])
def test_dm_triggers_without_mention(store, ctype):
    ev = fire(store, {"type": "message", "user": "U1", "channel": "D5",
                      "channel_type": ctype, "ts": "1.1", "text": "hey"})
    assert ev is not None and ev.payload["kind"] == "dm"
    assert ev.payload["channel_type"] == ctype


def test_plain_channel_chatter_is_ignored(store):
    """Messages that don't address wanda must not spawn sessions."""
    assert fire(store, {"type": "message", "user": "U1", "channel": "C9",
                        "channel_type": "channel", "ts": "1.1", "text": "morning all"}) is None


def test_reply_in_owned_thread_triggers(store):
    store.create_task(None, "C_TRIAGE", "77.1", kind="email")
    ev = fire(store, {"type": "message", "user": "U1", "channel": "C_TRIAGE",
                      "channel_type": "channel", "ts": "77.2", "thread_ts": "77.1", "text": "do it"})
    assert ev is not None and ev.payload["kind"] == "task"


def test_bot_and_self_messages_ignored(store):
    assert fire(store, {"type": "message", "user": "UBOT", "channel": "D5",
                        "channel_type": "im", "ts": "1.1", "text": "x"}) is None
    assert fire(store, {"type": "message", "bot_id": "B1", "user": "U2", "channel": "D5",
                        "channel_type": "im", "ts": "1.2", "text": "x"}) is None


def test_owner_list_restricts_when_set(store):
    ev = {"type": "message", "user": "U_STRANGER", "channel": "C9", "channel_type": "channel",
          "ts": "1.1", "text": "<@UBOT> hi"}
    assert fire(store, ev, slack_owner_user_ids=["U_ME"]) is None
    assert fire(store, ev) is not None  # empty list = anyone


def test_app_mention_twin_is_ignored(store):
    """Slack sends app_mention alongside message.* for the same text. Handling
    both ran the agent twice; only the message event is used now."""
    store.create_task(None, "C_TRIAGE", "77.1", kind="email")
    w, q, loop = watcher(store)
    common = {"user": "U1", "channel": "C_TRIAGE", "ts": "77.5", "thread_ts": "77.1",
              "text": "<@UBOT> go ahead"}
    w._handle(w.client, FakeReq({**common, "type": "app_mention"}, event_id="Ev_A"))
    w._handle(w.client, FakeReq({**common, "type": "message", "channel_type": "channel"},
                                event_id="Ev_B"))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    assert q.qsize() == 1, "one user message must produce exactly one trigger"


def test_dm_conversation_is_one_resumable_task(store):
    """Every top-level DM message must map to the same task, so the session
    resumes instead of starting fresh each time."""
    first = fire(store, {"type": "message", "user": "U1", "channel": "D5",
                         "channel_type": "im", "ts": "1.1", "text": "hi"})
    second = fire(store, {"type": "message", "user": "U1", "channel": "D5",
                          "channel_type": "im", "ts": "2.2", "text": "and another thing"})
    assert first.payload["task_key"] == second.payload["task_key"]
    # Unthreaded, so wanda's own replies stay visible in conversations.history.
    assert first.payload["reply_thread"] is None and second.payload["reply_thread"] is None


def test_duplicate_event_id_ignored(store):
    w, q, loop = watcher(store)
    ev = {"type": "message", "user": "U1", "channel": "C9", "channel_type": "channel",
          "ts": "1.1", "text": "<@UBOT> hi"}
    w._handle(w.client, FakeReq(ev))
    w._handle(w.client, FakeReq(ev))  # Slack redelivery
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    assert q.qsize() == 1


# --- transcript rendering ---

def test_render_resolves_names_and_links():
    msgs = [
        {"user": "U1", "ts": "1700000000", "text": "hey <@U2> see <https://x.test|the doc>"},
        {"user": "U2", "ts": "1700000060", "text": "ok"},
    ]
    out = render(msgs, {"U1": "alice", "U2": "bob"})
    assert "alice: hey @bob" in out
    assert "the doc (https://x.test)" in out
    assert "bob: ok" in out


def test_render_skips_joins_and_empty():
    out = render([{"user": "U1", "ts": "1", "subtype": "channel_join", "text": "joined"},
                  {"user": "U1", "ts": "2", "text": "   "}], {"U1": "alice"})
    assert out == "(no readable messages)"


def test_user_ids_includes_mentions():
    assert user_ids_in([{"user": "U1", "text": "ping <@U2> and <@U3|bob>"}]) == {"U1", "U2", "U3"}


def test_humanize_leaves_plain_text():
    assert humanize("just words", {}) == "just words"


# --- task anchoring ---

def test_mention_task_needs_no_email(store):
    tid = store.create_task(None, "C9", "100.1", kind="mention")
    row = store.get_task_by_thread("C9", "100.1")
    assert row["id"] == tid and row["message_pk"] is None and row["kind"] == "mention"


def test_same_thread_reuses_one_task(store):
    a = store.create_task(None, "C9", "100.1", kind="mention")
    b = store.create_task(None, "C9", "100.1", kind="mention")
    assert a == b, "a follow-up mention must resume the same session, not fork one"


# --- thread trimming ---

@pytest.mark.parametrize("limit,expected", [
    (0, []),
    (1, ["m5"]),                       # msgs[-0:] is the WHOLE list, not empty
    (2, ["m0", "m5"]),                 # parent + newest
    (3, ["m0", "m4", "m5"]),
    (6, ["m0", "m1", "m2", "m3", "m4", "m5"]),
    (99, ["m0", "m1", "m2", "m3", "m4", "m5"]),
])
def test_trim_thread_keeps_parent_and_newest(limit, expected):
    msgs = [{"id": f"m{i}"} for i in range(6)]
    assert [m["id"] for m in trim_thread(msgs, limit)] == expected


def test_labelled_mention_form_is_detected(store):
    """<@U123|name> is a real Slack form; the repo's own parser accepts it, so
    the trigger path must too or the person gets no reply at all."""
    ev = fire(store, {"type": "message", "user": "U1", "channel": "C9", "channel_type": "channel",
                      "ts": "9.9", "text": "<@UBOT|wanda> hi"})
    assert ev is not None and ev.payload["kind"] == "mention"


def test_mention_in_wandas_own_thread_stays_a_task(store):
    """A mention inside a thread wanda owns must resume that task, not open a
    competing guest task keyed to the same thread."""
    store.create_task(None, "C_TRIAGE", "77.1", kind="email")
    ev = fire(store, {"type": "message", "user": "U1", "channel": "C_TRIAGE",
                      "channel_type": "channel", "ts": "77.9", "thread_ts": "77.1",
                      "text": "<@UBOT> handle this"})
    assert ev.payload["kind"] == "task"


def test_owned_thread_replies_work_without_a_mention(store):
    store.create_task(None, "C9", "100.1", kind="mention")
    ev = fire(store, {"type": "message", "user": "U1", "channel": "C9", "channel_type": "channel",
                      "ts": "100.5", "thread_ts": "100.1", "text": "and the other one?"})
    assert ev is not None and ev.payload["kind"] == "task"
