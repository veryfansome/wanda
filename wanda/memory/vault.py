from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- layout ---------------------------------------------------------------

L2_DIRS = ("people", "orgs", "topics", "prefs", "open")
TYPE_TO_DIR = {"person": "people", "org": "orgs", "topic": "topics", "pref": "prefs"}
DIR_TO_TYPE = {v: k for k, v in TYPE_TO_DIR.items()}
SUBJECT_TYPES = ("person", "org", "topic", "pref", "list")
BEGIN = "<!-- wanda:begin claims -->"
END = "<!-- wanda:end claims -->"
INDEX_BEGIN = "<!-- wanda:begin index -->"
INDEX_END = "<!-- wanda:end index -->"

# Promotion thresholds, shared by the index (status) and the passes (graduation).
GRADUATE_CAUSES = 3
GRADUATE_DAYS = 2

# Caps, in bytes. Enforced by generators, never requested of writers.
PROJECTION_CAP_B = 4096
WALK_CAP_B = 3000
TRIAGE_MEMORY_CAP_B = 1200
WRITESPEC_PROSE_CAP_B = 1200
NOTE_CAP_B = 8192
CLAIM_TEXT_CAP_B = 600
LEDGER_LINE_CAP_B = 1024   # what the free text is trimmed to fit; fields are never truncated, so a long cause or ref can exceed it


@dataclass
class Vault:
    root: Path

    def __post_init__(self):
        # A symlinked vault (say, into an iCloud-synced Obsidian folder) must
        # yield the same paths from `inside()` and from `rel()` — including
        # before the directory exists, since nothing re-resolves after
        # ensure_vault() has created it.
        self.root = Path(self.root).expanduser().resolve()

    @property
    def ledger_dir(self) -> Path:
        return self.root / "belt" / "ledger"

    @property
    def subjects_dir(self) -> Path:
        return self.root / "belt" / "subjects"

    @property
    def retired_dir(self) -> Path:
        return self.root / "retired"

    def dir_for(self, subject_type: str) -> Path:
        return self.root / TYPE_TO_DIR[subject_type]

    def note_path(self, subject: str) -> Path | None:
        """`person/robin-vale` -> people/robin-vale.md, or None for a
        subject type that has no curated home (lists live on orgs)."""
        t, _, slug = subject.partition("/")
        d = TYPE_TO_DIR.get(t)
        return self.root / d / f"{slug}.md" if d else None

    def subject_file(self, subject: str) -> Path:
        t, _, slug = subject.partition("/")
        return self.subjects_dir / t / f"{slug}.md"

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def inside(self, rel: str) -> Path:
        """Resolve a vault-relative path and refuse anything that escapes the
        vault (`../`, absolute paths, symlinks out). Every CLI verb that takes
        a path goes through here."""
        if not rel or rel.startswith("/") or "\x00" in rel:
            raise ValueError(f"not a vault path: {rel!r}")
        root = self.root.resolve()
        p = (self.root / rel).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            raise ValueError(f"{rel!r} is outside the vault") from None
        return p

    def note_rel(self, subject: str) -> str:
        """`person/x` -> `people/x.md` ('' for a type with no curated home)."""
        p = self.note_path(subject)
        return self.rel(p) if p else ""

    def l2_notes(self):
        """Every curated note directly under an L2 directory; a leading `_`
        excludes a file. Not recursive, deliberately: a flat path is what
        subject_for_doc and _is_curated_note require, so a note in a
        subdirectory has no subject and is invisible to the index, to drift
        detection and to referrer rewriting."""
        for d in L2_DIRS:
            p = self.root / d
            if p.is_dir():
                for f in sorted(p.glob("*.md")):
                    if f.name != "CLAUDE.md" and not f.name.startswith("_"):
                        yield f

    def history_path(self, note_rel: str) -> Path:
        return self.retired_dir / "history" / note_rel

    def writespecs(self):
        """Every CLAUDE.md in the vault, root first, then by depth."""
        out = [self.root / "CLAUDE.md"] if (self.root / "CLAUDE.md").is_file() else []
        for d in L2_DIRS:
            p = self.root / d / "CLAUDE.md"
            if p.is_file():
                out.append(p)
        return out


