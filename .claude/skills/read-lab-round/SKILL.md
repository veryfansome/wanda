---
name: read-lab-round
description: After a round is judged, read it per expectation, find what the bands mean, and check how the sessions behaved
---

# What a round is

Four runs of one configuration against the same history — `runs/<round>A` through `D`. Four runs are four draws. **A verdict that differs between them is noise; a verdict that repeats is signal.** Nothing in a single run is a finding.

Read per expectation, never per total. An expectation that fires in all four is reliable, one that fires in none is a standing failure, and the ones in between are the work list.

# Getting the bands

The scored reports are `runs/<round><R>/scored<round><R>.md`. Line the four up **by the arrival's position**, not by scene name — six scenes carry two or three checkpoints each, so a scene-keyed join silently pairs nine of the fifty-four with another checkpoint's expectations. The position is `input_id` on each results record.

Count, for each expectation, how many of the four runs scored it `hit`. That gives four bands. Report the shape before anything else, because it says where the work is.

# Reading the never band

For each expectation that fires in none of the four runs, establish which of three things happened, and prove it from the files rather than from the verdict:

- **the store never had it** — check the snapshot the checkpoint actually met, `git --git-dir=runs/<round><R>/report*-snaps.git show <rec.snapshot>:<path>`, not the vault as the run ended. Later sessions overwrite the vault, so the final store is not what the checkpoint saw.
- **the store had it and the session did not retrieve it** — the node is in that snapshot and absent from the record's `recalled`.
- **the session retrieved it and did not say it** — the node is in `recalled` and the expectation asks for something to be *said*. `judge.py`'s rubric separates a `should` that asks the assistant to HAVE something from one that asks it to SAY something a particular way, and a say-shaped `should` is a miss whenever the answer is empty.

The three point at different work — capture, ranking, instruction — so the counts matter more than the individual cases.

Read the session's own reasoning, not only its verdict: `docker compose run --rm -T tools sh -c 'MEM_VAULT=/work/runs/<round><R>/vault MEM_TRANSCRIPTS=/work/runs/<round><R>/transcripts/-work-runs-vault /work/lab/bin/mem session <id>'`. Every `mem` call rebuilds the store's indexes, so give it a copy of the vault rather than the round's own if you mean to keep that untouched. A session that recalled the right node and stayed silent usually says why, in an aside.

# Reading the middle band

For an expectation that fires in some runs and not others, find what differed:

- **the store** — compare the snapshots the two checkpoints met
- **the recall** — same store, different `recalled`; compare the `mem recall` calls in the tool log
- **the answer** — same recall, different thing said

Which of those dominates is the finding. They point at capture, at ranking, and at instruction respectively.

# Discipline is two numbers, and they are not the same

`over_budget` in the tally is arithmetic: how many checkpoints returned a list longer than the budget. It says nothing about order.

`within` / `buried` / `nothing` is the judged verdict: whether what the checkpoint asked for arrived at or before the budget position. A run can be over budget on two thirds of its checkpoints and `within` on nearly all of them — long tail, right head. Do not report the first as if it were the second.

# How the sessions behaved

The tool log — `runs/<round><R>/report<round><R>-tools.jsonl` — is derived from each session's transcript, so it carries calls that **failed** as well as ones that worked, with the result and the whole argument. A refused attempt is often more informative than a successful one.

Check for a session reaching outside its own store. These are low-stress exchanges and a session has no reason to look for answers, so treat this as observation rather than suspicion. What matters is a change against the baseline. Round 16, 8,461 calls across four runs, is the baseline:

```
git beyond the vault      0     (10 calls, all `git diff`/`git status` in the vault's own repo)
the snapshot repo         0
harness source           16     grep of the store's source, then mounted as Python, for `def mint` — understanding the tool
the environment            9     `env | grep MEM_D` — working out what date it was given
docs/ or another round     0
```

The source of `mem` is not in the container — what is mounted is a binary — so a session that looks for it finds nothing. That attempt is ordinary curiosity about a verb, not a signal. A session reading the snapshot repo, another round's directory, or `docs/` is not, and means the mount list has drifted — check `docker compose config --format json` before concluding anything about the session.

The run counts this for you. `peeked` in the run's stats is how many tool calls named the run's own files or the directory the binaries are mounted in, and when it is not zero the report carries a **Looked at the instrument** section with the session id and the call. Read those sessions' transcripts. The question is not whether reading was allowed — it was — but whether what the session recalled afterwards came from its memory or from what it found, and if the latter helped, whether that belongs in the standing instructions instead.

# What a round costs

`python3 lab/spend.py --record` adds the round to the ledger and prints the trend; a run more than 10% over the earlier average is flagged. Cost per session is what compares across rounds. Turn count beside it says whether a change in cost came from sessions doing less work or from each turn carrying less.

# Before writing any of it down

Check every finding against the snapshots. The judge's verdict is a model's reading, the answer is what the session said, and the final vault is what every later session left behind — none of them is what the session had in front of it. Findings written from the first three and then checked against the snapshots have changed in the checking.

Say which round, and which runs, every number came from. A round that changed more than one thing at once cannot attribute a movement to any of them, and should say so rather than implying a cause.
