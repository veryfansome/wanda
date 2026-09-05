from __future__ import annotations

import email.policy
import html as html_mod
import logging
import random
import re
import threading
import time
from email.parser import BytesParser
from typing import Any, Callable

from imapclient import IMAPClient
from imapclient.exceptions import CapabilityError

from wanda.config import Config
from wanda.store import Store, utcnow
from wanda.tls import ssl_context

log = logging.getLogger(__name__)

FOLDER = "INBOX"
FETCH_PARTIAL = b"BODY.PEEK[]<0.65536>"
FETCH_BATCH = 20
IDLE_CHECK_S = 30
MAX_IDLE_FAILURES = 3
# idle_check swallows EOF/ECONNRESET and returns [] instantly, so an empty
# result that arrives far too fast means the socket is dead, not quiet.
INSTANT_RETURN_S = 1.0
MAX_INSTANT_EMPTY = 3
POLL_CYCLES_BEFORE_IDLE_RETRY = 10
HEADER_LIMIT = 512


class DeadConnection(Exception):
    """IDLE is returning instantly with no data — reconnect."""


def connect(cfg: Config) -> IMAPClient:
    client = IMAPClient(
        cfg.email_imap_host, port=cfg.email_imap_port, ssl=True, ssl_context=ssl_context(), timeout=30
    )
    client.login(cfg.email_icloud_email, cfg.email_icloud_app_password)
    return client


def resolve_trash_folder(client: IMAPClient, cfg: Config) -> str:
    if cfg.email_trash_folder:
        return cfg.email_trash_folder
    found = client.find_special_folder(b"\\Trash")
    if found:
        return found if isinstance(found, str) else found.decode()
    return "Deleted Messages"  # iCloud's conventional name


def _strip_html(text: str) -> str:
    # `(?:</\1\s*>|\Z)` so an UNTERMINATED <script>/<style> is stripped to the
    # end of the text, not left to leak its body as prose — the 64 KB partial
    # fetch makes a missing closing tag routine, and a body larger than the
    # fetch window is trivially attacker-controlled.
    text = re.sub(r"<(script|style)\b[^>]*>.*?(?:</\1\s*>|\Z)", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def parse_raw(raw: bytes, snippet_bytes: int) -> dict[str, str]:
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)

    def hdr(name: str) -> str:
        try:
            return str(msg.get(name) or "").strip()
        except Exception:
            return ""

    body = ""
    parse_failed = False
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            body = part.get_content()
            if part.get_content_type() == "text/html":
                body = _strip_html(body)
        if not body:
            # No preferred part, or it was empty — look for any text part
            # before falling back to raw bytes.
            for sub in msg.walk():
                if sub.get_content_maintype() == "text" and not sub.is_multipart():
                    candidate = sub.get_content()
                    if candidate.strip():
                        body = (_strip_html(candidate) if sub.get_content_subtype() == "html"
                                else candidate)
                        break
    except Exception:
        # The 64KB partial fetch can truncate MIME mid-part.
        parse_failed = True
    if not body and parse_failed:
        # Only when structured parsing actually failed: otherwise this splices
        # MIME scaffolding into the body for a legitimately text-free mail.
        # Strip it like any other HTML body — a truncated part can carry an
        # unterminated <script> or raw tags — rather than splicing it raw.
        tail = raw.split(b"\r\n\r\n", 1)
        body = _strip_html(tail[1].decode("utf-8", "replace")) if len(tail) == 2 else ""
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return {
        # Headers are attacker-controlled and unbounded; every consumer
        # (prompts, Slack posts, digests) assumes they are sane sizes.
        "message_id": hdr("Message-ID")[:HEADER_LIMIT],
        "from_addr": hdr("From")[:HEADER_LIMIT],
        "subject": hdr("Subject")[:HEADER_LIMIT],
        "date_hdr": hdr("Date")[:HEADER_LIMIT],
        # The body is carried to triage in memory and never persisted; the key
        # is "body", not "snippet", because nothing stores a snippet any more.
        "body": body[:snippet_bytes] or "(no text content)",
    }


