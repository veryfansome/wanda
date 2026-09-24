"""Judge a run's checkpoints by reading them, not by counting shared words.

Token overlap fails on answers that are correct: "never got a delivery
confirmation past its 17 January estimate" does not share enough words with
"shipped on the 14th expecting the 17th", and no threshold fixes that. It also cannot tell a distractor that leaked from one the
session surfaced deliberately, which is the difference between noise and
resolving a conflict.

Reads results.jsonl, writes a scored report. Running is separate from scoring
on purpose: a scoring change should never need the sessions run again.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# the store, built from the Rust crate. `lab/bin` is where the build puts it,
# beside the templates it reads the vault's standing texts from.
sys.path.insert(0, str(Path(__file__).resolve().parent / "bin"))
import corpus as C  # noqa: E402
import memory as S  # noqa: E402

ROOT = Path(__file__).resolve().parent
MODEL = "claude-sonnet-5"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["should", "should_not", "discipline"],
    "properties": {
        "should": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item", "verdict", "why"],
                "properties": {
                    "item": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["hit", "partial", "miss"]},
                    "why": {"type": "string", "description": "one short sentence"},
                },
            },
        },
        "should_not": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item", "verdict", "why"],
                "properties": {
                    "item": {"type": "string"},
                    "verdict": {"type": "string",
                                "enum": ["absent", "present-justified", "present-noise"]},
                    "why": {"type": "string", "description": "one short sentence"},
                },
            },
        },
        "discipline": {
            "type": "object",
            "additionalProperties": False,
            "required": ["verdict", "why"],
            "properties": {
                "verdict": {"type": "string", "enum": ["within", "buried", "nothing"]},
                "why": {"type": "string", "description": "one short sentence"},
            },
        },
    },
}

SYSTEM = """You are scoring what an assistant recalled in a situation, against what it
was supposed to recall.

For each `should`: hit if the assistant brought that thing to bear, in the
recalled items or in its answer. Different wording is fine; what matters is
whether the substance is there. "past its 17 January estimate" satisfies
"expecting the 17th". Partial if it is nearly there or hedged. Miss if the
assistant plainly did not have it.

Read each `should` for what it asks of the assistant, because two kinds are
mixed together. Most ask it to HAVE something — "the January lunch at Tony's",
"that Jane is fan's cousin" — and a recalled item carrying that substance is a
hit whether or not the answer repeats it. Some ask it to SAY something, and say
it a particular way: "as candidates rather than one answer", "put to fan here,
without his having asked", "told about it here, unprompted". Those are claims
about what the assistant volunteered. If the answer is empty, nothing was
volunteered, so a say-shaped `should` is a miss no matter what was retrieved —
retrieving both pizza places and then saying nothing is not offering the user a
choice. Do not read an empty answer as a formatting accident to be excused; on
some checkpoints staying silent is exactly the behaviour under test.

Each recalled item is shown with its body and its live edges — its
relationships to other nodes — because those are the node's own content: a
person node can hold "cousin_of → person:fan" and nothing else, and that is
the fact. So an edge on a recalled node can satisfy a `should`.

It cannot fail a `should not`. An entity that appears only as an edge target
on a recalled node — "coworker_of → person:priya" on fan's node, when the
session recalled fan — was not surfaced by the session; the session surfaced
fan. Judge every `should not` against what the session put in its recalled
list and its answer, never against what those items are linked to.

For each `should not`: these are distractors that should not crowd out the
answer. Absent if it did not come up. Present-justified if it did come up and
the answer shows a reason: naming a conflict in order to resolve it is not a
leak. Present-noise if it came up for no reason and took space.

Then judge discipline, which is a different question from both of the above and
must not be confused with either. The recalled items are numbered in the order
the assistant chose them, and the checkpoint carries a budget: roughly how many
items can come first before what matters is buried. A thing can be perfectly
fair to raise and still be in the way.

  within   everything the checkpoint asked for arrived at or before the budget
           position, or in the answer itself. Items after it do not matter;
           what matters is that the useful material came first.
  buried   the assistant had what was asked for but put it later than the
           budget, behind items that were not asked for.
  nothing  none of what was asked for came back at all, so there is no ordering
           to judge. The `should` verdicts already record that.

