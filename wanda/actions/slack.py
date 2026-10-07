from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry import default_retry_handlers
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from wanda.config import Config
from wanda.household import SHUT
from wanda.store import Store
from wanda.tls import ssl_context
from wanda.transcript import harmless
from wanda.triage import Verdict
from wanda.vault import ALERT_EVENT, EARLIER

log = logging.getLogger(__name__)

METADATA_EVENT_TYPE = "wanda_task"
URGENCY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
MIN_INTERVAL_S = 1.0  # chat.postMessage is ~1/s/channel
SNIPPET_LIMIT = 1500
TEXT_LIMIT = 3500  # well under Slack's 40k text cap, and headers can be huge
MISSING_THREAD_ERRORS = {"thread_not_found", "message_not_found", "channel_not_found"}
# Her reaction on a member's message from when she takes it until her answer
# or note to it is posted, or her session ends saying nothing: it says she has
# it, where she is otherwise silent until she has something to say.
WORKING = "eyes"
# what Slack answers a removal with when there is nothing to take off: no
# reaction there, or no such message or conversation
NOT_THERE = {"no_reaction", "message_not_found", "channel_not_found"}
# the most pages of 200 any one list is read in: a conversation's history or
# a thread, a member list, the workspace
MAX_CONTEXT_PAGES = 10
# One call of a read back from Slack, or one late add of her reaction, every
# this long: about 40 a minute, under the 50 a minute Slack allows an internal
# app for each method (Tier 3), which frames' reads of history and threads
# share.
READ_EVERY_S = 1.5


