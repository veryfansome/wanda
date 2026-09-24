---
name: run-lab-round
description: Launch a round of the corpus against the lab container and watch it through — prerequisites, the parse-outside-and-pipe-in shape, what to watch, what goes wrong
---

# What a round is

Four runs of one configuration against the same history, in `runs/<round>A` through `runs/<round>D`. Four is the minimum that means anything — round 3's two identical runs differed by seven hits out of twenty-two — and four is still only four draws.

One run is the whole corpus, one headless Claude Code session per arrival, each with the vault as its working directory. Today that is 141 arrivals, 54 of them checkpoints; the corpus grows, so take the counts from `python3 lab/corpus.py docs/recall-corpus.md` rather than from this paragraph.

This skill stops when the four logs print their stats line. Scoring and reading the round are `read-lab-round`.

# Before launching

**The token.** `claude setup-token` once on the host, and paste the result into `.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`. Subscription only; `.env` is gitignored. This is the one prerequisite that fails before anything starts — compose will not even render the service without it, with `required variable CLAUDE_CODE_OAUTH_TOKEN is missing a value: set it in .env`.

**The image.** `docker compose build`. Only the `Dockerfile` needs it.

**The build.** `LAB_REV=$(docker compose run --rm -T builder python3 /work/lab/build.py) || exit 1`, then `export LAB_REV`. A session can reach three binaries under `lab/bin/`, not the sources, because a comment is prose written by someone who knows the history. An edit to the crate takes effect on the next **build**, not the next `docker compose run`. Taking `LAB_REV` from the build makes a forgotten build harmless: the only way to have a revision is to have compiled. Keep the assignment and the export on separate lines — `export VAR=$(cmd)` reports export's status and swallows a failed build.

**A variant, if this round is testing one.** The three texts a session reads in its vault — the standing instructions and the two skills — are `memory/templates/*.md`. Do not edit them to run an experiment. Put only the files that differ in `lab/variants/<name>/` and build with `LAB_VARIANT=<name>`; `build.py` resolves the two and records the variant name and each resolved file's sha256 in `lab/bin/BUILD.json`, so the round names what its sessions met by content. Two runs of a round can carry different variants, for an A/B rather than four draws of one thing.

**The two variables.** `export LAB_RUN=16A`, and `LAB_REV` from the build above. `LAB_RUN` picks which directory under `runs/` this run writes into; inside the container that directory is always `/work/runs`, whichever round it is. `LAB_REV` is the revision the readings get stamped with, and it has to be given rather than asked for because `.git` is not mounted and `git rev-parse` cannot answer in there. The run exits rather than starting when it is missing or `unknown`; a tree with edits in it builds as `<rev>-dirty`.

**The directories, by hand.** `mkdir -p runs/$LAB_RUN/transcripts`. Both are bind-mount sources, and Docker creating a missing one fails the whole command: `error while creating mount source path '.../runs/16A': chown ...: permission denied`.

**The leak check.** A session that can read the history is not a measurement. The `check-lab-leak` skill is that reading, over what the build staged; run it after the build and before the round. `python3 lab/lint.py --structural-only` is free and exits non-zero on anything it finds, but it checks only the history itself — what did not parse, a checkpoint with nothing to measure, dates and markers that can drift — and reads none of what a session can reach.

# The shape of the command, and why

```sh
export LAB_RUN=16A
LAB_REV=$(docker compose run --rm -T builder python3 /work/lab/build.py) || exit 1
export LAB_REV
mkdir -p runs/$LAB_RUN/transcripts
python3 lab/corpus.py docs/recall-corpus.md \
  | docker compose run --rm -T lab \
      /work/lab/run --vault /work/runs/vault \
              --out /work/runs/report$LAB_RUN.md \
      2> runs/$LAB_RUN/run$LAB_RUN.log
```

**The history is parsed on the host and the arrivals are piped in.** `corpus.py` emits each arrival as `id`, `date`, `channel`, `speaker`, `text`, `scene` and `is_checkpoint`, plus the anchor the timeline was shifted to and a digest of the history it came from. What a checkpoint was supposed to bring back does not go in — the run never sees `should`, `should not` or `budget`, and `judge.py` joins them back afterwards by the arrival's position. So the process driving the sessions does not hold the answers, and because the arrivals come in on stdin they are not a file anything in the container can open.

Two consequences for the recipe. `-T` is required, and `-d` cannot be used: a detached container is given no stdin, and the run would block on it forever rather than fail. To background a run, background the whole pipeline.

Paths inside the container are absolute. The working directory is `/work/runs`, the run's own directory, not the repo.

Pass both `--vault` and `--out`, always. `--out` names the report and every path derived from its stem — results, sessions, mem log, tool log, the snapshot repo — and the vault is separate, comes from `--vault`, and is deleted and recreated at startup.

# Four in parallel

```sh
LAB_REV=$(docker compose run --rm -T builder python3 /work/lab/build.py) || exit 1
export LAB_REV
python3 lab/corpus.py docs/recall-corpus.md > /tmp/arrivals.json
for r in A B C D; do
  mkdir -p runs/16$r/transcripts
  LAB_RUN=16$r nohup sh -c "docker compose run --rm -T lab \
    /work/lab/run --vault /work/runs/vault \
            --out /work/runs/report16$r.md < /tmp/arrivals.json" \
    > /dev/null 2> runs/16$r/run16$r.log & disown
done
```

