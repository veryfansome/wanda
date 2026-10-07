"""Mention/DM triggering, context rendering, and task anchoring."""

import asyncio
import os
import sqlite3
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


def test_owner_list_restricts_when_set(store, caplog):
    ev = {"type": "message", "user": "U_STRANGER", "channel": "C9", "channel_type": "channel",
          "ts": "1.1", "text": "<@UBOT> hi"}
    assert fire(store, ev, slack_owner_user_ids=["U_ME"]) is None
    assert fire(store, ev) is not None  # empty list = anyone
    # a DM, a group DM's lines and a reply in her thread start nothing either,
    # each logged once per person and conversation
    store.create_task(None, "C9", "50.1", kind="mention")
    caplog.clear()
    w, q, loop = watcher(store, slack_owner_user_ids=["U_ME"])
    for i, (channel, kind, thread) in enumerate((("D5", "im", None), ("G5", "mpim", None), ("G5", "mpim", None),
                                                 ("C9", "channel", "50.1"))):
        w._handle(w.client, FakeReq({"type": "message", "user": "U_STRANGER", "channel": channel,
                                     "channel_type": kind, "ts": f"60.{i}", "text": "hi",
                                     **({"thread_ts": thread} if thread else {})}))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    assert q.empty()
    assert [r.getMessage() for r in caplog.records if "non-allowed" in r.getMessage()] == [
        "ignoring dm from non-allowed user U_STRANGER in D5", "ignoring dm from non-allowed user U_STRANGER in G5",
        "ignoring task from non-allowed user U_STRANGER in C9"]


def test_a_reply_under_an_alert_in_a_channel_is_hers(store, monkeypatch):
    """The alert records its thread, so a reply there needs no @wanda."""
    import wanda.actions.slack as actions

    class Web:
        def chat_postMessage(self, **kw):
            return {"ok": True, "channel": "C_ALERTS", "ts": "70.1"}

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(alert_channel="C_ALERTS"), store)
    sa.web = Web()
    asyncio.run(sa.alert("a vault snapshot failed"))
    ev = fire(store, {"type": "message", "user": "U1", "channel": "C_ALERTS", "channel_type": "group",
                      "ts": "70.5", "thread_ts": "70.1", "text": "what does this mean?"})
    assert ev is not None and ev.payload["kind"] == "task" and ev.payload["reply_thread"] == "70.1"


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


def test_a_message_to_her_is_kept_until_it_is_answered(store):
    """Every member's message to her, wherever it is, a DM's first included,
    before its conversation has a task; not a reply in an email task's
    thread, nor one starting nothing."""
    store.create_task(None, "C_TRIAGE", "77.1", kind="email")
    store.create_task(None, "C9", "50.1", kind="mention")
    for i, event in enumerate(({"channel": "D5", "channel_type": "im", "text": "hi"},
                               {"channel": "C9", "channel_type": "channel", "text": "<@UBOT> hi"},
                               {"channel": "C9", "channel_type": "channel", "text": "<@UBOT> hi", "thread_ts": "8.8"},
                               {"channel": "C9", "channel_type": "channel", "text": "and", "thread_ts": "50.1"},
                               {"channel": "C_TRIAGE", "channel_type": "channel", "text": "do it", "thread_ts": "77.1"},
                               {"channel": "C9", "channel_type": "channel", "text": "morning all"})):
        fire(store, {"type": "message", "user": "U1", "ts": f"90.{i}", **event})
    assert [(r["channel"], r["ts"], r["task_key"]) for r in store.kept()] == [
        ("D5", "90.0", "conversation"), ("C9", "90.1", "90.1"), ("C9", "90.2", "8.8"), ("C9", "90.3", "50.1")]
    assert store.get_task_by_thread("D5", "conversation") is None


def test_the_envelope_is_acknowledged_once_the_message_is_kept(store, monkeypatch):
    """Slack sends nothing again once it is acknowledged: a message lost to
    a stop or a crash between the two would be lost for good. Acknowledged
    too when keeping it raises."""
    w, q, loop = watcher(store)
    acked = []
    w.client = SimpleNamespace(send_socket_mode_response=lambda r: acked.append(
        (r.envelope_id, [x["ts"] for x in store.kept()])))
    dm = {"type": "message", "user": "U1", "channel": "D5", "channel_type": "im", "text": "hi"}
    w._handle(w.client, FakeReq({**dm, "ts": "1.1"}))
    assert acked == [("env1", ["1.1"])]

    def full(key, payload=None):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(store, "first_time", full)
    with pytest.raises(sqlite3.OperationalError):
        w._handle(w.client, FakeReq({**dm, "ts": "2.2"}))
    loop.close()
    assert acked == [("env1", ["1.1"]), ("env1", ["1.1"])]


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