Some checkpoints come with what was said earlier in the same scene, including
what the assistant itself answered then. A `should` about the assistant's own
earlier words — "the title it suggested", "that it undertook to remind her" —
is judged against that record: the substance of what it said then has to be
what it brings to bear now. The assistant scoring here did not see that
record; it had only its memory.

Judge only what is in front of you. Be strict about substance and relaxed about
wording."""


# Claude Code instructs every session, headless included, to keep a memory of
# its own in a directory that is not the store — a second memory system beside
# the one being scored
NO_AUTO_MEMORY = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def materialise(snaps: Path, sha: str, dest: Path) -> None:
    """The store as it stood at one snapshot, somewhere that can be written to.

    Never the original vault: what is scored is the store a checkpoint actually
    met, and the store it starts from has to be identical every time. The
    generated CLAUDE.md files come back as that session was shown them, because
    the landing surface is half of what findability means."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    tar = subprocess.run(["git", "archive", sha], env={"GIT_DIR": str(snaps)},
                         capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar, check=True)
    # its own repo, or whatever reads it inherits this project's CLAUDE.md
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)


def isolated() -> Path:
    """Claude Code scopes CLAUDE.md and memory to the enclosing git repo, so a
    judge invoked from the repo root reads this project's notes about the very
    experiment it is scoring. Its own repo, outside the tree, is the fix."""
    d = Path(tempfile.gettempdir()) / "lab-judge-cwd"
    if not (d / ".git").exists():
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    shutil.rmtree(Path.home() / ".claude" / "projects" /
                  str(d.resolve()).replace("/", "-") / "memory", ignore_errors=True)
    return d


# a runaway-body guard, not a token budget: no node in a seven-month store
# comes near it, and the whole of one costs the judge a couple of hundred
# tokens. Set where it does not bite on real bodies, so that when it does bite
# the node is genuinely out of hand.
BODY_CAP = 1600


def clip_body(body: str, cap: int = BODY_CAP) -> str:
    """A body, within the budget, cut on its own lines.

    A body is one line per update, appended: the first says what the node is,
    the last says where it stands. Taking the first `cap` characters keeps the
    oldest text and drops the newest, so a node that grew loses its current
    state; cutting mid-word can leave a fragment that says the opposite of the
    line it came from. Keep the first line and the newest lines that fit,
    filling backwards with any older line that still fits, and mark each gap."""
    GAP = "[…]"
    lines = [" ".join(l.split()) for l in body.splitlines() if l.strip()]
    if not lines:
        return ""
    whole = " ".join(lines)
    if len(whole) <= cap:
        return whole
    if cap <= len(GAP) + 2:
        return whole[:cap]

    def render(keep: set) -> str:
        out, prev = [], None
        for i in sorted(keep):
            if prev is not None and i != prev + 1:
                out.append(GAP)
            out.append(lines[i])
            prev = i
        if prev is not None and prev != len(lines) - 1:
            out.append(GAP)
        return " ".join(out)

    # the first line, then every line from the newest backwards that still
    # fits — a line too long to fit is skipped, not a reason to stop
    keep = {0}
    for i in range(len(lines) - 1, 0, -1):
        if len(render(keep | {i})) <= cap:
            keep.add(i)
    if keep != {0}:
        return render(keep)
    # nothing else fits whole: the newest line is where the node stands, so
    # show as much of its tail as the budget allows, cut at a word
    room = cap - len(lines[0]) - len(GAP) - 2
    if room <= 0:
        return whole[:cap]
    tail = lines[-1][-room:]
    # drop a leading partial word, unless that is most of what we have
    trimmed = tail.split(" ", 1)[-1] if " " in tail else tail
    return f"{lines[0]} {GAP} {trimmed if len(trimmed) > room // 2 else tail}"


def _lookup(vault: S.Vault, ref: str) -> str | None:
    try:
        return vault.resolve(ref)
    except S.Ambiguous:
        # a name two nodes share tells the judge nothing about which one the
        # session meant
        return None


ID_IN_REF = re.compile(r"(?<![0-9a-z])((?:\d{4}-\d{2}-\d{2}-)?[0-9a-f]{6})(?![0-9a-z])", re.I)


