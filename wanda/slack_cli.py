from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from wanda.config import Config
from wanda.tls import ssl_context
from wanda.transcript import render, user_ids_in

# Set by the harness for an agent session, so `post` can default to the
# conversation that triggered it and record that a reply was sent.
ENV_CHANNEL = "WANDA_SLACK_CONTEXT_CHANNEL"
ENV_THREAD = "WANDA_SLACK_CONTEXT_THREAD"
ENV_MARKER = "WANDA_SLACK_POST_MARKER"


def _client(cfg: Config, user_token: bool = False) -> WebClient:
    token = cfg.slack_user_token if user_token else cfg.slack_bot_token
    if not token:
        which = "WANDA_SLACK_USER_TOKEN" if user_token else "WANDA_SLACK_BOT_TOKEN"
        sys.exit(f"{which} is not set")
    return WebClient(token=token, ssl=ssl_context())


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
            ts = args.ts or os.environ.get(ENV_THREAD)
            if not channel or not ts:
                sys.exit("--channel and --ts are required")
            msgs = list(web.conversations_replies(channel=channel, ts=ts, limit=args.limit)["messages"])
            _emit(msgs, web, args.json)

        elif verb == "post":
            if not channel:
                sys.exit("--channel is required")
            thread = None if args.no_thread else (args.thread or os.environ.get(ENV_THREAD))
            resp = web.chat_postMessage(channel=channel, thread_ts=thread, text=args.text[:39000])
            # Tell the harness a reply was delivered, so it doesn't post the
            # session's final text on top of what the agent already said.
            if marker := os.environ.get(ENV_MARKER):
                Path(marker).write_text("posted")
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
            ids = web.conversations_members(channel=channel, limit=200)["members"]
            for uid, name in _names(web, [{"user": i} for i in ids]).items():
                print(f"{uid}\t{name}")

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