class CallFailed(Exception):
    """Any failure of a read back's call but Slack's own answer, which stays
    SlackApiError: the network, its timeout, the client. Wrapped at the call
    because not all the client raises is an OSError (a reply cut short,
    `http.client.IncompleteRead`, for one): unwrapped, such a failure would
    read as a fault in handling what Slack gave, which passes one
    conversation over, where a call that failed ends the read."""


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
        # The household's names are read, and her reaction put on and taken
        # off, on a client of their own, outside the pacing lock: a Slack that
        # hangs on one holds up no post. Ten seconds and no retry, where the
        # SDK waits thirty and retries a connection error: the next round or
        # pass tries again. Slack limits each method on its own, so these
        # take nothing from chat.postMessage's.
        self.names_web = WebClient(token=cfg.slack_bot_token, ssl=ssl_context(), timeout=10, retry_handlers=[])
        self._pace = asyncio.Lock()
        self._last_call = 0.0
        # a read back's calls, paced on their own
        self._read_pace = asyncio.Lock()
        self._last_read = 0.0
        self.read_calls = 0
        self._users: dict[str, dict] = {}
        self._own_ids: frozenset[str] = frozenset()
        self._dms: dict[str, str] = {}

    async def _call(self, method: str, /, **kwargs):
        async with self._pace:
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await asyncio.to_thread(getattr(self.web, method), **kwargs)
            finally:
                self._last_call = time.monotonic()

    async def _read(self, method: str, /, **kwargs):
        """A call of a read back from Slack, on the names' client, outside
        the posts' pacing, one every READ_EVERY_S. Whatever fails in it but
        Slack's answer is raised as CallFailed, so that the read can tell a
        call that failed from a fault in handling what the call gave."""
        async with self._read_pace:
            wait = READ_EVERY_S - (time.monotonic() - self._last_read)
            if wait > 0:
                await asyncio.sleep(wait)
            self.read_calls += 1
            try:
                return await asyncio.to_thread(getattr(self.names_web, method), **kwargs)
            except SlackApiError:
                raise
            except Exception as e:
                raise CallFailed(f"{method}: {str(e) or type(e).__name__}") from e
            finally:
                self._last_read = time.monotonic()

    # --- task threads ---

    async def post_task(self, row: sqlite3.Row, verdict: Verdict) -> str:
        emoji = URGENCY_EMOJI.get(verdict.urgency, "🟡")
        snippet = esc((row["snippet"] or "")[:SNIPPET_LIMIT]).replace("```", "'''")
        text = (
            f"{emoji} *{esc(verdict.summary)}*\n"
            f"From: {esc_inline(row['from_addr'])}\nSubject: {esc_inline(row['subject'])}\n"
            f"_{esc(verdict.reason)}_\n"
            f"```{snippet}```\n"
            f"Reply in this thread to have me work on it."
        )
        resp = await self._call(
            "chat_postMessage",
            channel=self.cfg.email_triage_slack_channel_id,
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
                channel=self.cfg.email_triage_slack_channel_id,
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

    async def reply(self, thread_ts: str | None, text: str, *, channel: str) -> None:
        """Both arguments are required and neither defaults. A default channel
        would silently publish a DM answer in the triage channel the one time a
        caller forgot it — which is exactly what happened before. The text is
        rendered harmless first, and carries no mark: an answer and her note
        are both hers."""
        await self._call(
            "chat_postMessage",
            channel=channel,
            thread_ts=thread_ts,
            text=harmless(text)[:39000],
        )

    # --- her reaction ---

    async def react(self, channel: str, ts: str) -> None:
        """Puts WORKING on a message, on the names' client; Slack saying it
        is there already is success."""
        try:
            await asyncio.to_thread(self.names_web.reactions_add, channel=channel, timestamp=ts, name=WORKING)
        except SlackApiError as e:
            if (e.response or {}).get("error") != "already_reacted":
                raise

    async def unreact(self, channel: str, ts: str) -> None:
        """Takes WORKING off a message, on the names' client; Slack saying
        there is nothing to take off is success."""
        try:
            await asyncio.to_thread(self.names_web.reactions_remove, channel=channel, timestamp=ts, name=WORKING)
        except SlackApiError as e:
            if (e.response or {}).get("error") not in NOT_THERE:
                raise

    # --- conversation context ---

    async def fetch_context(self, channel: str, thread_ts: str | None, since: float,
                            counted: Callable[[dict], bool] | None = None) -> list[dict]:
        """What a frame is built from, oldest first, with the metadata the
        harness marks its posts with. Outside a thread, the conversation's
        history back to `since`, read until EARLIER of the messages read pass
        `counted`: history comes newest first, so every line a frame can show
        has been read by then. A thread whole; when it runs past
        MAX_CONTEXT_PAGES, it is read again from `since`, so that its newest
        replies are read too, and both reads are kept, leaving unread only the
        replies between the first read's last and `since`. When more than
        MAX_CONTEXT_PAGES pages fall after `since`, the rest goes unread: the
        oldest outside a thread, the newest in one."""
        if not thread_ts:
            msgs: list[dict] = []
            n, pages = 0, 0
            async with contextlib.aclosing(self._history(channel, since, self._call)) as history:
                async for page, _ in history:
                    msgs.extend(page)
                    n += sum(1 for m in page if counted(m)) if counted else 0
                    pages += 1
                    if pages >= MAX_CONTEXT_PAGES or (counted and n >= EARLIER):
                        break
            return list(reversed(msgs))
        msgs, more = await self._replies(channel, thread_ts, None)
        if not more:
            return msgs
        # conversations.replies gives a thread's earliest replies first; from
        # `since` on, the read reaches its newest. Both reads are kept: the
        # first holds the thread's first message and its earliest replies, the
        # household's among them. Whether the second gives that first message
        # again is not documented, so each message is kept once.
        again, _ = await self._replies(channel, thread_ts, since)
        seen = {m.get("ts") for m in msgs}
        return msgs + [m for m in again if m.get("ts") not in seen]

    async def _history(self, channel: str, oldest: float,
                       call: Callable[..., Awaitable]) -> AsyncIterator[tuple[list[dict], bool]]:
        """A conversation's history back to `oldest`, a page at a time, newest
        first, each with whether more follows, through `call`."""
        cursor = None
        while True:
            kwargs = {"channel": channel, "oldest": f"{oldest:.6f}", "limit": 200, "include_all_metadata": True}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await call("conversations_history", **kwargs)
            cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
            yield resp.get("messages") or [], bool(cursor)
            if not cursor:
                return

    async def _replies(self, channel: str, thread_ts: str, since: float | None,
                       call: Callable[..., Awaitable] | None = None) -> tuple[list[dict], bool]:
        """A thread's replies, earliest first, after `since` if given, and
        whether MAX_CONTEXT_PAGES ran out with more to read, through `call`,
        the posts' pacing unless given."""
        call = call or self._call
        msgs: list[dict] = []
        cursor = None
        for _ in range(MAX_CONTEXT_PAGES):
            kwargs = {"channel": channel, "ts": thread_ts, "limit": 200, "include_all_metadata": True}
            if since is not None:
                kwargs["oldest"] = f"{since:.6f}"
            if cursor:
                kwargs["cursor"] = cursor
            resp = await call("conversations_replies", **kwargs)
            msgs.extend(resp.get("messages") or [])
            cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not resp.get("has_more") or not cursor:
                return msgs, False
        return msgs, True

    # --- reading back what was sent while she could not hear Slack ---

    async def conversations(self) -> list[dict]:
        """Every conversation she is in that is not archived, as Slack lists
        them (users.conversations), one opened while she could not hear
        Slack included."""
        found: list[dict] = []
        cursor = None
        for _ in range(MAX_CONTEXT_PAGES):
            kwargs = {"types": "public_channel,private_channel,mpim,im", "exclude_archived": True, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await self._read("users_conversations", **kwargs)
            found.extend(resp.get("channels") or [])
            cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                return found
        # a list cut short would leave conversations unread
        raise RuntimeError(f"she is in more conversations than one read lists ({MAX_CONTEXT_PAGES} pages of 200)")

    def read_history(self, channel: str, oldest: float) -> AsyncIterator[tuple[list[dict], bool]]:
        """A conversation's history back to `oldest`, as a read back reads
        it, a page at a time, newest first."""
        return self._history(channel, oldest, self._read)

    async def read_thread(self, channel: str, thread_ts: str, oldest: float) -> list[dict]:
        """A thread's replies after `oldest`, earliest first, as a read back
        reads them; Slack gives its first message too, whatever its age."""
        msgs, _ = await self._replies(channel, thread_ts, oldest, self._read)
        return msgs

    def know_own_ids(self, ids: frozenset[str]) -> None:
        """Her bot user id and bot id, from the watcher's auth.test at
        start-up, which comes before any frame is built."""
        self._own_ids = ids

    async def own_ids(self) -> frozenset[str]:
        """The bot user id and bot id wanda posts under, as start-up handed
        them over."""
        return self._own_ids

    def kept(self, user_ids) -> dict[str, dict]:
        """What `users` holds for these ids, with no call."""
        return {uid: self._users[uid] for uid in user_ids if uid in self._users}

    async def users(self, user_ids: set[str]) -> dict[str, dict]:
        """users.info for each id not held, kept for the process lifetime: an
        empty record when Slack answers that it shows no one, which stands. Any
        other failure is not kept, so a passing failure costs one message its
        names rather than leaving someone unnamed until a restart."""
        for uid in user_ids - self._users.keys():
            try:
                self._users[uid] = (await self._call("users_info", user=uid)).get("user") or {}
            except SlackApiError as e:
                if (e.response or {}).get("error") in SHUT:
                    self._users[uid] = {}
                else:
                    log.warning("could not look up Slack user %s: %s", uid, e)
            except Exception:
                log.warning("could not look up Slack user %s", uid)
        return self.kept(user_ids)

    async def user_now(self, user_id: str) -> dict:
        """users.info for one id, read now, whatever is kept, and kept in its
        place. Raises when the read fails, leaving what was kept."""
        user = (await asyncio.to_thread(self.names_web.users_info, user=user_id)).get("user") or {}
        self._users[user_id] = user
        return user

    async def user_names(self, user_ids: set[str]) -> dict[str, str]:
        """Resolve ids to display names, cached for the process lifetime."""
        names = {}
        for uid, u in (await self.users(user_ids)).items():
            prof = u.get("profile") or {}
            names[uid] = prof.get("display_name") or prof.get("real_name") or u.get("name") or uid
        return names

    async def members(self, channel: str) -> list[str]:
        """Who can read what is posted in a conversation. A channel's members
        read its threads too, whether or not they have said anything there."""
        ids: list[str] = []
        cursor = None
        for _ in range(MAX_CONTEXT_PAGES):
            kwargs = {"channel": channel, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await self._call("conversations_members", **kwargs)
            ids.extend(resp.get("members") or [])
            cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                return ids
        # a list cut short would leave readers out of a frame
        raise RuntimeError(f"{channel} has more members than {MAX_CONTEXT_PAGES} pages of them")

    async def workspace(self) -> list[dict]:
        """Everyone in this Slack (users.list): who can open a public channel
        without joining it. Read each time it is asked, so that someone who
        joined the Slack a minute ago counts: a turn in a public channel is
        rare, and the household's Slack is a page of a few accounts. Each
        record is kept as `users` keeps one."""
        people: list[dict] = []
        cursor = None
        for _ in range(MAX_CONTEXT_PAGES):
            kwargs = {"limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await self._call("users_list", **kwargs)
            people.extend(resp.get("members") or [])
            cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                self._users |= {u["id"]: u for u in people if u.get("id")}
                return people
        # a list cut short could leave out someone outside the household
        raise RuntimeError(f"this Slack has more people than {MAX_CONTEXT_PAGES} pages of them")

    async def dm_channel(self, user_id: str) -> str:
        """The direct message with this person, opened if it never has been: a
        session the clock started has no message to reply under."""
        if user_id not in self._dms:
            resp = await self._call("conversations_open", users=user_id)
            self._dms[user_id] = resp["channel"]["id"]
        return self._dms[user_id]

    async def alert(self, text: str) -> None:
        """Posts an alert, rendered harmless, and outside a DM records its
        thread as a conversation of hers, so that a member's reply under it
        reaches her as one in a thread begun with @wanda does. In a DM every
        reply already does, and a thread recorded there would read as a
        channel's. Nothing is recorded with no store open, and a record that
        fails is logged, not raised: the alert is posted, and a caller that
        retried it would post it twice."""
        resp = await self._call(
            "chat_postMessage", channel=self.cfg.alerts_to,
            text=truncate_text(f"⚠️ {harmless(text)}"),
            # the mark by which a frame shows it as an alert posted in her
            # name, never as something she said
            metadata={"event_type": ALERT_EVENT, "event_payload": {"for": "the household"}},
        )
        channel, ts = resp.get("channel") or "", resp.get("ts")
        # an alert to a user's id goes to their DM with her, and which channel
        # Slack answers with for one is not documented
        dm = self.cfg.alerts_to.startswith(("U", "W")) or channel.startswith("D")
        if self.store is None or dm or not channel or not ts:
            return
        try:
            self.store.create_task(None, channel, ts, kind="mention")
        except Exception as e:
            log.warning("could not record the thread of the alert %s in %s: %s", ts, channel, e)

    # --- daily digest ---

    async def _digest_thread(self, local_date: str) -> str:
        digest = self.store.get_digest(local_date)
        if digest is not None:
            return digest["thread_ts"]
        resp = await self._call(
            "chat_postMessage",
            channel=self.cfg.email_triage_slack_channel_id,
            text=f"🧹 Triage digest — {local_date}",
        )
        self.store.set_digest(local_date, self.cfg.email_triage_slack_channel_id, resp["ts"])
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
                "chat_postMessage", channel=self.cfg.email_triage_slack_channel_id,
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
                "chat_postMessage", channel=self.cfg.email_triage_slack_channel_id,
                thread_ts=fresh, text=line,
            )