def test_render_labels_her_own_messages_me():
    """wanda reads this transcript, so what she posted is hers: labelled "me",
    whether Slack carries her bot user id or only her bot id. A mention of her
    keeps the name the writer used."""
    msgs = [
        {"user": "U1", "ts": "1", "text": "<@UBOT> can you check the invoice?"},
        {"user": "UBOT", "bot_id": "BME", "ts": "2", "text": "on it"},
        {"bot_id": "BME", "ts": "3", "text": "it is due Friday"},
        {"bot_id": "BOTHER", "username": "deploybot", "ts": "4", "text": "deployed"},
    ]
    out = render(msgs, {"U1": "alice", "UBOT": "wanda"}, me=frozenset({"UBOT", "BME"}))
    assert "alice: @wanda can you check the invoice?" in out
    assert "me: on it" in out and "me: it is due Friday" in out
    assert "deploybot: deployed" in out
    assert "wanda:" not in out


def test_render_without_her_ids_keeps_display_names():
    out = render([{"user": "UBOT", "ts": "1", "text": "on it"}], {"UBOT": "wanda"})
    assert "wanda: on it" in out


def test_cli_labels_her_own_posts_and_row_me(monkeypatch, capsys):
    """`wanda slack` is how a session reads more context, so its listings
    label wanda "me" the way the seed transcript does."""
    from wanda import slack_cli

    class Web:
        def auth_test(self):
            return {"user_id": "UBOT", "bot_id": "BME"}

        def users_info(self, user):
            return {"user": {"profile": {"display_name": {"U1": "alice", "UBOT": "wanda"}[user]}}}

        def conversations_history(self, channel, limit):
            return {"messages": [{"user": "UBOT", "ts": "2", "text": "on it"},
                                 {"user": "U1", "ts": "1", "text": "<@UBOT> can you check?"}]}

        def conversations_members(self, **kw):
            return {"members": ["U1", "UBOT"]}

        def search_messages(self, query, count):
            return {"messages": {"matches": [
                {"channel": {"name": "general"}, "user": "U1", "username": "alice", "text": "invoice?"},
                {"channel": {"name": "general"}, "user": "UBOT", "username": "wanda", "text": "due Friday"},
                {"channel": {"name": "general"}, "bot_id": "BME", "username": "wanda", "text": "paid"},
            ]}}

    monkeypatch.setattr(slack_cli, "_client", lambda cfg, user_token=False: Web())
    for args in (SimpleNamespace(verb="history", channel="C9", limit=50, json=False),
                 SimpleNamespace(verb="search", query="invoice", limit=20),
                 SimpleNamespace(verb="members", channel="C9", limit=200)):
        assert slack_cli.run(cfg(), args) == 0
    out = capsys.readouterr().out
    assert "alice: @wanda can you check?" in out and "me: on it" in out
    assert "[general] alice: invoice?" in out
    assert "[general] me: due Friday" in out and "[general] me: paid" in out
    assert "U1\talice" in out and "UBOT\tme" in out
    assert "wanda:" not in out and "\twanda" not in out


@pytest.mark.parametrize("error", ["a passing failure", "a Slack error"])
def test_a_failed_user_lookup_is_not_kept(monkeypatch, error):
    """A passing failure, or any Slack error but the one saying it shows no
    one, costs one message its names, not every message until a restart."""
    import wanda.actions.slack as actions
    from slack_sdk.errors import SlackApiError

    class Web:
        up, calls = False, 0

        def users_info(self, user):
            self.calls += 1
            if not self.up:
                if error == "a Slack error":
                    raise SlackApiError("The request to the Slack API failed.", {"ok": False, "error": "ratelimited"})
                raise RuntimeError("ratelimited")
            return {"user": {"id": user, "profile": {"display_name": "jane"}}}

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(), store=None)
    sa.web = Web()

    async def lookups():
        assert await sa.users({"U9"}) == {}
        sa.web.up = True
        assert (await sa.users({"U9"}))["U9"]["profile"]["display_name"] == "jane"
        await sa.users({"U9"})

    asyncio.run(lookups())
    assert sa.web.calls == 2, "a failure is retried and a success is kept"


def test_an_id_slack_shows_no_one_for_is_asked_once(monkeypatch):
    """Slack's answer that it shows no one stands; held as an empty record,
    it is not asked again at every frame."""
    import wanda.actions.slack as actions
    from slack_sdk.errors import SlackApiError

    class Web:
        calls = 0

        def users_info(self, user):
            self.calls += 1
            raise SlackApiError("The request to the Slack API failed.", {"ok": False, "error": "user_not_found"})

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(), store=None)
    sa.web = Web()

    async def lookups():
        assert await sa.users({"U9"}) == {"U9": {}}
        assert await sa.users({"U9"}) == {"U9": {}}

    asyncio.run(lookups())
    assert sa.web.calls == 1 and sa.kept(["U9", "U8"]) == {"U9": {}}


