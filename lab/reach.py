"""What reached a session: which indexes it was shown, and how much of each node.

    python3 lab/reach.py runs/17A [runs/17B ...]

Writes one record per session to `<run>/report<run>-reach.jsonl`. Nothing here
runs a session or calls a model — it reads what the sessions were already shown,
so it can be run over a finished round as many times as you like, and over a
round finished months ago.

Read from the transcripts rather than from the run's own tool log. That log
keeps the first 300 characters of every result and the first 400 of every
command, which is most of a node file gone; a measurement of what arrived cannot
be taken from a record that drops it.

Route does not matter here, and nothing here tries to infer one. An index counts
when a line of its listing is in the context, whether the session opened it,
`tail`ed it, or was handed it by Claude Code for reading a node in that
directory. A node counts as read whole when its file text is in the context,
whether that came from `mem show`, from `Read`, or from a `cat prefs/*.md` that
named no file at all. What the measurement separates is not how something
arrived but how much of it did: the whole node, or a line that names it.

Asking whether the text is there rather than which command fetched it is what
makes that true. A rule written per route has to guess — `head -3 people/x.md`
names the file and delivers a tenth of it, and a directory-wide `cat` delivers
everything and names nothing. The text either arrived or it did not.

A node is only counted if it was in the store when the session began. A session
sees the ids it writes come back to it, and the enrich skill often shows a new
node to check its edges; counting those would say a session reached a memory
that did not exist when it arrived.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# the nine kinds, as the store spells them in an id and as it spells them in a
# directory name — `preference:8f21ac` lives in `prefs/8f21ac.md`
KIND_DIR = {
    "person": "people", "place": "places", "org": "orgs", "group": "groups",
    "thing": "things", "topic": "topics", "event": "events",
    "preference": "prefs", "trajectory": "trajectories",
}
DIR_KIND = {d: k for k, d in KIND_DIR.items()}

LOCAL = r"(?:[0-9a-f]{6}|\d{4}-\d{2}-\d{2}-[0-9a-f]{6})"
NODE_ID = re.compile(rf"\b({'|'.join(KIND_DIR)}):({LOCAL})\b")
NODE_PATH = re.compile(rf"\b({'|'.join(DIR_KIND)})/({LOCAL})\.md\b")
# an index spells a node `939068` and a node's own edges spell it [[939068]] —
# the kind is the directory the line is in, so neither writes it out. The local
# part alone identifies the node: no two in any store this lab has built share
# one. Delimited, because six hex digits are six hex digits.
STEM = re.compile(rf"(?:`({LOCAL})`|\[\[({LOCAL})\]\])")
# one line of a generated index: `- ``939068``  Sarah — another parent …`. The
# index is counted from its listing and not from its `# people / 10 here.`
# header, because the header is not the thing — a session handed those three
# lines and nothing else has been told a number, not shown a store.
INDEX_LINE = re.compile(rf"^-\s+`({LOCAL})`\s", re.M)
# a bare filename, which is what `ls people/` returns
BARE_FILE = re.compile(rf"(?<![\w/])({LOCAL})\.md\b")
# the Read tool numbers the lines it returns, so the file's own first column is
# not where the text begins
READ_GUTTER = re.compile(r"^\s*\d+\t", re.M)
# whitespace is not content, and it does not survive the trip intact: a node's
# trailing blank lines are gone by the time it arrives, and a session that pipes
# a directory through `grep -v '^$'` strips the blank line inside it too
SPACE = re.compile(r"\s+")


def records(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def arrived(recs: list[dict]) -> list[str]:
    """Every text that entered the session's context.

    A tool result arrives as a string or as a list of blocks; an attachment
    arrives with no tool call at all, which is how a directory's index reaches a
    session that only read a node in that directory."""
    texts: list[str] = []
    for d in recs:
        att = d.get("attachment") or {}
        if att:
            inner = att.get("content")
            if isinstance(inner, dict):
                inner = inner.get("content")
            if isinstance(inner, str):
                texts.append(inner)
            if att.get("displayPath"):
                texts.append(str(att["displayPath"]))
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            # the arrival itself, which is the one user turn. It is what the
            # session was asked, not anything it was given from the store.
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, str):
                    texts.append(c)
                elif isinstance(c, list):
                    texts.extend(x.get("text", "") for x in c if isinstance(x, dict))
            elif b.get("type") == "text":
                # what the session said. It carries node ids it is working from,
                # and those are ids it had already been given rather than new
                # arrivals, so this is deliberately not counted as reach.
                continue
    return texts


def summary_line(text: str) -> str:
    """A line to look for before looking for the whole file.

    Almost every node carries a summary; six of the 2,094 in rounds 16 and 17
    do not, and those skip the filter rather than falling back to the head of
    the file — a short file's head is the file, trailing newlines and all, and
    those do not survive the trip, so the filter would veto what it is there to
    speed up. Two nodes could share a summary, which costs a wasted full-text
    test and nothing else: the full text is what decides."""
    for l in text.splitlines():
        if l.startswith("summary:"):
            return l
    return ""


def reach(recs: list[dict], had: dict[str, str]) -> dict:
    """Which indexes arrived, and how much of each node, given the store it met."""
    texts = arrived(recs)
    flat = READ_GUTTER.sub("", "\n".join(texts))
    flat_ws = SPACE.sub(" ", flat)

    kind_of = {i.split(":", 1)[1]: i.split(":", 1)[0] for i in had}
    indexes = {KIND_DIR[kind_of[st]] for st in INDEX_LINE.findall(flat) if st in kind_of}

    # the whole file is in the context, compared with whitespace flattened on
    # both sides. Checking the summary first is only a filter: it is one line of
    # the file, so a node that fails it cannot pass, and it keeps the long test
    # off the great majority that never arrived at all.
    whole = {i for i, text in had.items()
             if text.strip() and SPACE.sub(" ", summary_line(text)).strip() in flat_ws
             and SPACE.sub(" ", text).strip() in flat_ws}

    # named. An index line and a recall hit carry the summary with the id; the
    # backlinks `mem show` prints carry the id and the edge and nothing else.
    # The band is the chance to read the node, not a reading of it.
    line = {f"{k}:{v}" for k, v in NODE_ID.findall(flat)}
    for d, stem in NODE_PATH.findall(flat):
        line.add(f"{DIR_KIND[d]}:{stem}")
    for a, b in STEM.findall(flat):
        st = a or b
        if st in kind_of:
            line.add(f"{kind_of[st]}:{st}")
    for st in BARE_FILE.findall(flat):
        if st in kind_of:
            line.add(f"{kind_of[st]}:{st}")
    line |= whole

    return {
        "indexes": sorted(indexes),
        "whole": sorted(whole),
        "line": sorted(line & set(had)),
        "new": sorted(line - set(had)),
    }


def store_before(run: Path, stem: str) -> dict[int, dict[str, str]]:
    """The store as each session met it: every node's id and its text.

    The snapshot repo holds one commit per session, taken before it ran, which
    is the store that session was given. Blobs are fetched once each — a node
    that no session touched is one blob across all 141 commits."""
    git = run / f"{stem}-snaps.git"
    if not git.is_dir():
        return {}

    def g(*args: str) -> str:
        return subprocess.run(["git", "--git-dir", str(git), *args],
                              capture_output=True, text=True).stdout

    trees: dict[int, list[tuple[str, str]]] = {}
    for entry in g("log", "--format=%H %s").splitlines():
        sha, _, subject = entry.partition(" ")
        # `before 041`, and `before 041|2026-03-02|fan` from a run made before
        # the key stopped carrying the arrival
        tail = subject.removeprefix("before ").split("|")[0].strip()
        if not tail.isdigit():
            continue
        nodes = []
        for l in g("ls-tree", "-r", sha).splitlines():
            meta, _, path = l.partition("\t")
            m = NODE_PATH.search(path)
            if m:
                nodes.append((f"{DIR_KIND[m.group(1)]}:{m.group(2)}", meta.split()[2]))
        trees[int(tail)] = nodes

    want = sorted({blob for ns in trees.values() for _, blob in ns})
    text: dict[str, str] = {}
    if want:
        # bytes throughout: `cat-file --batch` gives each blob's length in
        # bytes, and the store's prose is full of em dashes, so counting the
        # header's number in characters walks off the end of every node that
        # holds one and takes the head of the next with it.
        out = subprocess.run(["git", "--git-dir", str(git), "cat-file", "--batch"],
                             input="\n".join(want).encode(), capture_output=True).stdout
        pos = 0
        for blob in want:
            head, _, _ = out[pos:].partition(b"\n")
            pos += len(head) + 1
            n = int(head.split()[2])
            text[blob] = out[pos:pos + n].decode(errors="replace")
            pos += n + 1
    return {k: {i: text[b] for i, b in ns} for k, ns in trees.items()}


def sessions(run: Path) -> dict[str, Path]:
    """Each session's transcript, by the id the run recorded for it."""
    out = {}
    for p in (run / "transcripts").rglob("*.jsonl"):
        out[p.stem] = p
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="what reached each session of a run")
    ap.add_argument("runs", nargs="+", help="run directories, e.g. runs/17A")
    args = ap.parse_args()

    for r in args.runs:
        run = Path(r).resolve()
        stem = next((p.name[: -len("-sessions.jsonl")]
                     for p in run.glob("*-sessions.jsonl")), "")
        if not stem:
            print(f"{run} holds no sessions log", file=sys.stderr)
            return 2
        tx = sessions(run)
        live = store_before(run, stem)
        if not live:
            print(f"{run} has no snapshot repo, so nothing says what its "
                  f"sessions were given", file=sys.stderr)
            return 2
        if not tx:
            print(f"{run} kept no transcripts, so nothing says what arrived",
                  file=sys.stderr)
            return 2
        logged = [json.loads(l) for l in
                  (run / f"{stem}-sessions.jsonl").read_text().splitlines()]

        # a session whose CLI call failed is logged with no session id, and its
        # transcript is on disk with nothing pointing at it. When there is one
        # of each they are each other — the run starts one session per arrival,
        # so nothing else could have written it. Two of either and the pairing
        # is a guess, so it is not made.
        blank = [s for s in logged if not s.get("session")]
        loose = sorted(set(tx) - {s.get("session") for s in logged})
        paired = ""
        if len(blank) == 1 and len(loose) == 1:
            blank[0]["session"] = loose[0]
            paired = (f", arrival {blank[0]['input_id']} paired with the one "
                      f"transcript nothing claimed")

        recs, missing = [], 0
        for s in logged:
            p = tx.get(s.get("session"))
            if p is None:
                missing += 1
                continue
            if s["input_id"] not in live:
                print(f"{run}: no snapshot for arrival {s['input_id']}, so "
                      f"nothing says which store it met", file=sys.stderr)
                return 2
            recs.append({"input_id": s["input_id"], "session": s["session"],
                         **reach(records(p), live[s["input_id"]])})

        # written whole, and only once it is whole: a run left half-measured
        # reads exactly like a run measured to the end.
        out = run / f"{stem}-reach.jsonl"
        out.write_text("".join(json.dumps(r) + "\n" for r in recs))
        n = len(recs)
        gone = f", {missing} with no transcript kept" if missing else ""
        print(f"{out.name}: {n} sessions{gone}{paired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