def resolve_ref(vault: S.Vault | None, ref: str) -> list[str]:
    """Every node a recalled ref names, in the order it names them.

    A session writes its recalled list for a reader as well as for a tool, so
    one ref can run a name on after an id, spell the kind as its directory or
    as the verb that writes it, or list several events with their date given
    once for all of them. Every id in it is looked up, whatever kind is written
    before it, and only a ref that names no node by id is looked up whole, as a
    name."""
    if vault is None:
        return []
    found: list[str] = []
    day = by_tail = None
    for m in ID_IN_REF.finditer(ref):
        written = m[1].lower()
        # no id is minted without a digit; six hex letters are a word
        if not any(c.isdigit() for c in written[-6:]):
            continue
        if len(written) > 6:
            day = written[:11]
        tries = ([day + written] if day and len(written) == 6 else []) + [written]
        nid = next((n for n in map(lambda t: _lookup(vault, t), tries) if n), None)
        if nid is None:
            # an event's id written without its date, or after another day's.
            # An id is unique with its date, so the six characters alone can
            # name two events, and then they name neither
            if by_tail is None:
                by_tail = {}
                for n, _, _ in vault.nodes():
                    by_tail.setdefault(n[-6:], []).append(n)
            hits = by_tail.get(written[-6:], [])
            nid = hits[0] if len(hits) == 1 else None
        if nid and nid not in found:
            found.append(nid)
    if not found:
        nid = _lookup(vault, ref)
        found = [nid] if nid else []
    return found


def has_sha(snaps: Path, sha: str) -> bool:
    """Found is not the same as usable: a repo can be present and the commit
    absent, which would otherwise surface as a crash mid-pass."""
    return bool(sha) and subprocess.run(
        ["git", "--git-dir", str(snaps), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True).returncode == 0


def snaps_for(rec: dict, results: Path, flag: str) -> Path | None:
    """The repo holding the store this record's checkpoint met.

    The operator's flag is tried first, then the repo the record names, then
    the one beside the results file. A record names its repo by basename,
    because records are written inside the container and read on the host and
    an absolute path would not survive that; the name is looked for beside the
    results file and beside the lab, since a replay writes its output wherever
    it was told to. `store` is what a replay record already carries, so replay
    files already on disk work.

    A candidate that does not hold *this record's* commit is not found — a
    wrong `--snaps`, or a stale repo under the right name, must not quietly
    send the whole pass to the fallback vault."""
    names = [flag] if flag else []
    for key in ("snaps", "store"):
        if rec.get(key):
            names.append(rec[key].replace("-results", "-snaps.git"))
    names.append(results.name.replace("-results.jsonl", "-snaps.git"))
    for n in names:
        cand = Path(n)
        roots = [Path("")] if cand.is_absolute() else [results.resolve().parent, ROOT]
        for root in roots:
            # a bare repo is a directory; a file of the same name is not one
            if (root / cand).is_dir() and has_sha(root / cand, rec.get("snapshot") or ""):
                return root / cand
    return None


def judge_one(rec: dict, vault: S.Vault, timeout_s: int = 120) -> tuple[dict, int]:
    lines, unresolved = [], 0
    # an opaque id says nothing to the judge; every id it sees is rendered
    # with the node's name, in the recalled line and in each edge target, so
    # that a relationship held in an edge can satisfy a `should`
    names = {nid: S.label(meta) for nid, meta, _ in vault.nodes()}

    def named(nid: str) -> str:
        n = names.get(nid, "")
        return f"{n} ({nid})" if n and n.lower() not in nid.lower() else nid

    def rendered(nid: str) -> str:
        p = vault.path_for(nid)
        if not p.exists():
            return ""
        meta, b = S.fm_load(p.read_text(), vault.root)
        # a guard against a runaway body, not a display choice: what the judge
        # cannot see, it scores as absent. Retracted lines are gone from it,
        # because they are no longer true. The summary is the node's own
        # content too, and comes first.
        body = clip_body(S.live_body(b))
        summary = S.one_line(meta.get("summary", ""))
        if summary and summary.lower() != S.label(meta).lower():
            # a migrated summary is the body's first line, clipped; once is
            # enough
            if body.lower().startswith(summary.rstrip("…").lower()):
                pass
            else:
                body = summary + (" | " + body if body else "")
        # and the edges, for the same reason: a relationship lives in an edge,
        # so a person node can hold the whole fact and have an empty body.
        # Without them the judge scores as a miss what the store plainly holds.
        live = [f"{e.get('rel')} → {named(e.get('to', ''))}" for e in meta.get("edges", [])
                if e.get("rel")]
        if live:
            body = (body + " | " if body else "") + "edges: " + "; ".join(live[:12])
        return body

    for i, ref in enumerate(rec.get("recalled", []), 1):
        nids = resolve_ref(vault, ref)
        if not nids:
            unresolved += 1
        # numbered, because the order a session put them in is what it thought
        # mattered most, and a budget is about what came first
        if len(nids) <= 1:
            nid = nids[0] if nids else None
            body = rendered(nid) if nid else ""
            shown = ref if not nid or names.get(nid, "").lower() in ref.lower() else f"{ref} = {names[nid]}"
            lines.append(f"{i}. {shown}" + (f" — {body}" if body else ""))
            continue
        # still one item, since the budget counts what the session listed
        lines.append(f"{i}. {ref}")
        for nid in nids:
            body = rendered(nid)
            lines.append(f"   {named(nid)}" + (f" — {body}" if body else ""))
    # what the assistant itself said earlier in the scene. A `should` that
    # names "the title she suggested on the 21st" is unscoreable without it,
    # and the store may hold nothing of it — that is what such a scene tests
    earlier = ""
    for pr in rec.get("prior") or []:
        earlier += (f"    {pr['date']}, from {pr['speaker']}: {pr['text']}\n"
                    f"    the assistant answered: {pr['answer'] or '(nothing)'}\n")
    prompt = (
        f"Situation ({rec['date']}, from {rec['speaker']}):\n    {rec['text']}\n\n"
        + (f"Earlier in this scene, in order:\n{earlier}\n" if earlier else "")
        + f"What the assistant recalled, in its own order:\n" + ("\n".join(lines) or "  (nothing)") +
        f"\n\nWhat it answered:\n    " + (rec.get("answer") or
         "(nothing — the assistant volunteered no answer at all)") + "\n\n"
        f"should:\n" + "\n".join(f"- {s}" for s in rec["should"]) +
        (f"\n\nshould not:\n" + "\n".join(f"- {s}" for s in rec["should_not"])
         if rec["should_not"] else "") +
        f"\n\nbudget: {rec['budget']} items before what matters is buried"
    )
    argv = ["claude", "-p", "--output-format", "json", "--model", MODEL,
            "--json-schema", json.dumps(SCHEMA), "--system-prompt", SYSTEM,
            "--no-session-persistence", "--tools", ""]
    # judged from its own repo, so the scorer does not inherit this project's
    # CLAUDE.md and memory index along with the thing it is scoring
    p = subprocess.run(argv, input=prompt.encode(), capture_output=True,
                       timeout=timeout_s, cwd=str(isolated()),
                       env={**os.environ, **NO_AUTO_MEMORY})
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:300])
    body = json.loads(p.stdout.decode()).get("result") or "{}"
    return (json.loads(body) if isinstance(body, str) else body), unresolved