def fetch_parsed(client: IMAPClient, uids: list[int], snippet_bytes: int) -> list[tuple[int, dict[str, str]]]:
    out: list[tuple[int, dict[str, str]]] = []
    for i in range(0, len(uids), FETCH_BATCH):
        batch = uids[i : i + FETCH_BATCH]
        data = client.fetch(batch, [FETCH_PARTIAL])
        for uid in batch:
            item: dict[bytes, Any] = data.get(uid, {})
            raw = next((v for k, v in item.items() if k.startswith(b"BODY[")), None)
            if raw is None:
                log.warning("uid %s: no body in fetch response, skipping", uid)
                continue
            out.append((uid, parse_raw(raw, snippet_bytes)))
    return out


def fetch_body(cfg: Config, folder: str, uidvalidity: int, uid: int) -> str | None:
    """Re-fetch and parse one message body from IMAP, addressed by
    (folder, uidvalidity, uid). Triage's fallback when a body was not carried
    in memory — a crash between ingest and triage. Returns None when the
    message is gone or the mailbox was re-created (UIDVALIDITY changed), in
    which case the UID no longer names the same message and triage classifies
    from the headers alone. A fresh short-lived connection, so it never
    touches the watcher thread's IDLE socket."""
    try:
        with connect(cfg) as client:
            info = client.select_folder(folder, readonly=True)
            if int(info[b"UIDVALIDITY"]) != uidvalidity:
                log.warning("re-fetch of uid %s: UIDVALIDITY changed; body unrecoverable", uid)
                return None
            data = client.fetch([uid], [FETCH_PARTIAL])
            item: dict[bytes, Any] = data.get(uid, {})
            raw = next((v for k, v in item.items() if k.startswith(b"BODY[")), None)
            if raw is None:
                return None
            return parse_raw(raw, cfg.email_snippet_bytes)["body"]
    except Exception:
        log.exception("re-fetch of uid %s in %s failed", uid, folder)
        return None


def dedupe_key_for(parsed: dict[str, str], folder: str, uidvalidity: int, uid: int) -> str:
    return parsed["message_id"] or f"{folder}:{uidvalidity}:{uid}"


