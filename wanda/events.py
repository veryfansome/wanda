from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    """Normalized event from any watcher. The queue contract is the whole
    pluggability story in v1: a watcher is anything that puts Events on the
    shared asyncio queue (from its own thread via loop.call_soon_threadsafe)."""

    source: str  # "imap" | "slack" | future sources
    dedupe_key: str
    payload: dict[str, Any] = field(default_factory=dict)

