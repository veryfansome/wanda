from __future__ import annotations

import re
from datetime import datetime

MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
BODY_LIMIT = 1200


def _ts_label(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "?"


def humanize(text: str, names: dict[str, str]) -> str:
    """Turn Slack's wire markup into something readable in a prompt: <@U123>
    becomes @alice, <url|label> becomes label (url)."""
    text = MENTION_RE.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), text or "")
    return LINK_RE.sub(lambda m: f"{m.group(2) or m.group(1)} ({m.group(1)})", text)


def user_ids_in(messages: list[dict]) -> set[str]:
    ids: set[str] = set()
    for m in messages:
        if m.get("user"):
            ids.add(m["user"])
        ids.update(MENTION_RE.findall(m.get("text") or ""))
    return ids


def trim_thread(messages: list[dict], limit: int) -> list[dict]:
    """Keep the thread parent plus the NEWEST replies. Note `messages[-(n):]`
    with n == 0 is the whole list, not an empty tail — so limits of 0 and 1
    have to be handled before slicing."""
    if limit <= 0:
        return []
    if len(messages) <= limit:
        return messages
    if limit == 1:
        return messages[-1:]
    return [messages[0]] + messages[-(limit - 1):]


def render(messages: list[dict], names: dict[str, str]) -> str:
    """A plain-text transcript, oldest first. Untrusted content: the caller is
    responsible for fencing it and telling the model not to obey it."""
    lines = []
    for m in messages:
        if m.get("subtype") in ("channel_join", "channel_leave"):
            continue
        who = names.get(m.get("user") or "", m.get("username") or m.get("bot_id") or "unknown")
        body = humanize(m.get("text") or "", names).strip()
        if files := m.get("files"):
            body += " [attached: " + ", ".join(f.get("name", "file") for f in files) + "]"
        if not body:
            continue
        if len(body) > BODY_LIMIT:
            body = body[:BODY_LIMIT] + "…"
        lines.append(f"[{_ts_label(m.get('ts', ''))}] {who}: {body}")
    return "\n".join(lines) or "(no readable messages)"
