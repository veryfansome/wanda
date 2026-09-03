from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry import default_retry_handlers
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from wanda.config import Config
from wanda.tls import ssl_context
from wanda.transcript import render, trim_thread, user_ids_in

# Set by the harness for an agent session, so `post` can default to the
# conversation that triggered it and record that a reply was sent.
ENV_CHANNEL = "WANDA_SLACK_CONTEXT_CHANNEL"
ENV_THREAD = "WANDA_SLACK_CONTEXT_THREAD"
ENV_MARKER = "WANDA_SLACK_POST_MARKER"
POST_LOG_SUFFIX = ".posts.jsonl"  # beside the marker: what was posted, for the run record
MAX_PAGES = 10


def _client(cfg: Config, user_token: bool = False) -> WebClient:
    token = cfg.slack_user_token if user_token else cfg.slack_bot_token
    if not token:
        which = "WANDA_SLACK_USER_TOKEN" if user_token else "WANDA_SLACK_BOT_TOKEN"
        sys.exit(f"{which} is not set (looked for a .env beside the wanda package)")
    # The SDK default retries connection errors only; 429s would raise.
    return WebClient(
        token=token, ssl=ssl_context(),
        retry_handlers=default_retry_handlers() + [RateLimitErrorRetryHandler(max_retry_count=3)],
    )


def _names(web: WebClient, messages: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for uid in user_ids_in(messages):
        try:
            u = web.users_info(user=uid)["user"]
            prof = u.get("profile") or {}
            names[uid] = prof.get("display_name") or prof.get("real_name") or u.get("name") or uid
        except SlackApiError:
            names[uid] = uid
    return names


def _emit(messages: list[dict], web: WebClient, as_json: bool) -> None:
    if as_json:
        print(json.dumps(messages, indent=2))
    else:
        print(render(messages, _names(web, messages)))


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("slack", help="read and write Slack (also used by agent sessions)")
    verbs = p.add_subparsers(dest="verb", required=True)

    v = verbs.add_parser("history", help="recent messages in a channel or DM")
    v.add_argument("--channel", help=f"channel id (default: ${ENV_CHANNEL})")
    v.add_argument("--limit", type=int, default=50)
    v.add_argument("--json", action="store_true")

    v = verbs.add_parser("thread", help="replies in a thread")
    v.add_argument("--channel")
    v.add_argument("--ts", help=f"thread parent ts (default: ${ENV_THREAD})")
    v.add_argument("--limit", type=int, default=50)
    v.add_argument("--json", action="store_true")

    v = verbs.add_parser("post", help="post a message")
    v.add_argument("--text", required=True)
    v.add_argument("--channel")
    v.add_argument("--thread", help="thread ts to reply in; omit to post at top level")
    v.add_argument("--no-thread", action="store_true", help="post to the channel, not the thread")

    v = verbs.add_parser("search", help="search messages (needs WANDA_SLACK_USER_TOKEN)")
    v.add_argument("query")
    v.add_argument("--limit", type=int, default=20)

    verbs.add_parser("channels", help="list channels the bot is in")

    v = verbs.add_parser("members", help="list members of a channel")
    v.add_argument("--channel")
    v.add_argument("--limit", type=int, default=200, help="max members to name (default 200)")

    v = verbs.add_parser("user", help="look up a user by id")
    v.add_argument("user_id")


def run(cfg: Config, args: argparse.Namespace) -> int:
    verb = args.verb
    channel = getattr(args, "channel", None) or os.environ.get(ENV_CHANNEL)
    web = _client(cfg)

    try:
        if verb == "history":
            if not channel:
                sys.exit("--channel is required")
            msgs = list(web.conversations_history(channel=channel, limit=args.limit)["messages"])
            msgs.reverse()
            _emit(msgs, web, args.json)

        elif verb == "thread":
            ts = args.ts or os.environ.get(ENV_THREAD) or None
            if not channel or not ts:
                sys.exit("--channel and --ts are required")
            # replies pages forward from the parent, so a bare limit would
            # return the START of a long thread, not what was just said.
            msgs, cursor = [], None
            for _ in range(MAX_PAGES):
                kwargs = {"channel": channel, "ts": ts, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = web.conversations_replies(**kwargs)
                msgs.extend(resp["messages"])
                cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
                if not resp.get("has_more") or not cursor:
                    break
            msgs = trim_thread(msgs, args.limit)
            _emit(msgs, web, args.json)

        elif verb == "post":
            if not channel:
                sys.exit("--channel is required")
            # The triggering thread only applies to the triggering channel.
            # (A DM is threaded too: wanda always answers in a thread.)
            env_thread = os.environ.get(ENV_THREAD) or None
            if channel != os.environ.get(ENV_CHANNEL):
                env_thread = None
            thread = None if args.no_thread else (args.thread or env_thread)
            resp = web.chat_postMessage(channel=channel, thread_ts=thread, text=args.text[:39000])
            # Record WHERE this landed. The harness suppresses its own reply
            # only when the agent answered the conversation that triggered it —
            # a post to some other channel must not discharge that obligation.
            if marker := os.environ.get(ENV_MARKER):
                with contextlib.suppress(OSError):
                    with open(marker, "a") as fh:  # one line per post; never truncate
                        fh.write(f"{channel}\t{thread or ''}\n")
                with contextlib.suppress(OSError):
                    with open(marker + POST_LOG_SUFFIX, "a") as fh:
                        fh.write(json.dumps({"channel": channel, "thread": thread or "", "text": args.text[:4000]}) + "\n")
            print(f"posted to {channel} ts={resp['ts']}")

        elif verb == "search":
            resp = _client(cfg, user_token=True).search_messages(query=args.query, count=args.limit)
            for m in (resp.get("messages") or {}).get("matches") or []:
                ch = (m.get("channel") or {}).get("name", "?")
                print(f"[{ch}] {m.get('username') or m.get('user')}: {(m.get('text') or '')[:300]}")

        elif verb == "channels":
            resp = web.users_conversations(types="public_channel,private_channel,im,mpim", limit=200)
            for c in resp.get("channels") or []:
                label = c.get("name") or ("DM " + (c.get("user") or ""))
                print(f"{c['id']}\t{label}")

        elif verb == "members":
            if not channel:
                sys.exit("--channel is required")
            ids, cursor = [], None
            for _ in range(MAX_PAGES):
                kwargs = {"channel": channel, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = web.conversations_members(**kwargs)
                ids.extend(resp["members"])
                cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
                if not cursor:
                    break
            total = len(ids)
            shown = ids[:args.limit]  # one users.info call each; keep it bounded
            for uid, name in _names(web, [{"user": i} for i in shown]).items():
                print(f"{uid}\t{name}")
            if len(shown) < total or cursor:
                print(f"… showing {len(shown)} of {total}{'+' if cursor else ''} members")

        elif verb == "user":
            u = web.users_info(user=args.user_id)["user"]
            prof = u.get("profile") or {}
            print(json.dumps({
                "id": u.get("id"), "name": u.get("name"),
                "display_name": prof.get("display_name"), "real_name": prof.get("real_name"),
                "tz": u.get("tz"), "is_bot": u.get("is_bot"),
            }, indent=2))
    except SlackApiError as e:
        err = (e.response or {}).get("error", str(e))
        print(f"slack error: {err}", file=sys.stderr)
        if err == "missing_scope":
            print("the app needs reinstalling with updated scopes (see slack/manifest.yaml)", file=sys.stderr)
        return 1
    return 0
