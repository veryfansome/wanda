"""The memory digest: what wanda did to her own memory, once a day, in a
thread the owner can reply to (`rule k4`). Lines are queued durably in
wanda.db by the passes and flushed here."""
from __future__ import annotations

import logging
from datetime import datetime

from slack_sdk.errors import SlackApiError

from wanda.actions.slack import MISSING_THREAD_ERRORS, TEXT_LIMIT, esc_inline, truncate_text

log = logging.getLogger(__name__)

MAX_LINES = 15
COUNT_LINE_RESERVE_C = 80   # characters held back inside TEXT_LIMIT so the "… N more" line always fits
KIND_LABEL = {
    "mint": "🆕", "writespec": "📐", "hand-edit": "✍️", "conflict": "⚠️", "rejected": "🧩", "flag": "🚩",
    "offer": "💡", "rule": "📌", "graduated": "⬆️", "retired": "🪦", "lapsed": "⏳", "error": "❌",
    "verify": "🔐",
}


def digest_key(local_date: str) -> str:
    return f"memory:{local_date}"


async def _post_parent(slack, store, channel: str, key: str, local_date: str) -> str:
    """Post the day's digest parent and record it. Called a second time only
    when Slack says the recorded one is gone."""
    resp = await slack._call("chat_postMessage", channel=channel, text=f"🧠 wanda memory — {local_date}")
    store.set_digest(key, channel, resp["ts"])
    return resp["ts"]


async def post_digest(slack, store, cfg, local_date: str | None = None) -> int:
    """Post pending lines under today's memory digest parent. Returns the
    number of lines posted. Idempotent: lines are marked posted only after
    Slack accepts them."""
    pending = store.digest_pending()
    if not pending:
        return 0
    local_date = local_date or datetime.now().astimezone().strftime("%Y-%m-%d")
    channel = cfg.email_triage_slack_channel_id
    key = digest_key(local_date)
    parent = store.get_digest(key)
    thread_ts = parent["thread_ts"] if parent else await _post_parent(slack, store, channel, key, local_date)
    # MAX_LINES is the daily shape; this budget is what stops a day of long
    # lines being cut mid-line with nothing saying so. Lengths are characters,
    # as TEXT_LIMIT is, and are measured after escaping — what Slack receives.
    budget = TEXT_LIMIT - COUNT_LINE_RESERVE_C
    lines: list[str] = []
    used = 0
    for r in pending[:MAX_LINES]:
        line = f"{KIND_LABEL.get(r['kind'], '•')} {esc_inline(r['text'])}"
        if lines and used + 1 + len(line) > budget:
            break
        if not lines and len(line) > budget:
            line = line[:budget - 1] + "…"   # esc_inline expands & 1->5, so one line can outgrow a whole post
        used += (1 if lines else 0) + len(line)
        lines.append(line)
    hidden = len(pending) - len(lines)
    if hidden:
        lines.append(f"… {hidden} more — `wanda memory digest --all` lists them")
    text = truncate_text("\n".join(lines))
    try:
        await slack._call("chat_postMessage", channel=channel, thread_ts=thread_ts, text=text)
    except SlackApiError as e:
        # Only a genuinely missing parent may start a new one. Anything else —
        # a rate limit — must propagate, or every retry churns a fresh parent.
        if (e.response or {}).get("error") not in MISSING_THREAD_ERRORS:
            raise
        log.warning("memory digest parent %s is gone; starting a fresh one", thread_ts)
        store.clear_digest(key)
        thread_ts = await _post_parent(slack, store, channel, key, local_date)
        await slack._call("chat_postMessage", channel=channel, thread_ts=thread_ts, text=text)
    # Everything pending was represented (shown or counted), so all of it is posted.
    store.digest_mark_posted([r["id"] for r in pending])
    return len(pending)