class ImapWatcher(threading.Thread):
    """One dedicated thread for one mailbox. IDLE is only a latency
    optimization: every wake (or timeout, or reconnect) runs a catch-up
    UID SEARCH, which is the actual source of truth for new mail."""

    def __init__(self, cfg: Config, store: Store, notify: Callable[[], None]):
        super().__init__(name="imap-watcher", daemon=True)
        self.cfg = cfg
        self.store = store
        self.notify = notify  # threadsafe kick for the processor
        self._stop = threading.Event()
        self._idle_failures = 0
        self._poll_cycles = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with connect(self.cfg) as client:
                    info = client.select_folder(FOLDER)
                    uidvalidity = self._sync_cursor(info)
                    # NB: _idle_failures deliberately survives reconnects. An
                    # IDLE failure always unwinds through here, so resetting it
                    # would make the poll fallback unreachable and turn a
                    # server that refuses IDLE into a reconnect storm.
                    log.info("imap connected, uidvalidity=%s", uidvalidity)
                    while not self._stop.is_set():
                        if self._catch_up(client, uidvalidity):
                            self.notify()
                        self.store.set_meta("last_successful_poll_at", utcnow())
                        self._wait_for_activity(client)
                        # Only now is the connection proven end to end. Resetting
                        # at SELECT would turn any repeatable post-connect fault
                        # into a ~1/second login loop against iCloud.
                        backoff = 1.0
            except Exception:
                if self._stop.is_set():
                    break
                log.exception("imap watcher error; reconnecting in %.0fs", backoff)
                self._stop.wait(backoff + random.uniform(0, backoff / 2))
                backoff = min(backoff * 2, 300.0)

    def _sync_cursor(self, select_info: dict) -> int:
        uidvalidity = int(select_info[b"UIDVALIDITY"])
        uidnext = int(select_info[b"UIDNEXT"])
        cur = self.store.get_cursor(FOLDER)
        if cur is None:
            log.info("no cursor; baselining at uid %d (only new mail from now on)", uidnext - 1)
            self.store.set_cursor(FOLDER, uidvalidity, uidnext - 1)
        elif cur[0] != uidvalidity:
            # Re-baseline, never re-triage: Message-ID dedupe is the second line
            # of defense, but a full-mailbox replay is never acceptable.
            log.warning("UIDVALIDITY changed %s -> %s; re-baselining", cur[0], uidvalidity)
            self.store.set_cursor(FOLDER, uidvalidity, uidnext - 1)
        return uidvalidity

    def _catch_up(self, client: IMAPClient, uidvalidity: int) -> int:
        cur = self.store.get_cursor(FOLDER)
        last_seen = cur[1] if cur else 0
        # "UID n:*" always returns the last message even when n > max, so filter.
        uids = sorted(u for u in client.search(["UID", f"{last_seen + 1}:*"]) if u > last_seen)
        if not uids:
            return 0
        ingested = 0
        for uid, parsed in fetch_parsed(client, uids, self.cfg.email_snippet_bytes):
            key = dedupe_key_for(parsed, FOLDER, uidvalidity, uid)
            if self.store.ingest_message(
                dedupe_key=key,
                message_id=parsed["message_id"],
                folder=FOLDER,
                uidvalidity=uidvalidity,
                uid=uid,
                from_addr=parsed["from_addr"],
                subject=parsed["subject"],
                date_hdr=parsed["date_hdr"],
            ):
                # Hand the body to triage in memory; it is never written to the
                # database. A crash before triage loses it, and triage then
                # re-fetches from IMAP by (folder, uidvalidity, uid).
                self.store.stash_body(key, parsed["body"])
                ingested += 1
            # Advance only after the insert committed: crash-safe, at-least-once.
            self.store.set_cursor(FOLDER, uidvalidity, uid)
        if ingested:
            log.info("ingested %d new message(s)", ingested)
        return ingested

    def _wait_for_activity(self, client: IMAPClient) -> None:
        if self._idle_failures >= MAX_IDLE_FAILURES:
            # Degraded: plain polling on this connection. Periodically clear the
            # streak so a server that recovers IDLE support is picked back up.
            self._poll_cycles += 1
            if self._poll_cycles >= POLL_CYCLES_BEFORE_IDLE_RETRY:
                self._poll_cycles = 0
                self._idle_failures = 0
                log.info("retrying IDLE after %d polling cycles", POLL_CYCLES_BEFORE_IDLE_RETRY)
            self._stop.wait(self.cfg.email_poll_fallback_s)
            return
        try:
            client.idle()
            try:
                deadline = time.monotonic() + self.cfg.email_idle_timeout_s
                instant_empty = 0
                while not self._stop.is_set() and time.monotonic() < deadline:
                    started = time.monotonic()
                    if client.idle_check(timeout=IDLE_CHECK_S):
                        break
                    if time.monotonic() - started < INSTANT_RETURN_S:
                        instant_empty += 1
                        if instant_empty >= MAX_INSTANT_EMPTY:
                            raise DeadConnection("idle_check returning instantly with no data")
                    else:
                        instant_empty = 0
            finally:
                client.idle_done()
            self._idle_failures = 0  # count consecutive failures, not lifetime ones
            self.store.set_meta("imap_mode", "idle")
        except CapabilityError:
            # The server refuses IDLE. That is what the degrade counter is for;
            # the socket itself is fine, so keep it and poll rather than
            # reconnecting in a loop.
            self._idle_failures += 1
            log.warning("server refused IDLE (%d/%d)", self._idle_failures, MAX_IDLE_FAILURES)
            if self._idle_failures >= MAX_IDLE_FAILURES:
                self.store.set_meta("imap_mode", "poll")
                self._poll_cycles = 0
                self._stop.wait(self.cfg.email_poll_fallback_s)
                return
            raise
        except Exception:
            # A dead socket (sleep/wake, NAT teardown) is a reconnect, not a
            # reason to give up on IDLE — counting it here would degrade a
            # perfectly IDLE-capable server after a few lid closes.
            log.exception("IDLE cycle failed; reconnecting")
            raise
