---
name: score-lab-round
description: Score a finished round through judge.py, read what each tally field measures, and replay or rebuild a store when a verdict needs splitting
---

# What scoring is

A run leaves `runs/<round><R>/report<round><R>-results.jsonl`, one record per checkpoint — the arrival, what the session recalled in its own order, what it answered, and the sha of the store it met. Nothing in it says what the checkpoint was supposed to bring back.

`judge.py` joins the expectations back from the history, materialises each checkpoint's own store, and asks a model to read the two against each other. Running and scoring are separate on purpose: a change to the rubric should never cost another round of sessions.

Score all four runs. A verdict from one run is a draw, not a finding, and `read-lab-round` is what to do with the four once they exist.

# Scoring the four runs

```sh
for r in A B C D; do
  docker compose run --rm tools python3 lab/judge.py \
    --results runs/16$r/report16$r-results.jsonl \
    --vault runs/16$r/vault \
    --out runs/16$r/scored16$r.md \
    2> runs/16$r/judge16$r.out
done
```

Use `tools`, never `lab`. `tools` mounts the repo whole, which is what the judge needs — the expectations live in `docs/recall-corpus.md`. `lab` mounts three binaries and one run directory so that a session cannot reach the history. A scoring pass run there would fail rather than cheat, but keep the habit.

`LAB_RUN` and `LAB_REV` do nothing here. They exist for the `lab` service, which picks its run directory from the first and stamps its readings with the second; `tools` sees the repo at `/work` and takes ordinary repo-relative paths.

One model call per checkpoint, on `claude-sonnet-5` — 54 checkpoints per run against the current corpus, so at most 216 calls for a round. The stderr redirect is worth keeping: judge prints one line per checkpoint as it goes, and that is the only progress there is.

`--vault` is the fallback store, used only for a record whose snapshot cannot be found. It is not where the scoring normally reads from.

`--snaps` names the snapshot repo when it is not beside the results file. For an ordinary run it is, so leave it off.

# Why `--corpus`, and what the digest check is stopping

A run is given arrivals and nothing else. `corpus.py` emits date, channel, speaker, text, scene and position; `should`, `should not` and `budget` never enter the container, so the process driving the sessions does not hold the answers.

`judge.py --corpus` (default `docs/recall-corpus.md`) re-parses the history, shifts it to the anchor the run recorded, and joins each record to its expectations **by the arrival's position**. The shift matters: an expectation names dates and they moved with the timeline, so the expectations have to be re-rendered against the run's own anchor rather than today's.

A position only means something against one history. Insert a line into the corpus and every position after it names a different arrival — and nothing downstream can see that, because the join still succeeds and still produces a full tally.

So every record carries `history`, a digest of the corpus bytes, and judge refuses any record whose digest does not match the corpus it was given. It names the first few, says how many, and exits 2 without writing a scored report at all. Scoring around the failure is not an option it offers, and that is deliberate: a partial tally reads exactly like a whole one.

The practical consequence is that **a round must be scored before the corpus is edited**, or scored against the commit it was run from. Round 16's records carry `41f9c3c879cb`, which is the corpus as it stands; a scene added tomorrow invalidates every unscored run on disk.

# A checkpoint is judged against the store it met

The run commits the vault to `report<run>-snaps.git` before every session — 141 commits for round 16, one per arrival, each named `before <id>`. Each results record carries that sha in `snapshot`.

Judge materialises the record's own commit into a temp directory and renders the recalled refs from there: each ref with the node's name, its summary, its body and its live edges. Edges are rendered because a relationship lives in an edge — a person node can hold `cousin_of → person:fan` and an empty body, and that edge is the whole fact. A body over 1600 characters is cut on its own lines, first line plus the newest that fit, because bodies are appended one line per update and taking the first N characters keeps the oldest state and drops the current one.

The repo is found from `--snaps`, then from the record's own `snaps` or `store` field, then from the results filename. A candidate counts only if it actually holds **that record's commit**, so a wrong `--snaps` or a stale repo under the right name cannot quietly send a whole pass to the fallback.

What goes wrong when this breaks is not a crash. The store grows through a run, so scoring against the vault as the run ended reads every later session's writes back into every earlier checkpoint: refs render richer than they were, names that did not exist yet resolve, and the tally moves toward hit. The pass completes and looks normal. `scored_against_fallback_vault` is the only thing in the output that says otherwise, which is why it is worth reading before the verdicts.

