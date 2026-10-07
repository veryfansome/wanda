from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from wanda.config import Config
from wanda.events import Event
from wanda.store import Store
from wanda.transcript import MENTION_RE
from wanda.tls import ssl_context

log = logging.getLogger(__name__)

HUMAN_SUBTYPES = (None, "file_share", "thread_broadcast")
DM_TYPES = ("im", "mpim")
# Task key for a DM's ongoing (unthreaded) conversation.
DM_TASK_KEY = "conversation"


class Connections(SocketModeClient):
    """The SDK's client, which says when a new connection replaces one:
    every connection goes through `connect`, the start's, the monitor's
    after a connection closed or went stale, and a refresh's, which Slack
    asks for and which opens the new connection before closing the old.
    `heard` is told the new connection's session id and whether one was
    still open, which is so only on a refresh; the start's connection, with
    none before it, and a try that opened nothing tell it nothing."""

    def __init__(self, *, heard: Callable[[str, bool], None], **kw):
        super().__init__(**kw)
        self.heard = heard

    def connect(self) -> None:
        replaced, open_before = self.current_session is not None, self.is_connected()
        super().connect()
        if replaced and self.is_connected():
            self.heard(self.current_session.session_id, open_before)


class SlackWatcher:
    """Socket Mode listener. Acks every envelope once what it brings is
    written down (Slack retries past ~3s, and never sends a message again
    once it is acknowledged), passes deletions on (kind `deleted`), so that a
    message still waiting for its turn can be withdrawn, says when a new
    connection replaces one (kind `heard`) or when what an envelope brought
    could not be written down (kind `owed`), so that what she may have missed
    is read back from Slack, and classifies every other message, live or
    read back (`trigger`), into one of four triggers:

      dm            — any message in a DM or group DM; no mention needed
      task          — a message in a thread wanda owns (e.g. an email task, or
                      one of her alerts in a channel)
      mention       — @wanda rooting its own thread in a channel
      mention_guest — @wanda inside a thread wanda does not own; it answers,
                      but later un-mentioned replies there are left alone

    Only `message` events are handled. Slack also sends `app_mention` for the
    same text, but acting on both ran the agent twice, and only the message
    event reliably carries channel_type — so app_mention is acked and dropped,
    and a mention is detected from the message text.
    """

    def __init__(self, cfg: Config, store: Store, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.cfg = cfg
        self.store = store
        self.loop = loop
        self.queue = queue
        self.bot_user_id: str | None = None
        self.bot_id: str | None = None
        self.client: SocketModeClient | None = None
        # (user, conversation) already logged as not allowed: in a group DM
        # every line of someone outside the household would be logged
        self._ignored: set[tuple[str, str]] = set()

    def start(self) -> None:
        """Connects, once auth.test has named her bot user, and her bot id
        if it has one: every frame tells her own posts by them."""
        # SocketModeClient takes its websocket TLS context from this client.
        web = WebClient(token=self.cfg.slack_bot_token, ssl=ssl_context())
        auth = web.auth_test()
        self.bot_user_id, self.bot_id = auth.get("user_id"), auth.get("bot_id")
        if not self.bot_user_id:
            raise RuntimeError("auth.test named no bot user")
        self.client = Connections(app_token=self.cfg.slack_app_token, web_client=web, heard=self._heard)
        self.client.socket_mode_request_listeners.append(self._handle)
        self.client.connect()
        log.info("slack socket mode connected (bot user %s)", self.bot_user_id)

    def stop(self) -> None:
        if self.client:
            self.client.close()

    def _heard(self, session: str, refresh: bool) -> None:
        # from the SDK's thread, as a message is handed on: what arrives on a
        # refreshed connection queues behind it, and after a reconnection in
        # practice too, a message taking several thread hops; one that came
        # first would only be answered first
        self.loop.call_soon_threadsafe(self.queue.put_nowait, Event(
            source="slack", dedupe_key=f"heard:{session}",
            payload={"kind": "heard", "session": session, "refresh": refresh}))

    def last_heard(self) -> tuple[str, float | None] | None:
        """The open connection's session id and the time its last pong
        carried, None before the first: frames on one socket arrive in
        order, so every event Slack sent before that pong has reached her.
        None with no connection open."""
        if self.client is None or not self.client.is_connected():
            return None
        session = self.client.current_session
        return session.session_id, session.last_ping_pong_time

    def _allowed(self, user: str) -> bool:
        """Whether `user` may start a session. An empty list would let anyone
        in, and the daemon refuses to start with one."""
        return not self.cfg.slack_owner_user_ids or user in self.cfg.slack_owner_user_ids

    def _handle(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        try:
            self._take(req)
        except Exception:
            # what it brought may be lost, as when the store takes no write
            # on a full disk. The envelope is acknowledged all the same
            # (below), so Slack does not send it again, and what it brought
            # is read back from Slack once the store takes a write. Raised
            # on, for the SDK's log
            self.loop.call_soon_threadsafe(self.queue.put_nowait, Event(
                source="slack", dedupe_key=f"owed:{req.envelope_id}", payload={"kind": "owed"}))
            raise
        finally:
            # after a member's message is kept (Store.first_time): one
            # acknowledged first and lost to a stop or a crash before it was
            # written would be lost for good. Acknowledged too when the write
            # raises, as on a full disk, which Slack's sending it again would
            # meet the same way
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    def _take(self, req: SocketModeRequest) -> None:
        if req.type != "events_api":
            return
        event = req.payload.get("event", {})
        # app_mention is ignored on purpose: Slack sends it *alongside* a
        # message.* event for the same text, and only the message event
        # reliably carries channel_type. Working from one event type removes
        # the twin entirely, rather than trying to reconcile two.
        if event.get("type") != "message":
            return
        if event.get("subtype") == "message_deleted" and event.get("deleted_ts"):
            # seen, so that a read back of Slack, or Slack sending it again,
            # does not take it as new once it is gone
            try:
                self.store.first_time(f"{event.get('channel')}:{event['deleted_ts']}")
            except Exception as e:
                log.warning("could not record %s in %s as deleted: %s", event["deleted_ts"], event.get("channel"), e)
            # a deleted message still waiting for its conversation's turn is
            # withdrawn from it
            self.loop.call_soon_threadsafe(self.queue.put_nowait, Event(
                source="slack", dedupe_key=f"{event.get('channel')}:{event['deleted_ts']}:deleted",
                payload={"kind": "deleted", "channel": event.get("channel"), "ts": event["deleted_ts"]}))
            return
        taken = self.trigger(event)
        if taken is None:
            return
        payload, memory = taken
        channel, ts = payload["channel"], payload["ts"]
        # Keyed on the MESSAGE, not the envelope: one @-mention in a thread
        # arrives as both app_mention and message.*, with different event_ids,
        # and would otherwise run the agent twice. Same key also absorbs
        # Slack's redeliveries.
        if not self.store.first_time(f"{channel}:{ts}", payload if memory else None):
            return
        self.loop.call_soon_threadsafe(self.queue.put_nowait, Event(source="slack", dedupe_key=f"{channel}:{ts}",
                                                                    payload=payload))

    def trigger(self, event: dict) -> tuple[dict, bool] | None:
        """What a message event starts, as the payload handed on, and whether
        it is kept until it is answered; None for one that starts nothing.
        Both a live event and a message read back from Slack come through
        here, so that each is taken by the same rules."""
        if event.get("type") != "message":
            return None
        if event.get("bot_id") or event.get("subtype") not in HUMAN_SUBTYPES:
            return None
        user = event.get("user")
        if not user or user == self.bot_user_id:
            return None

        channel = event.get("channel")
        channel_type = event.get("channel_type")
        thread_ts = event.get("thread_ts")
        ts = event.get("ts")

        # Parsed, not substring-matched, so the labelled form <@U123|name>
        # counts too — transcript.render already treats it as a mention.
        mentioned = bool(self.bot_user_id) and self.bot_user_id in MENTION_RE.findall(
            event.get("text") or ""
        )
        existing = self.store.get_task_by_thread(channel, thread_ts) if thread_ts else None
        if channel_type in DM_TYPES:
            kind = "dm"  # a DM needs no mention
        elif existing and existing["kind"] != "mention_guest":
            # A thread wanda owns: follow-ups count whether or not they mention
            # it, and a mention here must not open a competing guest task.
            kind = "task"
        elif mentioned:
            # A mention rooting its own thread makes that thread wanda's; a
            # mention inside someone else's thread does not.
            kind = "mention" if not thread_ts else "mention_guest"
        else:
            # Ordinary chatter, including plain replies in a guest thread —
            # otherwise one @wanda would capture a human conversation forever.
            return None

        if not self._allowed(user):
            if (user, channel) not in self._ignored:
                self._ignored.add((user, channel))
                log.warning("ignoring %s from non-allowed user %s in %s", kind, user, channel)
            return None

        if kind == "dm" and not thread_ts:  # noqa: SIM108 — kept explicit
            # A DM is one conversation: every top-level message maps to one
            # task, so its turns run one at a time, each a fresh session.
            # Replies go unthreaded, which keeps them in conversations.history,
            # where the next turn's conversation so far is read.
            task_key, reply_thread = DM_TASK_KEY, None
        else:
            task_key = reply_thread = thread_ts or ts
        payload = {
            "kind": kind,
            "channel": channel,
            "channel_type": channel_type,
            "task_key": task_key,          # identifies the task and session
            "reply_thread": reply_thread,  # where answers get posted
            "in_thread": bool(thread_ts),
            "user": user,
            "text": event.get("text", ""),
            "files": [f.get("name") or "file" for f in event.get("files") or []],
            "ts": ts,
        }
        # A message to her is kept until it is answered, but for a reply in
        # an email task's thread, whose path leaves its own marker at a stop
        # (Processor.shutdown)
        return payload, kind != "task" or existing["kind"] != "email"
