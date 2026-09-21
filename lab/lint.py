"""Check each checkpoint against the store its own history would build.

A scene is a fiction of the document. The corpus is one cumulative store and a
checkpoint sees everything said before it, so an expectation written while
thinking about one scene is really a claim about the whole history to that date.
Adding a scene can quietly break an older one, through an entity they share.

The tempting fix is to guard round-to-round comparability, asking whether a new
scene changes an old scene's verdict. That gets harder every round, needs a
judgment call per pair, and protects a comparison that was never sound anyway:
the store differs at every checkpoint once anything upstream changes. This asks
a different and cheaper question — is each scene still valid *on its own terms*,
against the history that actually precedes it. That scales with the number of
scenes rather than with their square, and it is the question that catches a
distractor which has quietly become a reasonable thing to say.

    lab/lint.py --corpus docs/recall-corpus.md

Several files are read as one corpus, which is the point: it answers "if I take
all fifteen new scenes, what breaks" before anything runs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus as C  # noqa: E402

ROOT = Path(__file__).resolve().parent
MODEL = "claude-sonnet-5"

# a line that meant to be an input or an expectation and was not parsed as one.
# corpus.py skips whatever does not match, without an error, so a typo deletes a
# checkpoint rather than failing the run. This is the one defect that cannot be
# caught by reading the parsed result, because the parsed result looks fine.
LOOKS_LIKE_INPUT = re.compile(r"^[-?]\s+\d{4}")
LOOKS_LIKE_EXPECT = re.compile(r"^\s+(should not|should|budget)\s*:")

COHERENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "lines", "what", "confidence"],
                "properties": {
                    "kind": {"type": "string", "enum": [
                        "contradiction", "impossible-timing", "physically-impossible",
                        "reference-before-fact", "implausible"]},
                    "lines": {"type": "array", "items": {"type": "string"},
                              "description": "the dated lines involved, quoted"},
                    "what": {"type": "string", "description": "one sentence"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
    },
}

COHERENCE_SYSTEM = """You are reading a scripted history for internal
consistency. It is fiction about a household, written in pieces by different
hands over time and sharing one cast, so pieces written separately can end up
disagreeing with each other.

Every line on the `dm` channel is a message *to wanda*, the assistant, from the
speaker named. "You" and "your" in a dm mean wanda. Fan and mei both talk to
her; they are not talking to each other in these lines. Lines on the `email`
channel are mail that arrived in someone's mailbox, and the mailbox is named
at the end of the line. Lines on a `thread:<name>` channel are messages in one
Slack thread that everyone in it reads, wanda included: there fan and mei
*are* talking to each other, and to her. Your job is to find where the story
contradicts itself, not to judge whether it is a good test of anything.

Report only what a careful reader would call a genuine problem:

  contradiction          two lines state incompatible facts about the same
                         person or thing, and nothing between them corrects it.
                         A correction that is stated as one is fine and is the
                         point of some of this history.
  impossible-timing      something is referred to as done, due or past on a date
                         that does not work — an order arriving before it
                         shipped, a deadline described as passed before it fell.
  physically-impossible  someone is in two places at once, or an interval does
                         not add up, or a named day of the week is not that day.
  reference-before-fact  a line relies on something the history only establishes
                         later, with nothing earlier to support it.
  implausible            not impossible, but a reader would stumble: a person
                         behaving unlike everything else says about them.

Be strict about evidence and quote the dated lines. Say nothing rather than pad.

Much of what looks wrong here is deliberate, and you are shown what each moment
is testing so you can tell the difference. A tension that a checkpoint's
expectations *ask about* is the point of the scene, not a defect: one person
contradicting another, an instruction the user later questions, a claim a newer
first-hand account supersedes. Report a contradiction only when nothing in the
history corrects it and no checkpoint is written to notice it.