# What the tally means

The last section of each scored report is one JSON line. Two of its fields are strings of the form `n/total`, not integers, which matters if anything parses it.

Six of the counts measure the sessions:

`hit` / `partial` / `miss` — one per `should` item, not per checkpoint; the current corpus carries 120 `should` items across its 54 checkpoints. Hit means the substance was there, in the recalled items or in the answer, whatever the wording. The rubric separates a `should` that asks the session to *have* something from one that asks it to *say* something, and a say-shaped `should` is a miss whenever the answer is empty — retrieving both options and then volunteering nothing is not offering a choice.

`absent` / `present-justified` / `present-noise` — one per `should not` item. These are distractors. Present-justified is a distractor raised for a reason the answer shows: naming a conflict in order to resolve it is not a leak. Present-noise is one that came up and took space. A `should not` is judged against what the session put in its own recalled list and its answer, never against what those nodes are linked to — an entity reached only as an edge target was not surfaced by the session.

`within` / `buried` / `nothing` — one per checkpoint, the judged reading of discipline: did what the checkpoint asked for arrive at or before the budget position, or in the answer itself. Items after the budget do not matter.

`over_budget` — arithmetic, not a reading: how many checkpoints returned a list longer than the budget. **It is not `buried`.** A run can be over budget on two thirds of its checkpoints and `within` on nearly all of them, which is a long tail behind a right head.

Three fields measure the harness rather than the sessions, and are read first because they say whether the rest is comparable:

`scored_against_fallback_vault` — checkpoints scored against `--vault` because their snapshot could not be found. It should be 0, and was 0 in all four runs of round 16. Anything above it means those verdicts are mixed in with the rest and are not the same measurement; the report says so in bold at the foot as well.

`checkpoints_unscored`, with the scenes named on the `unscored:` line — a checkpoint that was refused rather than scored blind. The causes are a session that returned the schema's own scaffolding, a session that errored during the run, a store that could not be materialised, or a judge call that timed out or failed. Round 16 was 0, 0, 0 and 1; the one was a session error carried through from run time. `checkpoints_scored` is the denominator with these already removed, so a run with unscored checkpoints has a smaller denominator than its siblings and its totals are not directly comparable.

`refs_unresolved` — recalled refs that resolve against no node in the store, with the refs themselves listed at the foot of the report. Mostly this measures the sessions: they write prose into the recalled list rather than only ids. Round 16 ran 18/296, 29/294, 20/281 and 30/276, so six to eleven percent is the floor, not zero. It matters because an unresolved ref is scored without the node's body, which biases toward miss — and a sudden jump past that band is a reason to check that the right store was materialised before concluding anything about the sessions.

The report is written as the pass goes, so an interrupted or crashed run keeps every judgement it has already paid for.

# Recording what the round cost

```sh
python3 lab/spend.py --record
```

From the host, after all four runs have finished. It reads `runs/*/run<round><R>.log`, appends one line per run to `runs/spend.jsonl` and prints the trend. A run whose log has no final stats line has not finished and is not recorded, so a half-run round cannot quietly enter the ledger.

The ledger sits under `runs/` with the readings and is not committed — nothing a run produces is, and the ledger is a thing a run produces. It outlives the logs it is built from, which are cleaned up between rounds, but not the directory. Keep a copy elsewhere if the trend matters beyond this machine.

The table is per round and compares cost per session — a corpus that grows makes a round dearer without anything running hotter. A round more than 10% above the average of the earlier rounds gets `WARNING`, which is a reason to look at what the sessions did differently before launching another. Round 16 was 563 sessions at $0.199 each, $112.04 for the round, 19% under.

Judging is not in the ledger. `spend.py` reads run logs only, so the cost of a scoring pass is unrecorded.

# Replaying a checkpoint against a frozen store

A single pass confounds two things. The store an arrival meets is the product of everything before it, and the session that meets it is one draw, and when two runs disagree on a checkpoint nothing in either says which half it came from.

`replay` splits them. Putting the same arrival N times to one snapshot varies only the session. Putting it once to each run's snapshot varies only the store. The first says the recall is unreliable; the second says the stores differed, which is a capture question.

`--regenerate` rebuilds each snapshot's `CLAUDE.md` indexes from the node files before the session sees them — same store, new landing surface. That is how an index-format or instruction change gets measured against a frozen baseline. Leave it off when re-scoring a past run, which has to show the session the store it actually met; findability is decided by that surface, and mixing the two questions answers neither.