def with_expectations(recs: list[dict], paths: list[Path]) -> tuple[list[dict], list[dict]]:
    """What each checkpoint was supposed to bring back.

    A run does not carry it: the harness is given arrivals and nothing else, so
    the expectations are joined back here, from the history, shifted to the
    anchor that run recorded — an expectation names dates and they moved with
    it. The join is on the arrival's position, because a scene name is shared
    by several arrivals and a date moves. Records written before the split
    carry their own and are left alone.

    The arrival comes back the same way, and for a second reason: a run writes
    its records into its own directory, and that directory is mounted into the
    container its sessions work in. What a session must not read is the
    history, so the history is not written there."""
    cache: dict[str, dict] = {}
    joined, lost = [], []
    for rec in recs:
        if "should" in rec:
            joined.append(rec)
            continue
        stamp, iid = rec.get("anchor"), rec.get("input_id")
        if not (stamp and iid):
            lost.append(rec)
            continue
        # the position names an arrival in one particular history. Scoring
        # against a different one matches every record to the wrong
        # expectations and reports nothing amiss, so a record that cannot say
        # which history it came from is refused rather than joined.
        if rec.get("history") != C.digest(paths):
            lost.append(rec)
            continue
        if stamp not in cache:
            cache[stamp] = C.expectations(paths, date.fromisoformat(stamp))
        inp = cache[stamp].get(iid)
        if inp is None:
            lost.append(rec)
            continue
        # a prior entry is a position and what wanda answered; the arrival it
        # answered comes back from the history like everything else
        earlier = []
        for pr in rec.get("prior") or []:
            was = cache[stamp].get(pr.get("input_id"))
            earlier.append(pr if "text" in pr or was is None else
                           {"date": was.date, "channel": was.channel,
                            "speaker": was.speaker, "text": was.text,
                            "answer": pr.get("answer") or ""})
        arrival = {"scene": inp.scene, "date": inp.date, "channel": inp.channel,
                   "speaker": inp.speaker, "text": inp.text}
        # rec wins wherever it has a value of its own, so a record written
        # before the split is left as it was
        joined.append({**arrival, **rec, "prior": earlier,
                       "should": inp.should, "should_not": inp.should_not,
                       "budget": inp.budget})
    return joined, lost


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT.parent / "runs" / "results.jsonl"))
    ap.add_argument("--vault", default=str(ROOT.parent / "runs" / "vault"),
                    help="the fallback store, for a record whose snapshot cannot be found")
    ap.add_argument("--snaps", default="",
                    help="the snapshot repo, when it is not beside the results file")
    ap.add_argument("--out", default=str(ROOT.parent / "runs" / "scored.md"))
    ap.add_argument("--corpus", nargs="+",
                    default=[str(ROOT.parent / "docs" / "recall-corpus.md")],
                    help="where the expectations come from, for results that do not carry them")
    args = ap.parse_args()

    results = Path(args.results)
    recs = [json.loads(l) for l in results.read_text().splitlines() if l.strip()]
    recs, lost = with_expectations(recs, [Path(c) for c in args.corpus])
    if lost:
        # a partial tally reads as a whole one. Whatever is wrong — the wrong
        # history, a record from before positions were stamped — is the
        # operator's to fix, not something to score around
        for rec in lost[:5]:
            print(f"no expectations for "
                  f"{(rec.get('scene') or f'arrival {rec.get('input_id')}')[:44]} "
                  f"(id {rec.get('input_id')}, anchor {rec.get('anchor')})", file=sys.stderr)
        print(f"{len(lost)} of {len(lost) + len(recs)} records have no expectations; "
              f"is --corpus the history this run was made from?", file=sys.stderr)
        return 2
    # a checkpoint is scored against the store it met, materialised from the
    # snapshot the run took before that session. Scoring against the store as
    # it ended reads later sessions' writes back into the checkpoint.
    # removed on every exit path, not only the happy one: an interrupt during a
    # model call would otherwise leave a whole materialised store behind
    with tempfile.TemporaryDirectory(prefix="lab-judge-") as tmp:
        return score(recs, results, args, Path(tmp))