Parse once into a file, not four times in four pipes. `corpus.py` shifts the timeline so the history's last date lands near today, so two parses either side of midnight are two different timelines and the four runs would no longer be four draws of one thing. The file stays on the host — it is not mounted, and each container gets it through that shell's stdin redirect.

# How long, and what it costs

Round 16's four runs took between 11,190 and 12,138 seconds — three hours and a quarter each, 79 to 86 seconds a session over 141 sessions. They run in parallel, so the round is as long as the slowest of the four. Nothing useful comes out before the end: the judge wants a finished results file.

Round 16 cost $112.04, $0.199 a session, from Claude Code's own per-session estimate. Rounds 11 to 13 ran at $0.25 a session and round 15 at $0.225. Read the current figure off `python3 lab/spend.py` rather than off this paragraph.

The run passes `--max-budget-usd 2.50` per session. That is a runaway guard — several times what a long session spends — not a cost control.

Afterwards, `python3 lab/spend.py --record` appends the four runs to `runs/spend.jsonl` and prints the trend. A run more than 10% above the earlier rounds' average is flagged `WARNING`, and is a reason to look at what the sessions did differently before launching another.

# What to watch while it runs

`tail -f runs/16A/run16A.log`. One line per arrival, as it finishes:

```
[83/141] 2026-07-08 orders CHECK turns=18 cost=0.28888820000000004 | Redelivery scheduled: your item will now arr
```

`CHECK` marks a checkpoint — 54 of the 141 — and only those get a record in the report and the results file. The other two numbers are worth watching as a trend rather than read one at a time. Turns collapsing toward two or three across many sessions means the sessions have stopped doing the work, not that they got efficient; round 16 averaged 17.9 turns a session. Cost per session drifting well above the ledger's last round means the same thing from the other side, and it is cheaper to notice at arrival thirty than at arrival 141.

Two lines appear only when something has gone wrong, and both are worth catching while the run is still young:

```
  session error 2026-09-07: ... timed out after 420 seconds
  placeholder output 2026-05-02: ['answer']
```

A placeholder is a session that did the work and then filled the schema with scaffolding — `todo`, `tbd`, `example` — which reads as a recall failure and is not one. It is recorded, not retried.

The run ends with two JSON lines on stderr, the stats and the cost:

```
{"lab": "93064fb", "inputs": 141, "sessions": 141, "errors": 0, "turns": 2598, "placeholder": 0, "elapsed_s": 12137.7, "transcripts": "-work-runs-vault"}
{"cost": 29.1424}
```

`sessions` short of `inputs` is the error count. `"lab": "unknown"` means `LAB_REV` never reached the container. A further line — `auto-memory written by the last session: [...]` — means Claude Code's own memory system wrote into the session's project directory despite `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, which puts a second memory system beside the one under test. It is checked once at the end, so it can only ever see the last session's writes: it catches a flag that has stopped working, not a single session's leak.

# What actually goes wrong

**A session times out.** 420 seconds by default, `--timeout` to change it. The run counts it in `errors`, records the arrival with an empty answer and nothing recalled, and carries on. Round 16D lost one session of 141 that way. One is noise; several in a run is something to explain before scoring, because every later session in that run met a store missing whatever the dead one would have written. What it wrote before it was killed stays in the vault.

**The run directory has already been used.** A run directory holds one run, and the run refuses a second before launching any session:

```
runs/16A is not empty: report16A.md
each directory is used once — pick another
```

It exits 2, and it trips on anything at all in the directory other than `transcripts/`. The rule exists because what lands in a run directory afterwards is a scored report and a finished vault, and the whole directory is mounted into any session run against it later. Pick a new name, or delete the old directory once you are certain you have read it.

**`LAB_REV` is unset.** The run exits 2 before claiming the directory or writing anything, printing `LAB_REV is not set: this run cannot name the code that made it` and the build command to get one. Take it from the build and this cannot happen: the only way to have a revision is to have compiled.

**Nothing appears in the log at all.** The container has no stdin and the run is blocked reading it: either `-T` was dropped or the container was detached. Kill it and launch again.

**`docker compose run` fails on the mount.** The run directory or its `transcripts` did not exist. Create both and relaunch; nothing was spent.

# Smoke runs

A smoke run exercises the plumbing — mounts, token, prompt, vault, skills, logs — on a handful of sessions, before three hours and a hundred dollars are committed to a configuration that was never going to start.

```sh
LAB_REV=$(docker compose run --rm -T builder python3 /work/lab/build.py) || exit 1
export LAB_REV LAB_RUN=smoke1
mkdir -p runs/$LAB_RUN/transcripts
python3 lab/corpus.py docs/recall-corpus.md \
  | docker compose run --rm -T lab \
      /work/lab/run --limit 16 --vault /work/runs/vault \
              --out /work/runs/report$LAB_RUN.md 2>&1 | tail -20
```

The first checkpoint is the 16th arrival, so `--limit 16` is the smallest limit that reaches one and writes a results file at all. Below that you get a vault, a sessions log and a tool log and no results, which is usually what a smoke run is for.

A smoke directory is used once like any other, so give each one a new name or delete the last. With `LAB_RUN` unset entirely, compose sends the run to `runs/scratch` — a reasonable default for the first smoke run and a refusal on the second.

Nothing a run produces is committed: `runs/` is gitignored whole. Re-run to see it again.
