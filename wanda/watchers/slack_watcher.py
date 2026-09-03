from __future__ import annotations

import asyncio
import logging

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from wanda.config import Config
from wanda.events import Event
from wanda.memory.commands import is_command
from wanda.store import Store
from wanda.transcript import MENTION_RE
from wanda.tls import ssl_context

log = logging.getLogger(__name__)

HUMAN_SUBTYPES = (None, "file_share", "thread_broadcast")
DM_TYPES = ("im", "mpim")


class SlackWatcher:
    """Socket Mode listener. Acks every envelope immediately (Slack retries
    past ~3s), then classifies it into one of five triggers:

      command       — an owner's `rule|attest|forget|pin|unretire …`, or any
                      owner message in a digest thread; handled in-process,
                      never opens a session
      dm            — any message in a DM or group DM; no mention needed.
                      A DM behaves like a private channel: every top-level
                      message roots its own thread and task
      task          — a message in a thread wanda owns (e.g. an email task)
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
        """An empty allow-list means anyone in the workspace may talk to wanda."""
        return not self.cfg.slack_allowed_user_ids or user in self.cfg.slack_allowed_user_ids

    def _is_owner(self, user: str) -> bool:
        return user in self.cfg.memory_owner_user_ids

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

        # Parsed, not substring-matched, so the labelled form <@U123|name>
        # counts too — transcript.render already treats it as a mention.
        mentioned = bool(self.bot_user_id) and self.bot_user_id in MENTION_RE.findall(
            event.get("text") or ""
        )
        existing = self.store.get_task_by_thread(channel, thread_ts) if thread_ts else None
        text = event.get("text") or ""
        in_digest = bool(thread_ts) and self.store.get_digest_by_thread(channel, thread_ts) is not None
        if self._is_owner(user) and (is_command(text) or in_digest):
            # Checked first: an owner typing `rule …` in a DM must not open a
            # paid session, and a reply in the digest thread has no mention.
            kind = "command"
        elif in_digest:
            return  # someone else chatting in the digest thread
        elif channel_type in DM_TYPES:
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
            return

        if not self._allowed(user):
            log.warning("ignoring %s from non-allowed user %s", kind, user)
            return

        # Keyed on the MESSAGE, not the envelope: one @-mention in a thread
        # arrives as both app_mention and message.*, with different event_ids,
        # and would otherwise run the agent twice. Same key also absorbs
        # Slack's redeliveries.
        if not self.store.slack_event_first_time(f"{channel}:{ts}"):
            return
        # Every kind, DMs included: the thread this message is in, or the
        # thread it roots. wanda always answers in a thread.
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
                "thread_ts": thread_ts,
                "user": user,
                "text": text,
                "ts": ts,
            },
        )
        self.loop.call_soon_threadsafe(self.queue.put_nowait, ev)