Be careful with that second clause, because it is the one that will talk you
out of a real finding. A checkpoint testing a correction covers the correction
itself, not everything on the subject afterwards. If a line re-asserts what an
earlier line corrected, and it comes *after* the correction, that is a
contradiction no matter how many checkpoints ask about the original — the
scene tests whether the store dropped the old claim, and the history quietly
putting it back is a fault in the history. This history also
contains unresolved threads and things nobody ever answers, all on purpose."""


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["should", "should_not"],
    "properties": {
        "should": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item", "verdict", "why"],
                "properties": {
                    "item": {"type": "string"},
                    "verdict": {"enum": ["supported", "unsupported", "contradicted"]},
                    "why": {"type": "string"},
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
                    "verdict": {"enum": ["fair-distractor", "now-relevant", "never-established"]},
                    "why": {"type": "string"},
                },
            },
        },
    },
}

SYSTEM = """You are checking whether a test is still a fair test, not whether an
assistant passed it. You never see an assistant's answer. You see the history a
store would have been built from, and the expectations written for one moment in
it, and you say whether those expectations still make sense there.

For each `should` — something that ought to come back:
  supported    the history before this moment establishes it, so a good store
               could produce it.
  unsupported  nothing before this moment establishes it. The expectation cannot
               be met by any store, so the checkpoint measures nothing.
  contradicted later material makes it false or doubtful and no correction
               reconciles them, so a good store might rightly disagree with it.

For each `should not` — a distractor that ought not to crowd in:
  fair-distractor    it is in the history and a careless store might surface it
                     here, but it is genuinely not what this moment calls for.
                     This is the healthy state and most items should be this.
  now-relevant       it has become a reasonable thing to raise here. Perhaps it
                     is recent, or unresolved, or the situation touches it. The
                     expectation would now penalise a store for being right.
  never-established  nothing before this moment puts it in the store at all, so
                     it can never come up and the expectation measures nothing.
                     One exception: a `should not` that forbids *invention* — "a
                     cover story", "details she never gave" — is legitimately
                     not in the store, because it forbids what must never be
                     there. That is a fair distractor. The test is whether the
                     item names something that could be retrieved or something
                     that could only be made up.

Lines on the `dm` channel are one person speaking to the assistant alone;
`email` lines are mail in someone's mailbox; lines on a `thread:<name>`
channel are messages in one Slack thread that everyone in it reads, the
assistant included, so there the people are talking to each other as well as
to her, and whatever she says is heard by all of them.

Judge against the history you are given and the moment you are given. Recency
matters: something last mentioned six months ago and settled is a fair
distractor; the same thing raised last week and still open usually is not. Be
conservative — say fair-distractor and supported unless you can name the line
that changes it. Quote the dated line you are relying on in `why`.

