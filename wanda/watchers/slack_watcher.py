from __future__ import annotations

import asyncio
import logging

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from wanda.config import Config
from wanda.events import Event
from wanda.store import Store
from wanda.tls import ssl_context

log = logging.getLogger(__name__)

HUMAN_SUBTYPES = (None, "file_share", "thread_broadcast")
DM_TYPES = ("im", "mpim")
# Task key for a DM's ongoing (unthreaded) conversation.
DM_TASK_KEY = "conversation"


class SlackWatcher:
    """Socket Mode listener. Acks every envelope immediately (Slack retries
    past ~3s), then classifies it into one of three triggers:

      mention — @wanda in a channel, at top level or inside a thread
      dm      — any message in a DM or group DM
      task    — a reply in a thread wanda already owns (e.g. an email task)

    Channel mentions arrive twice (as app_mention and as message.channels), so
    only app_mention is taken for channels and plain messages are used for DMs.
    """

    def __init__(self, cfg: Config, store: Store, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.cfg = cfg
        self.store = store
        self.loop = loop
        self.queue = queue
        self.bot_user_id: str | None = None
        self.client: SocketModeClient | None = None

    def start(self) -> None:
        # SocketModeClient takes its websocket TLS context from this client.
        web = WebClient(token=self.cfg.slack_bot_token, ssl=ssl_context())
        self.bot_user_id = web.auth_test()["user_id"]
        self.client = SocketModeClient(app_token=self.cfg.slack_app_token, web_client=web)
        self.client.socket_mode_request_listeners.append(self._handle)
        self.client.connect()
        log.info("slack socket mode connected (bot user %s)", self.bot_user_id)

    def stop(self) -> None:
        if self.client:
            self.client.close()

    def _allowed(self, user: str) -> bool:
        """An empty owner list means anyone in the workspace may talk to wanda."""
        return not self.cfg.slack_owner_user_ids or user in self.cfg.slack_owner_user_ids

    def _handle(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = req.payload.get("event", {})
        # app_mention is ignored on purpose: Slack sends it *alongside* a
        # message.* event for the same text, and only the message event
        # reliably carries channel_type. Working from one event type removes
        # the twin entirely, rather than trying to reconcile two.
        if event.get("type") != "message":
            return
        if event.get("bot_id") or event.get("subtype") not in HUMAN_SUBTYPES:
            return
        user = event.get("user")
        if not user or user == self.bot_user_id:
            return

        channel = event.get("channel")
        channel_type = event.get("channel_type")
        thread_ts = event.get("thread_ts")
        ts = event.get("ts")

        mentioned = bool(self.bot_user_id) and f"<@{self.bot_user_id}>" in (event.get("text") or "")
        if channel_type in DM_TYPES:
            kind = "dm"  # a DM needs no mention
        elif mentioned:
            # A mention rooting its own thread makes that thread wanda's; a
            # mention inside someone else's thread does not.
            kind = "mention" if not thread_ts else "mention_guest"
        elif thread_ts and (task := self.store.get_task_by_thread(channel, thread_ts)):
            # Follow-ups without a mention are only for threads wanda owns —
            # otherwise one @wanda would make it answer a human conversation
            # forever.
            if task["kind"] == "mention_guest":
                return
            kind = "task"
        else:
            return  # ordinary channel chatter wanda was not addressed in

        if not self._allowed(user):
            log.warning("ignoring %s from non-allowed user %s", kind, user)
            return

        # Keyed on the MESSAGE, not the envelope: one @-mention in a thread
        # arrives as both app_mention and message.*, with different event_ids,
        # and would otherwise run the agent twice. Same key also absorbs
        # Slack's redeliveries.
        if not self.store.slack_event_first_time(f"{channel}:{ts}"):
            return
        if kind == "dm" and not thread_ts:  # noqa: SIM108 — kept explicit
            # A DM is one ongoing conversation: every top-level message maps to
            # the same task and resumes the same session. Replies go untreaded,
            # which also keeps them visible in conversations.history — the very
            # context the next message is seeded with.
            task_key, reply_thread = DM_TASK_KEY, None
        else:
            task_key = reply_thread = thread_ts or ts
        ev = Event(
            source="slack",
            dedupe_key=f"{channel}:{ts}",
            payload={
                "kind": kind,
                "channel": channel,
                "channel_type": channel_type,
                "task_key": task_key,          # identifies the task and session
                "reply_thread": reply_thread,  # where answers get posted
                "in_thread": bool(thread_ts),
                "user": user,
                "text": event.get("text", ""),
                "ts": ts,
            },
        )
        self.loop.call_soon_threadsafe(self.queue.put_nowait, ev)