class Pages:
    """A Slack that answers every list with a page and a cursor, as one past
    ten pages does."""

    def __init__(self):
        self.calls = 0

    def _page(self, member):
        self.calls += 1
        return {"members": [member], "response_metadata": {"next_cursor": f"c{self.calls}"}}

    def conversations_members(self, **kw):
        return self._page(f"U{self.calls}")

    def users_list(self, **kw):
        return self._page({"id": f"U{self.calls}"})


def test_a_list_of_readers_cut_short_by_its_pages_raises(monkeypatch):
    """Ten pages that end with a cursor still given would leave readers out,
    so the member list and the workspace raise."""
    import wanda.actions.slack as actions

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(), store=None)
    sa.web = Pages()
    with pytest.raises(RuntimeError, match="10 pages"):
        asyncio.run(sa.members("C1"))
    with pytest.raises(RuntimeError, match="10 pages"):
        asyncio.run(sa.workspace())
    assert sa.web.calls == 20


def test_the_workspace_read_is_kept_as_lookups_are(monkeypatch):
    import wanda.actions.slack as actions

    class Web:
        def users_list(self, **kw):
            return {"members": [{"id": "U1", "profile": {"display_name": "fan"}}, {"id": "U3", "deleted": True}]}

        def users_info(self, user):
            raise AssertionError("a person the workspace read gave is not looked up")

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(), store=None)
    sa.web = Web()
    asyncio.run(sa.workspace())
    assert sa.kept(["U1", "U3", "U9"]) == {"U1": {"id": "U1", "profile": {"display_name": "fan"}},
                                            "U3": {"id": "U3", "deleted": True}}
    assert asyncio.run(sa.users({"U1"}))["U1"]["profile"]["display_name"] == "fan"


class History:
    """Slack's two reads as its documentation gives them: history newest
    first, replies earliest first from the thread's first message, 200 a
    page, `oldest` exclusive."""

    def __init__(self, msgs):
        self.msgs, self.calls = sorted(msgs, key=lambda m: float(m["ts"])), []

    def _page(self, rows, kw):
        at = int(kw.get("cursor") or 0)
        more = at + 200 < len(rows)
        return {"messages": rows[at:at + 200], "has_more": more,
                "response_metadata": {"next_cursor": str(at + 200) if more else ""}}

    def conversations_history(self, **kw):
        self.calls.append(("history", kw))
        oldest = float(kw.get("oldest") or 0)
        return self._page([m for m in reversed(self.msgs) if float(m["ts"]) > oldest], kw)

    def conversations_replies(self, **kw):
        self.calls.append(("replies", kw))
        oldest = float(kw["oldest"]) if "oldest" in kw else None
        return self._page([m for m in self.msgs if oldest is None or float(m["ts"]) > oldest], kw)


def test_the_context_is_read_back_to_a_time_until_the_households_lines_are_read(monkeypatch):
    """Outside a thread, history back to `since`, paged, and no further than
    the page on which 20 of the messages read are the household's: every
    line a frame can show is read by then. A 1:1 DM's last 12 hours is one
    call."""
    import wanda.actions.slack as actions

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(), store=None)
    now, since = 100_000.0, 100_000.0 - 12 * 3600
    old = [{"ts": f"{since - 60 + i:.6f}", "user": "U1", "text": "yesterday"} for i in range(5)]
    five = [{"ts": f"{now - 9000 + i:.6f}", "user": "U1", "text": "ours"} for i in range(5)]
    theirs = [{"ts": f"{now - 8000 + i:.6f}", "user": "U3", "text": "theirs"} for i in range(300)]
    sa.web = History(old + five + theirs)
    got = asyncio.run(sa.fetch_context("C1", None, since, lambda m: m["user"] == "U1"))
    assert got == five + theirs
    assert [kw["oldest"] for _, kw in sa.web.calls] == [f"{since:.6f}"] * 2
    assert all(kw["include_all_metadata"] is True and kw["limit"] == 200 for _, kw in sa.web.calls)
    # ten of the household's lines on each page of 200, newest first
    straddle = [{"ts": f"{now - 20000 + i:.6f}", "user": "U1" if i % 20 == 0 else "U3", "text": "x"}
                for i in range(700)]
    sa.web = History(straddle)
    got = asyncio.run(sa.fetch_context("C1", None, since, lambda m: m["user"] == "U1"))
    assert len(sa.web.calls) == 2 and got == straddle[300:]
    assert [m for m in got if m["user"] == "U1"] == [m for m in straddle if m["user"] == "U1"][-20:]
    dm = [{"ts": f"{now - 3000 + i:.6f}", "user": "U1" if i % 2 else "UBOT", "text": "x"} for i in range(500)]
    sa.web = History(dm)
    got = asyncio.run(sa.fetch_context("D1", None, since, lambda m: True))
    assert len(sa.web.calls) == 1 and got == dm[-200:]
    sa.web = History(dm)
    assert asyncio.run(sa.fetch_context("D1", None, since)) == dm and len(sa.web.calls) == 3


