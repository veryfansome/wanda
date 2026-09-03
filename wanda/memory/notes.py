"""L2 notes: claim blocks with edges inside a machine-owned marker region,
and per-directory write-specs (CLAUDE.md) with a generated index block."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wanda.memory.vault import (
    BEGIN, END, INDEX_BEGIN, INDEX_END, parse_frontmatter, render_frontmatter, sha_text,
)

EDGE_RELS = ("derived-from", "owner-said", "owner-edited", "supersedes", "superseded-by",
             "contradicts", "refines", "about", "until", "tier", "retired")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#\^?([^\]|]+))?(?:\|[^\]]*)?\]\]")
CLAIM_LINE_RE = re.compile(r"^(?P<text>.*?)\s+\^(?P<block>[a-z0-9]{1,24})\s*$")
EDGE_LINE_RE = re.compile(r"^- (?P<rel>[a-z-]+):: ?(?P<value>.*)$")


@dataclass
class Edge:
    rel: str
    dst_doc: str = ""
    dst_block: str = ""
    value: str = ""      # for date-valued fields (until, owner-edited) and tier

    def render(self) -> str:
        if self.dst_doc:
            tgt = f"[[{self.dst_doc}#^{self.dst_block}]]" if self.dst_block else f"[[{self.dst_doc}]]"
            return f"- {self.rel}:: {tgt}"
        return f"- {self.rel}:: {self.value}"


@dataclass
class Claim:
    block: str
    text: str
    edges: list[Edge] = field(default_factory=list)
    folded: bool = False
    lineno: int = 0
    minted: bool = False   # had no block id when read: an owner-typed line

    def targets(self, rel: str) -> list[tuple[str, str]]:
        return [(e.dst_doc, e.dst_block) for e in self.edges if e.rel == rel and e.dst_doc]

    def value(self, rel: str) -> str:
        for e in self.edges:
            if e.rel == rel and not e.dst_doc:
                return e.value
        return ""

    def has(self, rel: str) -> bool:
        return any(e.rel == rel for e in self.edges)

    @property
    def sha(self) -> str:
        return claim_sha(self.text)

    def render(self) -> str:
        lines = [f"{self.text} ^{self.block}"]
        # Group multi-target rels on one line, in a stable order.
        by_rel: dict[str, list[str]] = {}
        order: list[str] = []
        for e in self.edges:
            if e.rel not in by_rel:
                by_rel[e.rel] = []
                order.append(e.rel)
            if e.dst_doc:
                by_rel[e.rel].append(f"[[{e.dst_doc}#^{e.dst_block}]]" if e.dst_block else f"[[{e.dst_doc}]]")
            else:
                by_rel[e.rel].append(e.value)
        for rel in order:
            lines.append(f"- {rel}:: {', '.join(by_rel[rel])}")
        return "\n".join(lines)


def claim_sha(text: str) -> str:
    """Hash with wikilink targets canonicalised, so an Obsidian rename that
    rewrites `[[old]]` to `[[new]]` in every referrer changes no hash and
    pins nothing."""
    canon = WIKILINK_RE.sub("[[]]", text)
    return sha_text(" ".join(canon.split()))


@dataclass
class Note:
    path: Path
    meta: dict[str, Any]
    pre: str            # everything before the region (title etc.)
    claims: list[Claim]
    post: str           # everything after the region (## Notes, owner prose)
    had_region: bool
    raw: str = ""

    @property
    def kind(self) -> str:
        """'' for a curated note; 'redirect' / 'tombstone' for the stubs the
        retire ritual leaves, which no reader should treat as a live note."""
        return str(self.meta.get("kind") or "")

    @property
    def title(self) -> str:
        t = self.meta.get("title")
        if t:
            return str(t)
        m = re.search(r"^# (.+)$", self.pre, re.M)
        return m.group(1).strip() if m else self.path.stem

    def live(self) -> list[Claim]:
        return [c for c in self.claims if not c.folded]

    def get(self, block: str) -> Claim | None:
        return next((c for c in self.claims if c.block == block), None)

    def next_block(self) -> str:
        n = 0
        for c in self.claims:
            m = re.fullmatch(r"c(\d+)", c.block)
            if m:
                n = max(n, int(m.group(1)))
        return f"c{n + 1}"

    def render(self) -> str:
        body = self.pre.rstrip("\n") + "\n\n" + render_region(self.claims) + "\n" + self.post
        return render_frontmatter(self.meta) + body if self.meta else body


def parse_edges(value: str, rel: str) -> list[Edge]:
    out = []
    links = list(WIKILINK_RE.finditer(value))
    if links:
        for m in links:
            out.append(Edge(rel, m.group(1).strip(), (m.group(2) or "").strip()))
    else:
        for part in [p.strip() for p in value.split(",") if p.strip()]:
            out.append(Edge(rel, value=part))
    return out


def parse_region(lines: list[str], start_lineno: int = 1) -> list[Claim]:
    claims: list[Claim] = []
    folded = False
    cur: Claim | None = None
    pending_minted = 0
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if not line.strip():
            cur = None
            continue
        if line.startswith("## "):
            folded = line.strip().lower() == "## history"
            cur = None
            continue
        if line.startswith("<!--") or line.startswith(">"):
            continue  # comments and callouts are furniture, not claims
        m = EDGE_LINE_RE.match(line)
        if m and cur is not None and m.group("rel") in EDGE_RELS:
            cur.edges.extend(parse_edges(m.group("value"), m.group("rel")))
            continue
        m = CLAIM_LINE_RE.match(line)
        if m:
            cur = Claim(m.group("block"), m.group("text").strip(), folded=folded, lineno=start_lineno + i)
        else:
            # Prose the owner typed inside the region: a claim of theirs.
            pending_minted += 1
            cur = Claim(f"owner{pending_minted}", line.strip("- ").strip(), folded=folded,
                        lineno=start_lineno + i, minted=True)
        claims.append(cur)
    return claims


def parse_note(path: Path, text: str | None = None) -> Note:
    raw = path.read_text(encoding="utf-8") if text is None else text
    doc = parse_frontmatter(raw)
    body = doc.body
    b, e = body.find(BEGIN), body.find(END)
    if b < 0 or e < 0 or e < b:
        return Note(path, doc.meta, body.rstrip("\n") + "\n", [], "", False, raw)
    pre = body[:b]
    region = body[b + len(BEGIN):e]
    post = body[e + len(END):]
    start_lineno = raw[: raw.find(BEGIN)].count("\n") + 2
    claims = parse_region(region.splitlines(), start_lineno)
    # Re-id owner-typed lines so they become addressable on the next write.
    n = 0
    for c in claims:
        m = re.fullmatch(r"c(\d+)", c.block)
        if m:
            n = max(n, int(m.group(1)))
    for c in claims:
        if c.minted:
            n += 1
            c.block = f"c{n}"
    return Note(path, doc.meta, pre, claims, post.lstrip("\n"), True, raw)


def render_region(claims: list[Claim]) -> str:
    live = [c for c in claims if not c.folded]
    hist = [c for c in claims if c.folded]
    parts = [BEGIN, ""]
    for c in live:
        parts.append(c.render())
        parts.append("")
    if hist:
        parts.append("## History")
        parts.append("> [!note]- Folded claims, kept for provenance")
        parts.append("")
        for c in hist:
            parts.append(c.render())
            parts.append("")
    parts.append(END)
    return "\n".join(parts) + "\n"


def new_note(path: Path, subject_type: str, title: str, ids: list[str] | None = None,
             created: str = "", aliases: list[str] | None = None) -> Note:
    meta: dict[str, Any] = {"type": subject_type, "title": title}
    if aliases:
        meta["aliases"] = aliases
    if ids is not None:
        meta["ids"] = ids
    if created:
        meta["created"] = created
    pre = f"\n# {title}\n"
    post = "\n## Notes\nEverything below this line is yours; wanda reads it only when asked and never edits it.\n"
    return Note(path, meta, pre, [], post, True)


# --- write-specs -------------------------------------------------------------------

@dataclass
class WriteSpec:
    path: Path
    meta: dict[str, Any]
    prose: str
    index: list[str]

    def render(self) -> str:
        """Prose is preserved byte for byte — the cap applies where it is
        loaded (projection, walk), never where the owner's text is stored."""
        prose = self.prose.strip("\n")
        block = "\n".join([INDEX_BEGIN, *self.index, INDEX_END])
        meta = {"kind": "write-spec", **{k: v for k, v in self.meta.items() if k != "kind"}}
        return render_frontmatter(meta) + prose + "\n\n" + block + "\n"

    @property
    def sha(self) -> str:
        return sha_text(" ".join(self.prose.split()))


def parse_writespec(path: Path, text: str | None = None) -> WriteSpec:
    raw = path.read_text(encoding="utf-8") if text is None else text
    doc = parse_frontmatter(raw)
    body = doc.body
    b, e = body.find(INDEX_BEGIN), body.find(INDEX_END)
    if b >= 0 and e > b:
        prose = body[:b]
        index = [ln for ln in body[b + len(INDEX_BEGIN):e].splitlines() if ln.strip()]
    else:
        prose, index = body, []
    return WriteSpec(path, doc.meta, prose.strip("\n"), index)
