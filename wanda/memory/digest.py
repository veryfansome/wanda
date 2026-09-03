"""The memory digest: what wanda did to her own memory, once a day, in a
thread the owner can reply to (`rule k4`). Lines are queued durably in
wanda.db by the passes and flushed here."""
from __future__ import annotations

import logging
from datetime import datetime

from wanda.actions.slack import esc_inline, truncate_text

log = logging.getLogger(__name__)

MAX_LINES = 15
KIND_LABEL = {
    "mint": "🆕", "writespec": "📐", "hand-edit": "✍️", "conflict": "⚠️", "rejected": "🧩", "flag": "🚩",
    "offer": "💡", "rule": "📌", "graduated": "⬆️", "retired": "🪦", "lapsed": "⏳", "audit": "🔍",
    "skipped": "⏸", "error": "❌", "import": "📥", "verify": "🔐",
}


def digest_key(local_date: str) -> str:
    return f"memory:{local_date}"


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
    if parent is None:
        resp = await slack._call("chat_postMessage", channel=channel, text=f"🧠 wanda memory — {local_date}")
        store.set_digest(key, channel, resp["ts"])
        parent = store.get_digest(key)
    thread_ts = parent["thread_ts"]
    shown = pending[:MAX_LINES]
    lines = [f"{KIND_LABEL.get(r['kind'], '•')} {esc_inline(r['text'])}" for r in shown]
    if len(pending) > MAX_LINES:
        lines.append(f"… {len(pending) - MAX_LINES} more — `wanda memory digest` shows everything")
    text = truncate_text("\n".join(lines))
    await slack._call("chat_postMessage", channel=channel, thread_ts=thread_ts, text=text)
    # Everything pending was represented (shown or counted), so all of it is posted.
    store.digest_mark_posted([r["id"] for r in pending])
    return len(pending)