def test_a_thread_is_read_whole_and_past_its_pages_again_from_a_time(monkeypatch):
    """A thread is not trimmed: what a frame shows is picked from all of it.
    Past ten pages it is read again from `since`, and both reads are kept,
    each message once: 2,000 old replies keep neither its newest nor the
    household's among its earliest from being read."""
    from datetime import datetime, timezone

    import wanda.actions.slack as actions
    from wanda import vault

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(), store=None)
    now, since = 100_000.0, 100_000.0 - 12 * 3600
    parent = {"ts": f"{since - 20000:.6f}", "user": "U1", "text": "the plan"}
    short = [parent] + [{"ts": f"{now - 500 + i:.6f}", "user": "U3", "text": "x"} for i in range(300)]
    sa.web = History(short)
    assert asyncio.run(sa.fetch_context("C1", parent["ts"], since)) == short
    assert [name for name, _ in sa.web.calls] == ["replies"] * 2
    flood = [{"ts": f"{since - 19000 + i:.6f}", "user": "U3", "text": "x"} for i in range(2000)]
    recent = [{"ts": f"{now - 5000 + i:.6f}", "user": "U1" if i % 3 == 0 else "U3", "text": "y"} for i in range(90)]
    sa.web = History([parent] + flood + recent)
    assert asyncio.run(sa.fetch_context("C1", parent["ts"], since)) == [parent] + flood[:1999] + recent
    assert len(sa.web.calls) == 11 and sa.web.calls[-1][1]["oldest"] == f"{since:.6f}"
    # fan's 49 replies, then an app's 2,000 over the days since, then someone's in the last hour
    first = {"ts": f"{since - 100000:.6f}", "user": "U1", "text": "the plan"}
    ours = [{"ts": f"{since - 90000 + i:.6f}", "user": "U1", "text": "ours"} for i in range(49)]
    feed = [{"ts": f"{since - 80000 + 30 * i:.6f}", "user": "U7", "bot_id": "B7", "text": "feed"}
            for i in range(2000)]
    last = [{"ts": f"{now - 3000 + i:.6f}", "user": "U3", "text": "z"} for i in range(5)]
    sa.web = History([first] + ours + feed + last)
    got = asyncio.run(sa.fetch_context("C1", first["ts"], since))
    shown = vault.shown(got, f"{now:.6f}", "thread", frozenset({"UBOT"}), datetime.fromtimestamp(now, timezone.utc),
                        kin=["U1"])
    assert shown[0] is first and [m for m in shown[1:] if m["user"] == "U1"] == ours

    class Parent(History):
        """Replies that give the thread's first message on every read."""

        def conversations_replies(self, **kw):
            page = super().conversations_replies(**kw)
            if "oldest" in kw and not kw.get("cursor"):
                page["messages"] = [self.msgs[0], *page["messages"]]
            return page
    sa.web = Parent([parent] + flood + recent)
    got = asyncio.run(sa.fetch_context("C1", parent["ts"], since))
    assert got == [parent] + flood[:1999] + recent


def test_a_members_name_is_read_now_on_a_client_of_its_own(monkeypatch):
    """users.info for one id, read past what is kept and kept in its place,
    with the daemon's trust store, ten seconds and no retry; a failure
    raises and leaves what was kept. Outside the pacing lock posts wait on:
    a read that hangs holds up no post."""
    import threading

    import wanda.actions.slack as actions
    from wanda.tls import ssl_context

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(slack_bot_token="xoxb-x"), store=None)
    assert sa.names_web is not sa.web
    assert (sa.names_web.ssl, sa.names_web.timeout, sa.names_web.retry_handlers) == (ssl_context(), 10, [])

    class Names:
        fail = hang = False
        gate = threading.Event()

        def users_info(self, user):
            if self.hang:
                self.gate.wait(5)
            if self.fail:
                raise RuntimeError("timed out")
            return {"user": {"id": user, "profile": {"display_name": "fan"}}}

    class Posts:
        posted = []

        def chat_postMessage(self, **kw):
            self.posted.append(kw["text"])
            return {"ok": True}

        def users_info(self, user):
            raise AssertionError("a name read now is not read on the posts' client")

    sa.names_web, sa.web = Names(), Posts()
    sa._users["U1"] = {"id": "U1", "profile": {"display_name": "fzhu"}}

    async def go():
        assert (await sa.user_now("U1"))["profile"]["display_name"] == "fan"
        assert (await sa.users({"U1"}))["U1"]["profile"]["display_name"] == "fan"
        sa.names_web.fail = True
        with pytest.raises(RuntimeError, match="timed out"):
            await sa.user_now("U1")
        assert sa._users["U1"]["profile"]["display_name"] == "fan"
        sa.names_web.fail, sa.names_web.hang = False, True
        reading = asyncio.create_task(sa.user_now("U1"))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(sa.reply(None, "Will do.", channel="D1"), 2)
        assert not reading.done()
        sa.names_web.gate.set()
        await reading

    asyncio.run(go())
    assert sa.web.posted == ["Will do."]


