from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry import default_retry_handlers
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from wanda.config import Config
from wanda.store import Store
from wanda.tls import ssl_context
from wanda.transcript import trim_thread
from wanda.triage import Verdict

log = logging.getLogger(__name__)

METADATA_EVENT_TYPE = "wanda_task"
URGENCY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
MIN_INTERVAL_S = 1.0  # chat.postMessage is ~1/s/channel
SNIPPET_LIMIT = 1500
TEXT_LIMIT = 3500  # well under Slack's 40k text cap, and headers can be huge
MISSING_THREAD_ERRORS = {"thread_not_found", "message_not_found", "channel_not_found"}
MAX_CONTEXT_PAGES = 10  # bounds a very long thread at ~2000 messages


def truncate_text(text: str) -> str:
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + "… (truncated)"


class DigestScanFailed(Exception):
    """The recovery history scan itself failed — 'not found' is not proven."""


def esc(text: str | None) -> str:
    """Neutralize Slack mrkdwn control sequences in untrusted text. Without
    this an email Subject can fire <!channel> or render a disguised link."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_inline(text: str | None) -> str:
    """esc() plus newline folding, for untrusted values placed mid-sentence
    (a Subject header can legally carry embedded newlines)."""
    return " ".join(esc(text).split())


class SlackActions:
    """Task-thread + digest lifecycle. Sync WebClient calls hop through
    asyncio.to_thread behind a pacing lock."""

    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        # The SDK default only retries connection errors; 429s raise immediately.
        self.web = WebClient(
            token=cfg.slack_bot_token,
            ssl=ssl_context(),
            retry_handlers=default_retry_handlers() + [RateLimitErrorRetryHandler(max_retry_count=3)],
        )
        self._pace = asyncio.Lock()
        self._last_call = 0.0
        self._user_cache: dict[str, str] = {}

    async def _call(self, method: str, /, **kwargs):
        async with self._pace:
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await asyncio.to_thread(getattr(self.web, method), **kwargs)
            finally:
                self._last_call = time.monotonic()

    # --- task threads ---

    async def post_task(self, row: sqlite3.Row, verdict: Verdict) -> str:
        emoji = URGENCY_EMOJI.get(verdict.urgency, "🟡")
        snippet = esc((row["snippet"] or "")[:SNIPPET_LIMIT]).replace("```", "'''")
        text = (
            f"{emoji} *{esc(verdict.summary)}*\n"
            f"From: {esc_inline(row['from_addr'])}\nSubject: {esc_inline(row['subject'])}\n"
            f"_{esc(verdict.reason)}_\n"
            f"```{snippet}```\n"
            f"Reply in this thread to have wanda work on it."
        )
        resp = await self._call(
            "chat_postMessage",
            channel=self.cfg.slack_channel_id,
            text=truncate_text(text),
            metadata={
                "event_type": METADATA_EVENT_TYPE,
                "event_payload": {"dedupe_key": row["dedupe_key"]},
            },
        )
        return resp["ts"]

    async def find_task_post(self, dedupe_key: str) -> str | None:
        """Recovery-time scan: was this task already posted before a crash?"""
        try:
            resp = await self._call(
                "conversations_history",
                channel=self.cfg.slack_channel_id,
                limit=100,
                include_all_metadata=True,
            )
        except Exception as e:
            # Never silently downgrade a failed scan to "not posted" — that
            # duplicates the task post and orphans the original thread.
            raise DigestScanFailed(str(e)) from e
        for m in resp.get("messages", []):
            meta = m.get("metadata") or {}
            if (
                meta.get("event_type") == METADATA_EVENT_TYPE
                and (meta.get("event_payload") or {}).get("dedupe_key") == dedupe_key
            ):
                return m["ts"]
        return None

    async def reply(self, thread_ts: str, text: str, channel: str | None = None) -> None:
        """channel defaults to the triage channel; mention and DM sessions pass
        the conversation they came from."""
        await self._call(
            "chat_postMessage",
            channel=channel or self.cfg.slack_channel_id,
            thread_ts=thread_ts,
            text=text[:39000],
        )

    # --- conversation context ---

    async def fetch_context(self, channel: str, thread_ts: str | None, limit: int) -> list[dict]:
        """The most RECENT messages, oldest first. A thread reads its replies;
        a channel or DM reads its history."""
        if not thread_ts:
            resp = await self._call("conversations_history", channel=channel, limit=limit)
            return list(reversed(resp.get("messages") or []))  # history is newest first
        # conversations.replies pages FORWARD from the parent, so a bare limit
        # returns the start of a long thread and drops what was just said.
        msgs: list[dict] = []
        cursor = None
        for _ in range(MAX_CONTEXT_PAGES):
            kwargs = {"channel": channel, "ts": thread_ts, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await self._call("conversations_replies", **kwargs)
            msgs.extend(resp.get("messages") or [])
            cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not resp.get("has_more") or not cursor:
                break
        return trim_thread(msgs, limit)  # keeps the parent plus the newest

    async def user_names(self, user_ids: set[str]) -> dict[str, str]:
        """Resolve ids to display names, cached for the process lifetime."""
        for uid in user_ids - self._user_cache.keys():
            try:
                resp = await self._call("users_info", user=uid)
                u = resp.get("user") or {}
                prof = u.get("profile") or {}
                self._user_cache[uid] = (
                    prof.get("display_name") or prof.get("real_name") or u.get("name") or uid
                )
            except Exception:
                self._user_cache[uid] = uid  # deleted user, or missing users:read
        return self._user_cache

    async def alert(self, text: str) -> None:
        await self._call(
            "chat_postMessage", channel=self.cfg.slack_channel_id,
            text=truncate_text(f"⚠️ wanda: {text}"),
        )

    # --- daily digest ---

    async def _digest_thread(self, local_date: str) -> str:
        digest = self.store.get_digest(local_date)
        if digest is not None:
            return digest["thread_ts"]
        resp = await self._call(
            "chat_postMessage",
            channel=self.cfg.slack_channel_id,
            text=f"🧹 Triage digest — {local_date}",
        )
        self.store.set_digest(local_date, self.cfg.slack_channel_id, resp["ts"])
        return resp["ts"]

    async def digest_entry(self, row: sqlite3.Row, verdict: Verdict, applied_action: str, note: str) -> None:
        local_date = datetime.now().astimezone().strftime("%Y-%m-%d")
        thread_ts = await self._digest_thread(local_date)
        label = {
            "trash": "🗑 Trashed",
            "shadow_trash": "🗑? WOULD trash",
            "ignore": "· Ignored",
        }.get(applied_action, applied_action)
        line = (
            f"{label}: {esc_inline(row['from_addr'])} — “{esc_inline(row['subject'])}” — "
            f"{esc_inline(verdict.reason)} (conf {verdict.confidence:.2f})"
        )
        if note:
            line += f" [{esc(note)}]"
        if applied_action in ("trash", "shadow_trash") and row["message_id"]:
            mid = esc(row["message_id"]).replace("`", "'")  # keep the code span closed
            line += f"\nMessage-ID: `{mid}`"
        line = truncate_text(line)
        try:
            await self._call(
                "chat_postMessage", channel=self.cfg.slack_channel_id,
                thread_ts=thread_ts, text=line,
            )
        except SlackApiError as e:
            # Only start a fresh parent when this one is genuinely gone. Any
            # other error must propagate to the caller's retry, or a rate limit
            # would churn out a new digest parent on every attempt.
            if (e.response or {}).get("error") not in MISSING_THREAD_ERRORS:
                raise
            log.warning("digest parent %s is gone; starting a fresh one", thread_ts)
            self.store.clear_digest(local_date)
            fresh = await self._digest_thread(local_date)
            await self._call(
                "chat_postMessage", channel=self.cfg.slack_channel_id,
                thread_ts=fresh, text=line,
            )
