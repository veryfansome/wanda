from __future__ import annotations

import re
from datetime import datetime

MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)(?:\|([^>]*))?>")
SPECIAL_RE = re.compile(r"<!([a-z]+)[^|>]*(?:\|([^>]*))?>")
OTHER_RE = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")
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


def plain(text: str, names: dict[str, str]) -> str:
    """A message as its writer saw it on screen, which is what a memory
    session is handed as their words: names for mentions, a link once, channel
    and @here references as written, and none of the &amp; &lt; &gt; escaping
    Slack sends. A raw <@U123> recorded as a name is a person nobody knows."""
    text = LINK_RE.sub(lambda m: m.group(1) if m.group(2) in (None, "", m.group(1))
                       else f"{m.group(2)} ({m.group(1)})", text or "")
    text = humanize(text, names)
    text = CHANNEL_RE.sub(lambda m: "#" + (m.group(2) or m.group(1)), text)
    text = SPECIAL_RE.sub(lambda m: m.group(2) or "@" + m.group(1), text)
    text = OTHER_RE.sub(lambda m: m.group(2) or m.group(1).removeprefix("mailto:"), text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


TOKEN_RE = re.compile(r"<([^<>]*)>")
PERSON_RE = re.compile(r"(?:@[UW]|#[CG])[A-Z0-9]+(?:\|[^<>]*)?")
SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:[^\s|]+")


def _shown(text: str) -> str:
    # Slack shows an escaped angle bracket as typed
    return text.replace("<", "&lt;").replace(">", "&gt;")


def harmless(text: str) -> str:
    """A post as the harness sends it: nothing in it pings a channel, @here,
    @everyone or a user group, and no link hides its address. Each innermost
    <...> is read once: a mention of a person or a channel, and a link shown
    as its own address, are kept; any other <!...> becomes its label, or @
    and its word; any other link becomes "label (address)"; every other < and
    > is escaped, so that no rendered piece joins another into markup and a
    second pass changes nothing."""
    text = text or ""
    out, at = [], 0
    for m in TOKEN_RE.finditer(text):
        out.append(_shown(text[at:m.start()]))
        at = m.end()
        body = m.group(1)
        target, _, label = body.partition("|")
        link = bool(SCHEME_RE.fullmatch(target))
        if PERSON_RE.fullmatch(body) or (link and label in ("", target, target.removeprefix("mailto:"))):
            out.append(m.group(0))
        elif target.startswith("!"):
            out.append(label or "@" + re.split(r"[\^\s|]", target[1:].strip() + " ")[0])
        elif link:
            out.append(f"{label} ({target.removeprefix('mailto:')})")
        else:
            out.append(_shown(m.group(0)))
    out.append(_shown(text[at:]))
    return "".join(out)


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


def is_mine(message: dict, me: frozenset[str]) -> bool:
    """`me` holds wanda's own Slack user and bot ids. Both are matched because
    a bot's post can carry either."""
    return bool(me & {message.get("user"), message.get("bot_id")})


def render(messages: list[dict], names: dict[str, str], me: frozenset[str] = frozenset()) -> str:
    """A plain-text transcript, oldest first. Untrusted content: the caller is
    responsible for fencing it and telling the model not to obey it.

    The session reading this is wanda, so her own messages are labelled "me"
    rather than with her display name; a mention of her inside a message keeps
    the name its writer used."""
    lines = []
    for m in messages:
        if m.get("subtype") in ("channel_join", "channel_leave"):
            continue
        if is_mine(m, me):
            who = "me"
        else:
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