def test_her_reaction_is_put_on_and_taken_off_on_the_names_client(monkeypatch):
    """`eyes`, outside the pacing lock posts wait on: a call that hangs holds
    up no post. Slack's already_reacted is on; no_reaction, message_not_found
    and channel_not_found on a removal are off; anything else raises."""
    import threading

    from slack_sdk.errors import SlackApiError

    import wanda.actions.slack as actions

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(slack_bot_token="xoxb-x"), store=None)

    class Names:
        calls = []
        error, hang = None, False
        gate = threading.Event()

        def reactions_add(self, **kw):
            return self.answer("add", kw)

        def reactions_remove(self, **kw):
            return self.answer("remove", kw)

        def answer(self, what, kw):
            self.calls.append((what, kw))
            if self.hang:
                self.gate.wait(5)
            if self.error:
                raise SlackApiError("The request to the Slack API failed.", {"ok": False, "error": self.error})
            return {"ok": True}

    class Posts:
        posted = []

        def chat_postMessage(self, **kw):
            self.posted.append(kw["text"])
            return {"ok": True}

    sa.names_web, sa.web = Names(), Posts()

    async def go():
        await sa.react("D1", "1.1")
        await sa.unreact("D1", "1.1")
        sa.names_web.error = "already_reacted"
        await sa.react("D1", "1.1")
        for error in ("no_reaction", "message_not_found", "channel_not_found"):
            sa.names_web.error = error
            await sa.unreact("D1", "1.1")
        for error, call in (("ratelimited", sa.react), ("ratelimited", sa.unreact), ("message_not_found", sa.react),
                            ("no_reaction", sa.react), ("already_reacted", sa.unreact)):
            sa.names_web.error = error
            with pytest.raises(SlackApiError):
                await call("D1", "1.1")
        sa.names_web.error, sa.names_web.hang = None, True
        reacting = asyncio.create_task(sa.react("D1", "1.1"))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(sa.reply(None, "Will do.", channel="D1"), 2)
        assert not reacting.done()
        sa.names_web.gate.set()
        await reacting

    asyncio.run(go())
    assert sa.names_web.calls[:2] == [("add", {"channel": "D1", "timestamp": "1.1", "name": "eyes"}),
                                      ("remove", {"channel": "D1", "timestamp": "1.1", "name": "eyes"})]
    assert sa.web.posted == ["Will do."]


def test_the_workspace_is_read_each_time(monkeypatch):
    """Someone who joined the Slack a minute ago can open a public channel
    now, so users.list is read each time it is asked."""
    import wanda.actions.slack as actions

    class Web:
        def __init__(self):
            self.lists, self.people = 0, [{"id": "U1"}]

        def users_list(self, **kw):
            self.lists += 1
            return {"members": list(self.people)}

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(), store=None)
    sa.web = Web()

    async def go():
        before = [u["id"] for u in await sa.workspace()]
        sa.web.people.append({"id": "U3"})
        after = [u["id"] for u in await sa.workspace()]
        return before, after

    before, after = asyncio.run(go())
    assert before == ["U1"] and after == ["U1", "U3"] and sa.web.lists == 2


def test_alerts_carry_the_harness_mark_and_her_answers_and_notes_none(monkeypatch):
    """An alert is posted with the harness's mark, an answer or her note with
    none, and the context a frame is built from is read with the marks, so
    `vault.earlier` can tell them apart."""
    import wanda.actions.slack as actions
    from wanda.main import FAILED
    from wanda.vault import ALERT_EVENT

    class Web:
        def __init__(self):
            self.calls = []

        def chat_postMessage(self, **kw):
            self.calls.append(("post", kw))
            return {"ts": "1.1"}

        def conversations_history(self, **kw):
            self.calls.append(("history", kw))
            return {"messages": []}

        def conversations_replies(self, **kw):
            self.calls.append(("replies", kw))
            return {"messages": []}

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(alert_channel="U0FAN"), store=None)
    sa.web = Web()

    async def go():
        await sa.alert("a vault snapshot failed")
        await sa.fetch_context("D1", None, 0.0)
        await sa.fetch_context("C1", "5.5", 0.0)
        await sa.reply(None, FAILED, channel="D1")
        await sa.reply(None, "an answer", channel="D1")

    asyncio.run(go())
    (_, post), (_, history), (_, replies), (_, note), (_, answer) = sa.web.calls
    assert post["channel"] == "U0FAN" and post["metadata"]["event_type"] == ALERT_EVENT
    assert "metadata" not in note and "metadata" not in answer
    assert history["include_all_metadata"] is True and replies["include_all_metadata"] is True


