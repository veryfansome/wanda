from __future__ import annotations

import fcntl
import os
import re
from urllib.parse import quote, unquote
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from wanda.memory.vault import (
    LEDGER_LINE_CAP_B,
    ULID_RE,
    Vault,
    clean_text,
    sha_text,
    ulid as new_ulid,
)

# Who wrote a line. The tier a claim gets is NOT read from this field — the
# index derives it from what the harness can verify (Slack authorship for
# `owner`, the task's kind for `agent`); see index.tier_for_obs.
SOURCES = ("triage", "agent", "owner", "harness", "import")
OPS = ("", "retract", "attest", "rule", "veto", "pin", "retire", "unretire", "open")
# Provenance tiers, least to most trusted.
TIERS = ("email", "session", "owner")

_FIELD_KEYS = ("src", "op", "cause", "due", "until", "tier", "ref")
_SUBJECT_RE = r"[a-z]+/[a-z0-9][a-z0-9._+@-]*"
LINE_RE = re.compile(
    r"^- (?P<hm>\d{2}:\d{2})Z `(?P<subject>" + _SUBJECT_RE + r")` `(?P<facet>[a-z0-9-]{0,32})`"
    r"(?P<fields>(?: [a-z]+=[^\s`]+)*) — (?P<text>[^`\n^]*?) \^(?P<ulid>[0-9a-hjkmnp-tv-z]{16})$"
)


@dataclass
class Observation:
    subject: str
    facet: str
    text: str
    src: str = "harness"
    op: str = ""
    cause: str = ""
    due: str = ""
    until: str = ""
    ref: str = ""            # for attest/veto/pin: the claim or key it addresses
    ulid: str = field(default_factory=new_ulid)
    when: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Filled by the parser:
    day: str = ""
    path: str = ""
    lineno: int = 0

    def __post_init__(self):
        if not self.day:
            self.day = self.when.strftime("%Y-%m-%d")


def format_line(o: Observation) -> str:
    fields = [f"src={o.src}"]
    if o.op:
        fields.append(f"op={o.op}")
    if o.cause:
        fields.append(f"cause={o.cause}")
    if o.due:
        fields.append(f"due={o.due}")
    if o.until:
        fields.append(f"until={o.until}")
    if o.ref:
        fields.append(f"ref={quote(o.ref, safe=':/|,@#^.-_=+')}")
    text = clean_text(o.text)
    line = f"- {o.when.strftime('%H:%M')}Z `{o.subject}` `{o.facet}` {' '.join(fields)} — {text} ^{o.ulid}"
    if len(line.encode()) > LEDGER_LINE_CAP_B:
        # Trim the free text, never a field.
        over = len(line.encode()) - LEDGER_LINE_CAP_B
        text = clean_text(text, cap_b=max(0, len(text.encode()) - over - 1))
        line = f"- {o.when.strftime('%H:%M')}Z `{o.subject}` `{o.facet}` {' '.join(fields)} — {text} ^{o.ulid}"
    return line


class Malformed(ValueError):
    pass


def parse_line(line: str, day: str = "", path: str = "", lineno: int = 0) -> Observation:
    m = LINE_RE.match(line.rstrip("\n"))
    if not m:
        raise Malformed(line[:120])
    fields: dict[str, str] = {}
    for kv in m.group("fields").split():
        k, _, v = kv.partition("=")
        if k not in _FIELD_KEYS:
            raise Malformed(f"unknown field {k}")
        fields[k] = v
    src = fields.get("src", "")
    if src not in SOURCES:
        raise Malformed(f"bad src {src!r}")
    op = fields.get("op", "")
    if op not in OPS:
        raise Malformed(f"bad op {op!r}")
    for k in ("due", "until"):
        if k in fields and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[k]):
            raise Malformed(f"bad {k}")
    hm = m.group("hm")
    when = datetime.strptime(f"{day or '1970-01-01'} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return Observation(
        subject=m.group("subject"), facet=m.group("facet"), text=m.group("text"),
        src=src, op=op, cause=fields.get("cause", ""), due=fields.get("due", ""),
        until=fields.get("until", ""), ref=unquote(fields.get("ref", "")), ulid=m.group("ulid"),
        when=when, day=day or when.strftime("%Y-%m-%d"), path=path, lineno=lineno,
    )


def day_header(day: str) -> str:
    return f"---\nkind: ledger\nday: {day}\n---\n# {day}\n\n"


def append(vault: Vault, o: Observation, lock_timeout_s: float = 5.0) -> Path:
    """One line, one O_APPEND write, under flock. The day file's header is
    written under the same lock on first use, and a file whose last byte is
    not a newline (a crashed writer) gets one first so two records can never
    fuse."""
    if not o.subject or not ULID_RE.match(o.ulid):
        raise ValueError("observation needs a subject and a 16-char ulid")
    vault.ledger_dir.mkdir(parents=True, exist_ok=True)
    lock_path = vault.ledger_dir / ".lock"
    path = vault.ledger_dir / f"{o.day}.md"
    line = format_line(o) + "\n"
    with open(lock_path, "w") as lock:
        _flock_blocking(lock, lock_timeout_s)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            size = os.fstat(fd).st_size
            prefix = b""
            if size == 0:
                prefix = day_header(o.day).encode()
            else:
                with open(path, "rb") as rf:
                    rf.seek(size - 1)
                    if rf.read(1) != b"\n":
                        prefix = b"\n"
            os.write(fd, prefix + line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    return path


def _flock_blocking(fh, timeout_s: float) -> None:
    import time
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("ledger lock busy")
            time.sleep(0.01)


@dataclass
class Rejected:
    path: str
    lineno: int
    line: str
    why: str

    @property
    def sha(self) -> str:
        return sha_text(f"{self.path}:{self.lineno}:{self.line}")


def day_files(vault: Vault) -> list[Path]:
    if not vault.ledger_dir.is_dir():
        return []
    return sorted(p for p in vault.ledger_dir.glob("????-??-??.md"))


def iter_observations(vault: Vault, days: list[Path] | None = None) -> Iterator[Observation | Rejected]:
    """Every record in every day file, oldest first. Unparseable lines are
    yielded as Rejected so the caller can report them once and move on —
    the ledger is append-only, so nothing is ever moved or fixed in place."""
    for p in (days if days is not None else day_files(vault)):
        day = p.stem
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for n, raw in enumerate(text.splitlines(), 1):
            if not raw.startswith("- "):
                continue  # header, headings, blank lines
            try:
                yield parse_line(raw, day=day, path=vault.rel(p), lineno=n)
            except Malformed as e:
                yield Rejected(vault.rel(p), n, raw[:300], str(e)[:120])


def report_rejected(vault: Vault, items: list[Rejected]) -> int:
    """Append to belt/ledger/rejected.md, deduped by (file, line, text)
    hash so a bad line that stays in place is reported exactly once."""
    if not items:
        return 0
    path = vault.ledger_dir / "rejected.md"
    seen: set[str] = set()
    if path.exists():
        seen = set(re.findall(r"\bsha=([0-9a-f]{16})\b", path.read_text(encoding="utf-8")))
    new = [r for r in items if r.sha not in seen]
    if not new:
        return 0
    lines = []
    if not path.exists():
        lines.append("# Rejected ledger lines\n\nLines wanda could not parse. Left in place; listed here once.\n\n")
    for r in new:
        lines.append(f"- {r.path}:{r.lineno} sha={r.sha} — {clean_text(r.why, 120)} — `{clean_text(r.line, 200)}`\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(lines))
    return len(new)