def score(recs: list, results: Path, args, workdir: Path) -> int:
    # S.Vault on a missing directory does not raise: nodes() is empty and every
    # ref renders as a bare id, so an absent fallback would score a whole pass
    # against nothing and call it done
    fallback = S.Vault(Path(args.vault)) if Path(args.vault).is_dir() else None
    if fallback is not None and not fallback.nodes():
        fallback = None
    out = ["# Scored report", "", "Judged by reading, not by word overlap.", ""]
    tally = {"hit": 0, "partial": 0, "miss": 0,
             "absent": 0, "present-justified": 0, "present-noise": 0,
             "within": 0, "buried": 0, "nothing": 0}
    skipped, refs, unresolved_total, over_budget = [], 0, 0, 0
    fallback_used, unresolved_refs = 0, []

    def write_report() -> None:
        """Written as the pass goes, so an interrupted or crashed run keeps the
        judgements it has already paid for."""
        Path(args.out).write_text("\n".join(out) + "\n")

    for rec in recs:
        if rec.get("placeholder") or rec.get("error"):
            # the session filled the schema with scaffolding, or died before
            # reporting at all, having possibly done every part of the work.
            # Scoring an empty record counts a report failure as a recall
            # failure, so it is not scored: it is named, and the denominator
            # says so.
            skipped.append(rec["scene"])
            why = (f"placeholder output in {rec['placeholder']}" if rec.get("placeholder")
                   else f"session error: {rec['error'][:120]}")
            out += [f"## {rec['scene']}", "", f"  UNSCORED — {why}; the session's report is not evidence", ""]
            continue
        snaps = snaps_for(rec, results, args.snaps)
        sha = rec.get("snapshot") or ""
        store, against = None, ""
        if snaps:
            store, against = workdir / "store", f"snapshot {sha[:8]}"
        elif fallback is not None:
            store, against = None, "the fallback vault"
        if store is None and against == "":
            # neither the store this met nor a usable fallback: scoring it
            # would be scoring against a store we cannot name
            skipped.append(rec["scene"])
            why = (f"no snapshot for this checkpoint"
                   f"{' and no usable --vault' if fallback is None else ''}; "
                   f"the store it met is unknown")
            out += [f"## {rec['scene']}", "", f"  UNSCORED — {why}", ""]
            print(f"unscored {rec['scene'][:40]}: {why}", file=sys.stderr)
            write_report()
            continue
        try:
            if store is not None:
                materialise(snaps, sha, store)
                v, unresolved = judge_one(rec, S.Vault(store))
            else:
                fallback_used += 1
                v, unresolved = judge_one(rec, fallback)
        except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError,
                subprocess.CalledProcessError, OSError) as e:
            # one slow or failed call must not abort the pass and leave an
            # empty file. The checkpoint is unscored, named, and the pass
            # goes on.
            skipped.append(rec["scene"])
            what = "the store could not be materialised" if isinstance(
                e, (subprocess.CalledProcessError, OSError)) else "the judge call failed"
            out += [f"## {rec['scene']}", "", f"  UNSCORED — {what}: "
                    f"{type(e).__name__}: {str(e)[:120]}; the checkpoint is not evidence "
                    f"either way", ""]
            print(f"unscored {rec['scene'][:40]}: {what}, {type(e).__name__}", file=sys.stderr)
            write_report()
            continue
        unresolved_refs += [r for r in rec.get("recalled", [])
                            if not resolve_ref(S.Vault(store) if store is not None else fallback, r)]
        refs += len(rec.get("recalled", []))
        over_budget += len(rec.get("recalled", [])) > rec["budget"]
        unresolved_total += unresolved
        out += [f"## {rec['scene']}", "",
                f"`{rec['date']} | {rec['speaker']} | {rec['text']}`", "",
                f"recalled {len(rec.get('recalled', []))} (budget {rec['budget']}) · {against}"
                + ("" if (rec.get("answer") or "").strip() else " · ANSWER EMPTY")
                + (f" · {rec['trace']}" if rec.get("trace") else ""), ""]
        for r in v.get("should", []):
            tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
            out.append(f"  should      {r['verdict']:8} {r['item']}")
            out.append(f"                       {r['why']}")
        for r in v.get("should_not", []):
            tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
            out.append(f"  should not  {r['verdict']:18} {r['item']}")
            out.append(f"                                 {r['why']}")
        d = v.get("discipline") or {}
        if d.get("verdict"):
            tally[d["verdict"]] = tally.get(d["verdict"], 0) + 1
            out.append(f"  discipline  {d['verdict']:18} budget {rec['budget']}")
            out.append(f"                                 {d.get('why', '')}")
        out += ["", f"  answer: {rec.get('answer') or ''}", ""]
        write_report()
        print(f"judged {rec['scene'][:40]}", file=sys.stderr)

    scored = len(recs) - len(skipped)
    tally["checkpoints_scored"] = scored
    # burial and length are both discipline and only one needs a model: whether
    # the useful part came first is a reading, whether too much came back is
    # arithmetic. They are different failures and want separate numbers.
    tally["over_budget"] = f"{over_budget}/{scored}"
    tally["checkpoints_unscored"] = len(skipped)
    # an unresolved ref means the judge scored that checkpoint without the
    # node's body, which biases toward miss. The rate alone hides a change —
    # it does not start at zero — so the refs themselves are named.
    tally["refs_unresolved"] = f"{unresolved_total}/{refs}"
    # which store each checkpoint was scored against. A pass mixing snapshots
    # with a fallback is mixing two measurements in one set of numbers.
    tally["scored_against_fallback_vault"] = fallback_used
    out += ["## Tally", "", json.dumps(tally), ""]
    if skipped:
        out += [f"unscored: {', '.join(skipped)}", ""]
    if unresolved_refs:
        out += ["refs that resolve against no node:", ""]
        out += [f"  {r}" for r in sorted(set(unresolved_refs))] + [""]
    if fallback_used:
        out += [f"**{fallback_used} of {scored} checkpoints were scored against the fallback "
                f"vault rather than the store they met.** Those verdicts are not comparable "
                f"with the rest.", ""]
    write_report()
    print(json.dumps(tally))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