def test_every_post_in_her_name_is_rendered_harmless(monkeypatch):
    """An answer or a note, an alert, and what a session posts itself: a
    special inside another is rendered once, never into a ping."""
    import wanda.actions.slack as actions
    from wanda import slack_cli

    class Web:
        posted = []

        def chat_postMessage(self, **kw):
            self.posted.append(kw["text"])
            return {"ok": True, "channel": kw["channel"], "ts": "1.1"}

    monkeypatch.setattr(actions, "MIN_INTERVAL_S", 0)
    sa = actions.SlackActions(cfg(alert_channel="U0FAN"), store=None)
    sa.web = Web()
    asyncio.run(sa.reply(None, "<!here|<!here>>", channel="D1"))
    asyncio.run(sa.alert("<!here|<!here>>"))
    monkeypatch.setattr(slack_cli, "_client", lambda cfg, user_token=False: Web())
    assert slack_cli.run(cfg(), SimpleNamespace(verb="post", text="<!here|<!here>>", channel="C9", thread=None,
                                                no_thread=False)) == 0
    assert Web.posted == ["&lt;!here|@here&gt;", "⚠️ &lt;!here|@here&gt;", "&lt;!here|@here&gt;"]


def test_a_deleted_message_is_passed_on(store):
    """So a message still waiting for its conversation's turn can be withdrawn."""
    ev = fire(store, {"type": "message", "subtype": "message_deleted", "channel": "D1",
                      "channel_type": "im", "ts": "200.1", "deleted_ts": "100.1", "hidden": True})
    assert ev.payload == {"kind": "deleted", "channel": "D1", "ts": "100.1"}


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


# --- one classification for the live watcher and a read back from Slack ---

DM_LINE = {"type": "message", "user": "U1", "channel": "D5", "channel_type": "im", "text": "hi"}
# Every case the watcher's tests above take a message through: the tasks it
# finds, the event, and the allowlist. Each is answered by `trigger` as `_take`
# answers it.
TRIGGER_CASES = [
    ((), {"user": "U1", "channel": "C9", "channel_type": "channel", "ts": "100.1", "text": "<@UBOT> hi"}, ()),
    ((), {"user": "U1", "channel": "C9", "channel_type": "channel", "ts": "100.9", "thread_ts": "100.1",
          "text": "<@UBOT> and this?"}, ()),
    ((("C9", "100.1", "mention_guest"),), {"user": "U2", "channel": "C9", "channel_type": "channel", "ts": "100.9",
                                           "thread_ts": "100.1", "text": "yeah agreed"}, ()),
    ((("C9", "100.1", "mention_guest"),), {"user": "U2", "channel": "C9", "channel_type": "channel", "ts": "101.0",
                                           "thread_ts": "100.1", "text": "<@UBOT> thoughts?"}, ()),
    ((), {"user": "U1", "channel": "D5", "channel_type": "im", "ts": "7.7", "text": "<@UBOT> hi"}, ()),
    ((), {"user": "U1", "channel": "D5", "channel_type": "im", "ts": "1.1", "text": "hey"}, ()),
    ((), {"user": "U1", "channel": "D5", "channel_type": "mpim", "ts": "1.1", "text": "hey"}, ()),
    ((), {"user": "U1", "channel": "C9", "channel_type": "channel", "ts": "1.1", "text": "morning all"}, ()),
    ((("C_TRIAGE", "77.1", "email"),), {"user": "U1", "channel": "C_TRIAGE", "channel_type": "channel",
                                        "ts": "77.2", "thread_ts": "77.1", "text": "do it"}, ()),
    ((), {"user": "UBOT", "channel": "D5", "channel_type": "im", "ts": "1.1", "text": "x"}, ()),
    ((), {"bot_id": "B1", "user": "U2", "channel": "D5", "channel_type": "im", "ts": "1.2", "text": "x"}, ()),
    ((), {"user": "U_STRANGER", "channel": "C9", "channel_type": "channel", "ts": "1.1", "text": "<@UBOT> hi"},
     ("U_ME",)),
    ((), {"user": "U_STRANGER", "channel": "C9", "channel_type": "channel", "ts": "1.1", "text": "<@UBOT> hi"}, ()),
    ((("C9", "50.1", "mention"),), {"user": "U_STRANGER", "channel": "C9", "channel_type": "channel", "ts": "60.3",
                                    "thread_ts": "50.1", "text": "hi"}, ("U_ME",)),
    ((("C_ALERTS", "70.1", "mention"),), {"user": "U1", "channel": "C_ALERTS", "channel_type": "group",
                                          "ts": "70.5", "thread_ts": "70.1", "text": "what does this mean?"}, ()),
    ((("C9", "50.1", "mention"),), {"user": "U1", "channel": "C9", "channel_type": "channel", "ts": "90.3",
                                    "thread_ts": "50.1", "text": "and"}, ()),
    ((), {"user": "U1", "channel": "C9", "channel_type": "channel", "ts": "90.2", "thread_ts": "8.8",
          "text": "<@UBOT> hi"}, ()),
    ((), {"user": "U1", "channel": "D5", "channel_type": "im", "ts": "9.1", "text": "with a file",
          "subtype": "file_share", "files": [{"name": "a.pdf"}, {}]}, ()),
    ((), {"user": "U1", "channel": "D5", "channel_type": "im", "ts": "9.2", "text": "joined",
          "subtype": "channel_join"}, ()),
    ((), {"user": "U1", "channel": "C9", "channel_type": "channel", "ts": "9.9", "text": "<@UBOT|wanda> hi"}, ()),
    ((("C_TRIAGE", "77.1", "email"),), {"user": "U1", "channel": "C_TRIAGE", "channel_type": "channel",
                                        "ts": "77.9", "thread_ts": "77.1", "text": "<@UBOT> handle this"}, ()),
    ((("C9", "100.1", "mention"),), {"user": "U1", "channel": "C9", "channel_type": "channel", "ts": "100.5",
                                     "thread_ts": "100.1", "text": "and the other one?"}, ()),
    ((("C_TRIAGE", "77.1", "email"),), {"type": "app_mention", "user": "U1", "channel": "C_TRIAGE", "ts": "77.5",
                                        "thread_ts": "77.1", "text": "<@UBOT> go ahead"}, ()),
]