Replay runs sessions, so it runs under `lab` and is measured the same way they are — including the binaries: build first, or the mount is stale. `replay` records no revision of its own, so note the build's revision yourself when you keep the result. The build decides the prompt as well as the tools: from round 20 it opens "You are wanda.", so a replay against an earlier round's store either builds at that round's revision or is read as carrying that sentence too. `lab` mounts exactly one directory at `/work/runs`, and a replay must not write into the round it replays: by scoring time that directory holds `scored<run>.md`, which is the expectations, and the directory is mounted whole into whatever session runs against it next. Give the replay its own directory beside the round, with the results file and the snapshot repo copied in — `replay` finds the snapshot repo beside the results file it was given.

A results record carries the arrival's position, not the arrival: a run writes into the directory its sessions read, so the history is not written there. `--inputs` is where it comes back from, and it must be built at the anchor the records name or every date lands on the wrong day — `replay` compares both the anchor and the history digest and refuses rather than guessing. Records from a run made before that split carry their own arrival and need no `--inputs`.

```sh
mkdir -p runs/16A_replay/transcripts
cp runs/16A/report16A-results.jsonl runs/16A_replay/
cp -R runs/16A/report16A-snaps.git runs/16A_replay/
anchor=$(python3 -c "import json;print(json.loads(open('runs/16A/report16A-results.jsonl').readline())['anchor'])")
python3 lab/corpus.py docs/recall-corpus.md --anchor "$anchor" > runs/16A_replay/arrivals.json
LAB_RUN=16A_replay docker compose run --rm -T lab /work/lab/replay \
  --results /work/runs/report16A-results.jsonl \
  --inputs /work/runs/arrivals.json \
  --scene "Scene 10" --reps 3 \
  --out /work/runs/replay-Scene10.jsonl \
  2> runs/16A_replay/replay-Scene10.log
```

The arrivals go into the replay's own directory, which is what that container is given — and they are the history, so they belong to a replay of it and to nothing else. `--scene` is a substring and defaults to every checkpoint in the file. `--results` takes several files, which is how the cross-store comparison is run: one rep against each of 16A through 16D.

**Replay unlinks its `--out` and the two log files beside it before it starts**, so give each replay its own name rather than reusing one whose output you still want.

A replay record carries the same `input_id`, `anchor`, `history` and `snapshot` a run's record does, plus the name of the repo the snapshot came from, so `judge.py --results <replay output>` scores it exactly as it scores a run and refuses it on the same digest check. Score it from `tools`, with `--out` in the replay's own directory:

```sh
docker compose run --rm tools python3 lab/judge.py \
  --results runs/16A_replay/replay-Scene10.jsonl \
  --out runs/16A_replay/scored-Scene10.md
```

Once a directory has been scored it holds expectations, so do not run another replay in it.

# Rebuilding a store from its mem calls

`rebuild.py` answers a different question: what store would the same session decisions have produced under the code as it stands now.

A run leaves `<stem>-mem.jsonl`, one line per `mem` invocation in order, and `<stem>-sessions.jsonl`, which input ran under which session id. Rebuild replays that argv against a fresh vault with the same simulated date and session id each call had. What the sessions decided is kept; only the tool's behaviour is new — a changed frontmatter, a new rule, a fix.

```sh
docker compose run --rm tools python3 lab/rebuild.py --from runs/16A/vault
```

It runs under `tools` because it needs the repo, and writes by default to `runs/16A_debug/vault` — beside the round, not inside it, for the same reason a replay is. It expects exactly one `*-mem.jsonl` beside the vault it is given and refuses otherwise, which is why a run directory holds one run.

Ids are the one thing a replay cannot reproduce by itself: they are minted at random and a session's later calls name the ids its earlier calls got. So the original vault is the oracle — a node of the same kind and name made in the same session gets the id it had, and two nodes of one name in one session are told apart by the order `mem` printed `ok <id>` back in that session's transcript.

Read two things from the output. Every call whose exit code differs from the recorded one is listed, and that is where the current code disagrees with what the run did. The node-set difference against the original vault follows it — same id, new, missing.

What it cannot replay is a session that edited a file with the shell instead of through `mem`, which happens. Those do not show as exit-code mismatches; they show as bodies that differ.
