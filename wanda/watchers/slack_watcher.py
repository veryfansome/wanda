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

log = logging.getLogger(__name__)

HUMAN_SUBTYPES = (None, "file_share", "thread_broadcast")


class SlackWatcher:
    """Socket Mode listener. Acks every envelope immediately (Slack retries
    past ~3s), then filters and enqueues. Filter order is a security boundary:
    thread replies drive an agent with tools on this Mac, so only configured
    owner user IDs get through."""

    def __init__(self, cfg: Config, store: Store, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.cfg = cfg
        self.store = store
        self.loop = loop
        self.queue = queue
        self.bot_user_id: str | None = None
        self.client: SocketModeClient | None = None

    def start(self) -> None:
        web = WebClient(token=self.cfg.slack_bot_token)
        self.bot_user_id = web.auth_test()["user_id"]
        self.client = SocketModeClient(app_token=self.cfg.slack_app_token, web_client=web)
        self.client.socket_mode_request_listeners.append(self._handle)
        self.client.connect()
        log.info("slack socket mode connected (bot user %s)", self.bot_user_id)

    def stop(self) -> None:
        if self.client:
            self.client.close()

    def _handle(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = req.payload.get("event", {})
        if event.get("type") != "message":
            return
        # Allowlist, not a blanket drop: a thread reply carrying a file or sent
        # with "Also send to channel" is a real owner command, just subtyped.
        if event.get("bot_id") or event.get("subtype") not in HUMAN_SUBTYPES:
            return
        user = event.get("user")
        if not user or user == self.bot_user_id:
            return
        if user not in self.cfg.slack_owner_user_ids:
            log.warning("dropping message from non-owner %s", user)
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return  # only task-thread replies are commands
        if self.cfg.slack_channel_id and event.get("channel") != self.cfg.slack_channel_id:
            return
        event_id = req.payload.get("event_id") or f"{event.get('channel')}:{event.get('ts')}"
        if not self.store.slack_event_first_time(event_id):
            return  # Slack redelivery
        ev = Event(
            source="slack",
            dedupe_key=event_id,
            payload={
                "channel": event.get("channel"),
                "thread_ts": thread_ts,
                "user": user,
                "text": event.get("text", ""),
                "ts": event.get("ts"),
            },
        )
        self.loop.call_soon_threadsafe(self.queue.put_nowait, ev)