@pytest.mark.parametrize("tasks, event, owners", TRIGGER_CASES)
def test_trigger_answers_as_the_live_watcher_takes(tmp_path, tasks, event, owners):
    """A message read back from Slack is classified by `trigger`, which the
    live path runs too: what it hands on and whether it is kept are what the
    watcher puts on the queue and keeps, and None is a message it drops.
    Since `_take` calls `trigger`, this holds them together through a
    change; the tests above are what pin how a message is classified."""
    live, read = Store(tmp_path / "live.db"), Store(tmp_path / "read.db")
    for s in (live, read):
        for channel, thread, kind in tasks:
            s.create_task(None, channel, thread, kind=kind)
    event = {"type": "message", **event}
    kw = {"slack_owner_user_ids": list(owners)} if owners else {}
    ev = fire(live, dict(event), **kw)
    w, _, loop = watcher(read, **kw)
    loop.close()
    got = w.trigger(dict(event))
    if ev is None:
        assert got is None
    else:
        payload, memory = got
        assert payload == ev.payload
        assert memory == bool(live.kept(payload["channel"], payload["task_key"]))
    live.close()
    read.close()


def test_a_deletion_records_its_message_as_seen(store):
    """Seen, so that a read back of Slack or Slack sending it again does not
    take it once it is gone; and its withdrawal is queued."""
    w, q, loop = watcher(store)
    w._handle(w.client, FakeReq({"type": "message", "subtype": "message_deleted", "channel": "D5",
                                 "deleted_ts": "5.5"}))
    w._handle(w.client, FakeReq({**DM_LINE, "ts": "5.5"}))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    assert [ev.payload["kind"] for ev in (q.get_nowait() for _ in range(q.qsize()))] == ["deleted"]
    assert store.kept() == []


def test_a_deletion_whose_record_fails_is_still_withdrawn(store, monkeypatch, caplog):
    w, q, loop = watcher(store)

    def full(key, payload=None):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(store, "first_time", full)
    w._handle(w.client, FakeReq({"type": "message", "subtype": "message_deleted", "channel": "D5",
                                 "deleted_ts": "5.5"}))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    assert q.get_nowait().payload == {"kind": "deleted", "channel": "D5", "ts": "5.5"}
    assert "could not record 5.5 in D5 as deleted: database or disk is full" in caplog.text


def test_a_line_the_watcher_could_not_keep_is_owed_a_read_back(store, monkeypatch):
    """The store refusing the write, as on a full disk: Slack is told it
    arrived and sends nothing again, so a read back is owed; the error still
    reaches the SDK's log, and the envelope is acknowledged."""
    w, q, loop = watcher(store)
    acked = []
    w.client = SimpleNamespace(send_socket_mode_response=lambda r: acked.append(r.envelope_id))

    def full(key, payload=None):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(store, "first_time", full)
    with pytest.raises(sqlite3.OperationalError):
        w._handle(w.client, FakeReq({**DM_LINE, "ts": "2.2"}))
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    ev = q.get_nowait()
    assert (ev.dedupe_key, ev.payload) == ("owed:env1", {"kind": "owed"}) and acked == ["env1"]


# --- a new connection to Slack ---

class _Conn:
    """A connection that is open until closed, as the SDK's says."""

    def __init__(self, session_id):
        self.session_id, self.open, self.last_ping_pong_time = session_id, True, None

    def is_active(self):
        return self.open

    def close(self):
        self.open = False

    def check_state(self):
        pass