# --- text hygiene -----------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LIVE_SQL = "('owner-stated','corroborated','provisional','disputed')"
OBS_OPS_SQL = "('', 'rule', 'attest')"  # ledger lines that carry content, as opposed to bookkeeping ops


def clean_text(text: str, cap_b: int = CLAIM_TEXT_CAP_B) -> str:
    """One sanitizer for every model- or user-authored string before it
    touches markdown the harness later parses. Removes what could forge a
    field, an edge, a block id or a record boundary: newlines, control chars,
    backticks, `^`, `[[`/`]]`, `::`, and the em-dash separator."""
    t = unicodedata.normalize("NFC", text or "")
    t = _CONTROL.sub(" ", t)
    t = t.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    t = t.replace("`", "'").replace("^", "").replace("—", "-")
    t = t.replace("[[", "[").replace("]]", "]").replace("::", ":")
    t = " ".join(t.split()).strip()
    # A claim line that started with a heading, list or quote marker would be
    # re-parsed as structure, not as a claim.
    t = re.sub(r"^[#>*+\-\s]+", "", t)
    t = t.replace("<", "‹").replace(">", "›")  # no tag can form in stored text
    return truncate_bytes(t, cap_b)


def clean_prose(text: str, cap_b: int = 4000) -> str:
    """For multi-line prose wanda writes into a write-spec: control characters
    and tag/marker forms out; markdown structure, wikilinks and inline fields
    kept (write-spec prose is never parsed as claims, and the projection
    renders links as paths)."""
    t = unicodedata.normalize("NFC", text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _CONTROL.sub(" ", t)
    t = t.replace("<!--", "‹!--").replace("-->", "--›").replace("<", "‹").replace(">", "›")
    lines = [" ".join(ln.split()) for ln in t.split("\n")]
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return truncate_bytes(out, cap_b)


def truncate_words(text: str, cap_b: int) -> str:
    """Cap at a word boundary with an ellipsis, for prose that was never a
    single fact to begin with (imports)."""
    if nbytes(text) <= cap_b:
        return text
    cut = truncate_bytes(text, cap_b - 1)
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


def truncate_bytes(text: str, cap_b: int) -> str:
    """Cut on a character boundary so a cap never splits a code point."""
    b = text.encode("utf-8")
    if len(b) <= cap_b:
        return text
    return b[:cap_b].decode("utf-8", "ignore").rstrip()


def nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def slugify(text: str, max_len: int = 48) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t[:max_len].strip("-")


# --- ids ---------------------------------------------------------------------

_B32 = "0123456789abcdefghjkmnpqrstvwxyz"


def ulid(now_ms: int | None = None) -> str:
    """16 chars: 10 of millisecond timestamp + 6 random, Crockford base32,
    lowercase. Time-sortable, needs no coordination, and short enough to be
    an Obsidian block id."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    out = []
    for _ in range(10):
        out.append(_B32[ms & 31])
        ms >>= 5
    ts = "".join(reversed(out))
    rnd = "".join(_B32[b & 31] for b in secrets.token_bytes(6))
    return ts + rnd


ULID_RE = re.compile(r"^[0-9a-hjkmnp-tv-z]{16}$")


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


# --- atomic files -------------------------------------------------------------

def write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """mkstemp in the same directory (unique per writer, so two concurrent
    generators can never replace each other's half-written temp), fsync,
    os.replace. Readers see the old file or the new one, never nothing.
    A file that does not exist yet keeps mkstemp's 0600, so a note wanda
    creates is private by default; pass `mode` for anything else (the L1
    subject files ask for 0444, render.py:133)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        elif path.exists():
            # Keep the target's mode (an L1 file is 0444 and must stay so).
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        with _suppress():
            os.unlink(tmp)
        raise


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


@dataclass
class Snapshot:
    """What a note looked like when read; re-checked before it is written,
    so a hand edit landing in between aborts the write instead of losing it."""
    path: Path
    mtime_ns: int
    size: int
    sha: str

    @classmethod
    def take(cls, path: Path) -> "Snapshot":
        st = path.stat()
        return cls(path, st.st_mtime_ns, st.st_size, sha_file(path))

    @classmethod
    def of_read(cls, path: Path, st: os.stat_result, data: bytes) -> "Snapshot":
        """For content just read: the check then covers the whole read to
        write window, not only the instant before the write."""
        return cls(path, st.st_mtime_ns, len(data), sha_bytes(data))

    def unchanged(self) -> bool:
        try:
            st = self.path.stat()
            return st.st_mtime_ns == self.mtime_ns and st.st_size == self.size and sha_file(self.path) == self.sha
        except OSError:
            return False   # an unreadable file is not unchanged: abandon this one write, never the whole pass


def write_if_unchanged(snap: Snapshot, text: str) -> bool:
    """Optimistic concurrency for L2 notes: write only if nothing touched the
    file since it was read. Returns False when the write was abandoned."""
    if not snap.unchanged():
        return False
    write_atomic(snap.path, text)
    return True


# --- frontmatter ----------------------------------------------------------------

@dataclass
class Doc:
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""


def parse_frontmatter(text: str) -> Doc:
    """A small YAML subset: `key: scalar`, `key: [a, b]`, and `key:` followed
    by `- item` lines (indented or not). Quoted scalars are unquoted. Enough
    for the fields wanda writes. Nested maps and block scalars are NOT
    understood and would be flattened by a machine rewrite — wanda's notes
    never carry them, and hand-written ones belong under `## Notes`."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return Doc({}, text)
    end = text.find("\n---", 4)
    if end < 0:
        return Doc({}, text)
    eol = text.find("\n", end + 1)
    if text[end + 4: eol if eol >= 0 else len(text)].strip():
        # `----` or `--- x` is not a closing fence. Keep that whole line, and
        # everything below it, in the body instead of eating its first four
        # bytes and welding the remainder on.
        head = text[4:end]
        body = text[end + 1:]
    else:
        head = text[4:end]
        body = text[end + 4:]
        if body.startswith("\n"):
            body = body[1:]
    meta: dict[str, Any] = {}
    key = None
    for raw in head.splitlines():
        if not raw.strip():
            continue
        if re.match(r"^\s*- ", raw) and key is not None:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(_unquote(raw.split("- ", 1)[1].strip()))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            meta[key] = []
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            meta[key] = [_unquote(x.strip()) for x in _split_csv(inner)] if inner else []
        else:
            meta[key] = _scalar(val)
    if not meta and head.strip():
        # A head with content but not one `key:` line is the owner's `---`
        # divider (or a YAML shape this parser does not read), not
        # frontmatter; keeping it in the body is what stops a machine rewrite
        # from deleting it.
        return Doc({}, text)
    return Doc(meta, body)


def _split_csv(s: str) -> list[str]:
    out, cur, q = [], "", None
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur += ch
        elif ch == ",":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        inner = s[1:-1]
        return inner.replace('\\"', '"') if s[0] == '"' else inner
    return s


def _scalar(v: str) -> Any:
    if v.lower() in ("true", "false", "yes", "no"):
        return v.lower() in ("true", "yes")
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return _unquote(v)


def _quote(v: Any) -> str:
    """Quote only what YAML would misread: a leading indicator character,
    `: ` or ` #` inside, quotes, surrounding space, reserved words, numbers."""
    s = str(v)
    needs = (
        s == "" or s[0] in "[]{}&*!|>'\"%@`#-?:," or ": " in s or " #" in s or '"' in s
        or s != s.strip() or s.lower() in ("true", "false", "null", "yes", "no", "~")
        or re.fullmatch(r"-?\d+(\.\d+)?", s) is not None or s.endswith(":")
    )
    return '"' + s.replace('"', '\\"') + '"' if needs else s


def render_frontmatter(meta: dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                lines.extend(f"  - {_quote(x)}" for x in v)
        elif v is None:
            continue
        else:
            lines.append(f"{k}: {_quote(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def render_doc(doc: Doc) -> str:
    return render_frontmatter(doc.meta) + doc.body
