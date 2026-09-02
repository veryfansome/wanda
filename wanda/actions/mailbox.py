from __future__ import annotations

import logging

from wanda.config import Config
from wanda.watchers.imap_watcher import FOLDER, connect, resolve_trash_folder

log = logging.getLogger(__name__)

# Move outcomes — "already_gone" and "uidvalidity_changed" both count as
# success for the state machine (nothing left to do for that UID).
MOVED = "moved"
ALREADY_GONE = "already_gone"
UIDVALIDITY_CHANGED = "uidvalidity_changed"


def move_to_trash(cfg: Config, uid: int, uidvalidity: int) -> str:
    """Idempotent move-to-Trash over a short-lived connection (volume is tiny
    — caps allow at most ~20/day — so a fresh connection per move is fine and
    avoids sharing the watcher thread's socket). Never expunges INBOX."""
    with connect(cfg) as client:
        info = client.select_folder(FOLDER)
        if int(info[b"UIDVALIDITY"]) != uidvalidity:
            log.warning("uid %d: UIDVALIDITY changed, skipping move", uid)
            return UIDVALIDITY_CHANGED
        if not client.search(["UID", str(uid)]):
            return ALREADY_GONE
        trash = resolve_trash_folder(client, cfg)
        if client.has_capability("MOVE"):
            client.move([uid], trash)
        else:
            client.copy([uid], trash)
            client.delete_messages([uid])
            client.expunge([uid])  # UID EXPUNGE (UIDPLUS): only this message
        log.info("uid %d moved to %r", uid, trash)
        return MOVED