def connections(monkeypatch, heard):
    """The watcher's client with the SDK's connect stubbed, as a connection
    that opens: no network."""
    from slack_sdk.socket_mode.builtin.client import SocketModeClient
    from slack_sdk.web import WebClient
    from wanda.watchers.slack_watcher import Connections

    made = iter(f"s{i}" for i in range(1, 10))

    def connect(self):
        old = self.current_session
        self.current_session = _Conn(next(made))
        if old:
            old.close()
    monkeypatch.setattr(SocketModeClient, "connect", connect)
    monkeypatch.setattr(SocketModeClient, "issue_new_wss_url", lambda self: "wss://stub.invalid/")
    return Connections(app_token="xapp-stub", web_client=WebClient(token="xoxb-stub"),
                       heard=heard if callable(heard) else lambda session, refresh: heard.append((session, refresh)))


def test_a_new_connection_after_one_closed_or_refreshed_is_heard_and_the_first_is_not(monkeypatch):
    """Every connection goes through `connect`: the start's, which replaces
    none and says nothing; the monitor's after one closed, which replaces
    one that is closed; and Slack's refresh, which opens the new connection
    with the old still open."""
    import json
    import time

    heard = []
    c = connections(monkeypatch, heard)
    try:
        c.connect()
        assert heard == []
        c.current_session.close()
        c.connect_to_new_endpoint()
        assert heard == [("s2", False)]
        c.enqueue_message(json.dumps({"type": "disconnect", "reason": "refresh_requested"}))
        for _ in range(100):
            if len(heard) == 2:
                break
            time.sleep(0.02)
        assert heard == [("s2", False), ("s3", True)]
    finally:
        c.close()


def test_a_new_connection_is_queued_as_heard_saying_whether_it_was_a_refresh(store, monkeypatch):
    """The watcher's own `heard`, from the SDK's thread to the queue the
    Processor reads: a reconnection after a closed connection, then Slack's
    refresh."""
    import json
    import time

    w, q, loop = watcher(store)
    c = connections(monkeypatch, w._heard)
    try:
        c.connect()
        c.current_session.close()
        c.connect_to_new_endpoint()
        c.enqueue_message(json.dumps({"type": "disconnect", "reason": "refresh_requested"}))
        for _ in range(100):
            loop.run_until_complete(asyncio.sleep(0))
            if q.qsize() == 2:
                break
            time.sleep(0.02)
    finally:
        c.close()
        loop.close()
    assert [(ev.source, ev.dedupe_key, ev.payload) for ev in (q.get_nowait() for _ in range(q.qsize()))] == [
        ("slack", "heard:s2", {"kind": "heard", "session": "s2", "refresh": False}),
        ("slack", "heard:s3", {"kind": "heard", "session": "s3", "refresh": True})]


def test_a_try_that_opens_nothing_is_not_heard_and_the_next_that_opens_is(store, monkeypatch):
    """With the SDK's own connect and a socket that cannot be made, nothing
    raises and nothing is heard; the next try that opens is."""
    import socket

    from slack_sdk.socket_mode.builtin import connection as conn_mod
    from slack_sdk.socket_mode.builtin.client import SocketModeClient
    from slack_sdk.web import WebClient
    from wanda.watchers.slack_watcher import Connections

    def refused(**kw):
        raise ConnectionRefusedError("no socket here")
    monkeypatch.setattr(conn_mod, "_establish_new_socket_connection", refused)
    monkeypatch.setattr(SocketModeClient, "issue_new_wss_url", lambda self: "wss://stub.invalid/")
    heard = []
    c = Connections(app_token="xapp-stub", web_client=WebClient(token="xoxb-stub"),
                    heard=lambda session, refresh: heard.append((session, refresh)))
    a, b = socket.socketpair()
    try:
        c.connect()
        c.current_app_monitor.shutdown()  # its own retry would race the one below
        assert heard == [] and not c.is_connected()
        c.connect()
        assert heard == [] and not c.is_connected()
        monkeypatch.setattr(conn_mod.Connection, "connect", lambda self: setattr(self, "sock", a))
        c.connect()
        assert heard == [(c.current_session.session_id, False)]
        # the SDK's own connection, as the watcher reads it, before any pong
        w, _, loop = watcher(store)
        loop.close()
        w.client = c
        assert w.last_heard() == (c.current_session.session_id, None)
    finally:
        c.close()
        a.close()
        b.close()


def test_last_heard_is_the_open_connections_last_pong(store):
    """None with no client, as when a test stubs the start, and with the
    connection closed; else its session and its last pong's time, which a
    pong sets for its own session alone (slack_sdk's Connection)."""
    w, _, loop = watcher(store)
    loop.close()
    w.client = None
    assert w.last_heard() is None
    conn = _Conn("s1")
    w.client = SimpleNamespace(current_session=conn, is_connected=lambda: conn.is_active())
    assert w.last_heard() == ("s1", None)
    conn.last_ping_pong_time = 1791347800.5
    assert w.last_heard() == ("s1", 1791347800.5)
    conn.close()
    assert w.last_heard() is None