Some distractors carry a note about what real runs against an earlier version of
this history did with them. Read it precisely, because two of the outcomes are
opposites. "Crowded in as noise" means assistants raised it for no reason and it
took space — weigh that heavily and above your own reading of what is relevant,
because it is evidence about what stores actually do where the rest of your
judgement is a guess about what they should do. A distractor that crowded in as
noise in most runs, and that new material has since made more recent or more
unresolved, is now-relevant even when the moment seems to be about something
else. "Raised and justified" is the opposite: the assistant named it in order to
resolve a conflict, which is the behaviour the store is supposed to have, and it
is not evidence against the expectation at all. "Stayed absent" is neutral on
its own — an expectation can be absent because it is well chosen or because
nothing ever established it, and only the history tells you which. You are not being asked whether surfacing it
was correct — only whether an expectation that forbids it is still a fair test
of memory rather than a disagreement about what the question means."""


def structural(paths: list[Path], inputs: list[C.Input]) -> list[str]:
    """Everything checkable without asking a model. Cheap, exact, always run.

    What a session can reach is not asked here. Whether a passage carries
    something particular to this history is a judgement, and the `check-lab-leak`
    skill is what makes it; a run's own reading of the instrument is counted by
    the run and reported with it."""
    out: list[str] = []
    for p in paths:
        for i, raw in enumerate(p.read_text().splitlines(), 1):
            if LOOKS_LIKE_INPUT.match(raw) and not C.LINE.match(raw):
                out.append(f"{p.name}:{i} looks like an input and did not parse — "
                           f"it is silently dropped: {raw.strip()[:90]}")
            if LOOKS_LIKE_EXPECT.match(raw) and not C.EXPECT.match(raw):
                out.append(f"{p.name}:{i} looks like an expectation and did not parse: "
                           f"{raw.strip()[:90]}")

    checkpoints = [i for i in inputs if i.is_checkpoint]
    for c in checkpoints:
        if not c.should:
            out.append(f"{c.scene}: checkpoint has no `should`, so nothing is measured")
        # a budget line is not scored, but its absence means the default 5 was
        # taken by accident rather than chosen
        for item in c.should + c.should_not:
            for d in re.findall(r"\d{4}-\d{2}-\d{2}", item):
                if d > c.date:
                    out.append(f"{c.scene}: expectation names {d}, after the "
                               f"checkpoint's own {c.date}: {item[:60]}")

    # a month marker anchored to a date that is nothing in the corpus can drift
    # from the thing it names: shifts are whole weeks, and "April" anchored to
    # the first and a lunch on the nineteenth fall in different months whenever
    # a shift puts a month boundary between them. Scene 8 read "May" for a
    # June lunch one week and was right the week before, and no single lint
    # run could have seen it. Anchoring to the event's own date makes the two
    # move together; `month` is for a bare month naming no event.
    dated = set()
    for p in paths:
        text = p.read_text()
        dated |= {m.group(1) for m in re.finditer(r"^[-?] (\d{4}-\d{2}-\d{2}) \|", text, re.M)}
        dated |= {m.group(1) for m in re.finditer(r"\{\{(\d{4}-\d{2}-\d{2})\|(?:dom|dm|dmy|d|iso)\}\}", text)}
    for p in paths:
        for i, raw in enumerate(p.read_text().splitlines(), 1):
            for m in re.finditer(r"\{\{(\d{4}-\d{2}-\d{2})\|mo\}\}", raw):
                if m.group(1) not in dated:
                    out.append(f"{p.name}:{i} month marker {m.group(0)} anchors to a date that is "
                               f"no input and no dated reference, so it can drift from what it "
                               f"names; anchor it to the thing's date, or use `month` for a bare month")

    names = Counter(i.scene for i in inputs)
    for scene, n in names.items():
        num = re.match(r"Scene (\d+)", scene or "")
        if num and sum(1 for s in names if re.match(rf"Scene {num.group(1)}\b", s or "")) > 1:
            out.append(f"scene number {num.group(1)} is used by more than one heading")

    seen = defaultdict(list)
    for i in inputs:
        if not i.is_checkpoint:
            seen[(i.date, i.speaker, i.text)].append(i.scene)
    for (date, _, text), scenes in seen.items():
        if len(scenes) > 1:
            out.append(f"note: {date} \"{text[:50]}\" appears in {len(scenes)} scenes "
                       f"({', '.join(scenes)[:70]}) and so is replayed {len(scenes)} times")
    return out


def past_leaks(scored: list[Path]) -> dict[tuple[str, str], tuple[int, int]]:
    """How often each `should not` was surfaced anyway, from past scored runs.

    The model's own sense of what is relevant here is a guess; this is the only
    hard evidence available about what a store actually does with a distractor,
    and it is what separates an expectation that is merely hard from one the
    corpus has been losing. Keyed loosely, because a scored report holds the
    expectation text and the scene heading and nothing else to join on."""
    hits: dict[tuple[str, str], list[str]] = defaultdict(list)
    for path in scored:
        scene = ""
        for line in path.read_text().splitlines():
            if line.startswith("## "):
                scene = line[3:].strip()
            m = re.match(r"\s+should not\s+(present-noise|present-justified|absent)\s+(.+)$", line)
            if m and scene:
                hits[(scene, m.group(2).strip())].append(m.group(1))
    # noise and justified are opposites, not degrees. Naming a conflict in order
    # to resolve it is the behaviour the store is supposed to have, so counting
    # it as a leak tells the model a scene is failing when it is working: scene
    # 9's retracted "sister" claim is present-justified 4/4 and is the point of
    # the scene. Only noise is evidence that an expectation has gone bad.
    return {k: (v.count("present-noise"), v.count("present-justified"), len(v))
            for k, v in hits.items()}


def check_one(cp: C.Input, history: list[C.Input], timeout_s: int = 180,
              leaks: dict | None = None) -> dict:
    # judge reaches the store through a compiled module, and the structural
    # pass has no need of it: deferred so the free checks run anywhere
    import judge as J
    lines = [f"{i.date} | {i.channel} | {i.speaker} | {i.text}" for i in history]
    prompt = (
        "The history, in order, as the store was built from it:\n" +
        "\n".join(f"  {l}" for l in lines) +
        f"\n\nThe moment being tested ({cp.date}, {cp.channel}, from {cp.speaker}):\n"
        f"  {cp.text}\n\n"
        "should:\n" + "\n".join(f"- {s}" for s in cp.should) +
        ("\n\nshould not:\n" + "\n".join(
            f"- {s}" + _leak_note(leaks, cp.scene, s) for s in cp.should_not)
         if cp.should_not else "")
    )
    argv = ["claude", "-p", "--output-format", "json", "--model", MODEL,
            "--json-schema", json.dumps(SCHEMA), "--system-prompt", SYSTEM,
            "--no-session-persistence", "--tools", ""]
    # same isolation as the judge: run from a repo of its own, or it reads this
    # project's CLAUDE.md and memory about the very corpus it is checking
    p = subprocess.run(argv, input=prompt.encode(), capture_output=True,
                       timeout=timeout_s, cwd=str(J.isolated()),
                       env={**os.environ, **J.NO_AUTO_MEMORY})
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:300])
    body = json.loads(p.stdout.decode()).get("result") or "{}"
    return json.loads(body) if isinstance(body, str) else body


def _leak_note(leaks: dict | None, scene: str, item: str) -> str:
    if not leaks:
        return ""
    n = leaks.get((scene, item))
    if not n or not n[2]:
        return ""
    noise, justified, total = n
    bits = []
    if noise:
        bits.append(f"crowded in as noise in {noise} of {total} past runs")
    if justified:
        bits.append(f"raised and justified in {justified} of {total}, which is correct behaviour")
    if not bits:
        bits.append(f"stayed absent in all {total} past runs")
    return "   [" + "; ".join(bits) + "]"


def coherence(inputs: list[C.Input], subject: str | None, timeout_s: int = 600) -> dict:
    """One pass over the history, whole or filtered to a single subject.

    Per-subject is where this earns its keep. Contradictions arise because the
    cast is shared and scenes were written separately, so they are almost always
    about one person or one thing, and a pass that reads only that entity's
    lines sees them next to each other instead of scattered through ninety."""
    import judge as J
    # each input and its annotation travel as one unit, and the annotation
    # carries its own date. Filtering them separately orphans an annotation
    # from the checkpoint it belongs to; it stacks under the nearest matching
    # line and reads as one moment contradicting itself, when the two were
    # days apart.
    units = []
    for i in inputs:
        block = [f"{i.date} | {i.channel} | {i.speaker} | {i.text}"]
        # what the moment is testing, so a designed tension is legible as one
        if i.is_checkpoint and i.should:
            block.append(f"        [{i.date} tests: {'; '.join(i.should)[:300]}]")
        units.append(block)
    if subject:
        pat = re.compile(rf"\b{re.escape(subject)}\b", re.I)
        keep = [l for u in units if any(pat.search(l) for l in u) for l in u]
        head = (f"Every line in the history mentioning {subject}, in order. Judge only "
                f"whether these are consistent with each other.\n\n")
    else:
        keep, head = [l for u in units for l in u], "The whole history, in order.\n\n"
    prompt = head + "\n".join(f"  {l}" for l in keep)
    argv = ["claude", "-p", "--output-format", "json", "--model", MODEL,
            "--json-schema", json.dumps(COHERENCE_SCHEMA),
            "--system-prompt", COHERENCE_SYSTEM,
            "--no-session-persistence", "--tools", ""]
    p = subprocess.run(argv, input=prompt.encode(), capture_output=True,
                       timeout=timeout_s, cwd=str(J.isolated()),
                       env={**os.environ, **J.NO_AUTO_MEMORY})
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:300])
    body = json.loads(p.stdout.decode()).get("result") or "{}"
    return json.loads(body) if isinstance(body, str) else body


def subjects(inputs: list[C.Input], floor: int = 3) -> list[str]:
    """Recurring names, which is where a shared cast goes wrong."""
    words: Counter = Counter()
    for i in inputs:
        for w in re.findall(r"\b[A-Z][a-z]{2,}\b|\b(?:fan|mei|jane)\b", i.text):
            words[w.lower().capitalize()] += 1
    skip = {"Every", "Order", "About", "Account", "Your", "Their", "There", "This",
            "That", "When", "What", "Worth", "Heads", "Redelivery", "Bellwood"}
    # month names render into prose now that dates are markers, so they look
    # like recurring proper nouns and would each draw their own pass
    skip |= {date(2026, m, 1).strftime("%B") for m in range(1, 13)}
    return [w for w, c in words.most_common() if c >= floor and w not in skip]


def main() -> int:
    ap = argparse.ArgumentParser(description="check each checkpoint against its own history")
    ap.add_argument("--corpus", nargs="+", default=[str(ROOT.parent / "docs" / "recall-corpus.md")],
                    help="one or more corpus files, read as a single corpus")
    ap.add_argument("--out", default=str(ROOT / "lint.md"))
    ap.add_argument("--scene", default="", help="substring; default every checkpoint")
    ap.add_argument("--scored", nargs="*", default=[],
                    help="past scored reports; grounds should-not judgements in what "
                         "stores actually surfaced rather than in what seems relevant")
    ap.add_argument("--coherence", action="store_true",
                    help="check the history against itself for contradictions, "
                         "impossible timing and physical impossibilities")
    ap.add_argument("--structural-only", action="store_true",
                    help="skip the model pass")
    args = ap.parse_args()

    paths = [Path(p) for p in args.corpus]
    delta = C.anchor_delta(paths)
    inputs: list[C.Input] = []
    for p in paths:
        inputs.extend(C.parse(p, delta))
    ordered = C.replay_order(inputs)

    report = ["# Corpus lint", "",
              f"{len(paths)} file(s), {len(inputs)} inputs, "
              f"{sum(1 for i in inputs if i.is_checkpoint)} checkpoints, "
              f"{sum(len(i.should) + len(i.should_not) for i in inputs)} expectations.", ""]

    leaks = past_leaks([Path(p) for p in args.scored]) if args.scored else {}
    problems = structural(paths, inputs)
    # a note describes the corpus; a problem says something is broken. Only the
    # second should fail the gate, or the three deliberately shared inputs would
    # make every run of this red forever.
    notes = [p for p in problems if p.startswith("note:")]
    problems = [p for p in problems if not p.startswith("note:")]
    report += ["## Structural", ""]
    report += [f"- {p}" for p in problems] or ["- nothing broken"]
    report += [""] + [f"- {p}" for p in notes] if notes else []
    report.append("")

    if args.coherence:
        report += ["## Coherence", ""]
        found, errors = 0, 0
        seen: set = set()
        for subj in [None] + subjects(ordered):
            try:
                # the whole-history pass reads every line and annotation and
                # has finished in 475s on 128 inputs; the per-subject passes
                # read a tenth of that. One ceiling for both made the gate flaky.
                res = coherence(ordered, subj, timeout_s=1800 if subj is None else 600)
            except Exception as e:
                # a pass that died found nothing, which looks exactly like a
                # pass that found nothing. Count it, say so, and fail.
                errors += 1
                report.append(f"- **ERROR** on {subj or 'whole history'}: {str(e)[:160]}")
                print(f"coherence {subj or 'whole history':22} ERRORED", file=sys.stderr)
                continue
            for f in res.get("findings", []):
                # the whole-history pass and the per-subject passes see the same
                # lines, so a real finding surfaces two or three times
                key = (f["kind"], tuple(sorted(l.strip()[-60:] for l in f.get("lines", []))))
                if key in seen:
                    continue
                seen.add(key)
                found += 1
                report.append(f"- **{f['kind']}** ({f['confidence']}, via "
                              f"{subj or 'whole history'}) — {f['what']}")
                for ln in f.get("lines", [])[:3]:
                    report.append(f"    {ln}")
            print(f"coherence {subj or 'whole history':22} "
                  f"{len(res.get('findings', []))} found", file=sys.stderr)
        report += ["", f"{found} raised in total"
                   + (f", and {errors} pass(es) errored — that is not a clean run" if errors else "."), ""]
        Path(args.out).write_text("\n".join(report) + "\n")
        print(json.dumps({"structural": len(problems), "notes": len(notes),
                          "coherence_findings": found, "coherence_errors": errors}))
        return 1 if problems or found or errors else 0

    counts: Counter = Counter()
    if not args.structural_only:
        report += ["## Each checkpoint against the history before it", ""]
        for cp in [i for i in ordered if i.is_checkpoint]:
            if args.scene and args.scene not in cp.scene:
                continue
            # everything strictly earlier, plus same-day non-checkpoints: the
            # replay order gives a checkpoint the whole day it arrives in
            history = [i for i in ordered if (i.date, i.is_checkpoint) < (cp.date, True)
                       and i is not cp]
            try:
                v = check_one(cp, history, leaks=leaks)
            except Exception as e:  # a corpus check should never block on one bad call
                report += [f"### {cp.scene}", "", f"  ERROR {e}", ""]
                print(f"error {cp.scene[:40]}: {e}", file=sys.stderr)
                continue
            bad = [r for r in v.get("should", []) if r["verdict"] != "supported"]
            bad += [r for r in v.get("should_not", []) if r["verdict"] != "fair-distractor"]
            for r in v.get("should", []) + v.get("should_not", []):
                counts[r["verdict"]] += 1
            mark = "" if not bad else "  ← needs a look"
            report += [f"### {cp.scene}{mark}", "",
                       f"`{cp.date} | {cp.speaker} | {cp.text}` · {len(history)} inputs before it", ""]
            for r in v.get("should", []):
                report.append(f"  should      {r['verdict']:13} {r['item']}")
                report.append(f"                            {r['why']}")
            for r in v.get("should_not", []):
                note = _leak_note(leaks, cp.scene, r["item"])
                report.append(f"  should not  {r['verdict']:17} {r['item']}{note}")
                report.append(f"                                {r['why']}")
            report.append("")
            print(f"checked {cp.scene[:44]:44} {len(bad)} to look at", file=sys.stderr)

    tally = {"structural": len(problems), "notes": len(notes), **dict(sorted(counts.items()))}
    report += ["## Tally", "", "```", json.dumps(tally, indent=2), "```", ""]
    Path(args.out).write_text("\n".join(report) + "\n")
    print(json.dumps(tally))
    # a broken expectation is a broken test, so this is a build failure
    return 1 if (problems or any(k for k in counts
                                 if k in ("unsupported", "contradicted",
                                          "now-relevant", "never-established"))) else 0


if __name__ == "__main__":
    raise SystemExit(main())
