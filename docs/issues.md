# Defects in the lab

Each is reproduced against the code as it stands. A count names the round it came from — round 16 unless the entry says otherwise — and a round is four runs of one configuration, so a thing seen once is noise and a thing seen in all four is signal. They are grouped by what they cost us, worst first within each group.

A file-and-line reference here names the Python that was the implementation when the entry was written — `store.py`, `mem.py`, `run.py`. The store and the CLI are Rust now, in `memory/`, and the harness in `lab/harness/`. The behaviour an open entry describes is still the behaviour; the line number is where it was found, not where it lives.

## Measurements we cannot trust

What a run reports is not what happened, so a band cannot be read at face value.

### 40. Two of the nine kinds are all but unexercised, so no round says much about them

The store has nine kinds (`memory/src/fm.rs:15`). Across eight runs — rounds 16 and 17, 1,127 sessions — a `group` is created in one run and a `thing` in two, and never more than a handful:

```
nodes in the final vault, by kind
                 16A   16B   16C   16D   17A   17B   17C   17D
  people          10    10    10    10    10     9    10    10
  places           4     5     4     5     4     8     4     6
  orgs             8     8     6     9    11     7    11     9
  groups           0     0     0     2     0     0     0     0
  things           0     6     0     0     0     6     0     0
  topics           4     5     5     3     7     6     8     3
  events         103    95    97   105   115   112   112   111
  prefs           13    15    13    13    13    15    14    13
  trajectories    29    30    26    28    31    33    35    29
```

A kind with no node has no directory and no index, so a session never meets one either: `lab/reach.py` over the same eight runs finds the groups index reaching one session of 1,127 and the things index reaching 35, all 35 inside the two runs that made a thing.

Nothing is broken. It means the lab measures seven of the nine kinds, and a verdict about how the store behaves is a verdict about those seven. The other half is that whether a thing or a group gets made turns on the wording one session happens to choose, so a round-to-round difference in either column is a draw and not a change.

**Fix.** Either the corpus gets scenes that need a group and a thing, or the two kinds come out of the store. Which of those is right is a question about the design, not about the lab, so it waits for one.

### 45. A replay puts the whole history, and the finished run, where its sessions can read it

The `score-lab-round` skill's replay recipe builds the arrivals with `corpus.py` and writes them to `runs/<run>_replay/arrivals.json` — every arrival's speaker, text and scene title, the whole history bar the expectations. That directory is what `compose.yaml` mounts at `/work/runs` for `LAB_RUN=<run>_replay`, and replay sessions are given `Read`, `Glob`, `Grep` and `Bash` with no path restriction (`session.rs:298-301`). So a replayed session can `cat /work/runs/arrivals.json` — including arrivals that come after the checkpoint being replayed. Replaying a scene's checkpoint, the file already holds the scene that resolves it.

This is the one thing a run is built to prevent. A run takes its arrivals on stdin so that they are not a file anything inside can open, and records positions rather than arrivals for the same reason; the recipe undoes both for replay. Its own prose shows the slip — *"they are the history, so they belong to a replay of it and to nothing else"* — which forgets that the replay's sessions are inside it. The replay's stderr log goes to the same directory and prints each scene title after every rep (`replay.rs:309-311`).

The same recipe copies two more things there, and it has to: `replay` looks for the snapshot repo beside the results file (`replay.rs:169`), and the container is given only the run's own directory. `cp runs/<run>/report<run>-results.jsonl` puts every checkpoint's answer and recalled nodes within reach, and `cp -R runs/<run>/report<run>-snaps.git` puts every store the run ever held — all 141 `before N` commits, the last being the store as the run ended. In a run those files grow one session at a time, so they only ever hold the past. In a replay they are the finished run, and a session replaying an early checkpoint can read the store as it stood months later with `git`, which the image has because `run` uses it.

Not reachable today: no `*_replay` directory exists under `runs/`. It is reachable the next time the recipe is followed.

Beside it, not a leak by `check-lab-leak`'s test but the same rule broken: a run's own stderr log, which the documented command sends to `runs/$LAB_RUN/run$LAB_RUN.log`, carries each finished arrival's date, speaker, a checkpoint mark and the first 44 characters of its text. A session can read the dates, speakers and text of past arrivals through its transcripts anyway. What the log adds is the history's length and the checkpoint mark: nothing a session is given says whether an arrival is a checkpoint, so the mark tells it which kinds of past arrival were scored. `peeked` would not count a session reading it: the instrument list matches the report stem and `/work/lab`, not the log.

**Fix.** The replay directory must hold nothing past the checkpoint being replayed. That means the arrivals on stdin, as `run` has them — `replay` reads `--inputs` once per results file, so it has to read them once before that loop — a results file cut to the records being replayed, and a snapshot repo holding only the commits those records name, built outside the container before it starts. The scene title in both stderr lines becomes the arrival's position, as `ident` already does. For the run's log: log positions only, with no checkpoint mark, or send it outside the run directory — `spend.py` and the `score-lab-round` skill read it from `runs/*/` and move with it — and add it to what `peeked` watches.

### 51. A session whose output is refused can end on a placeholder, and the checkpoint is lost

A session hands back its answer through Claude Code's structured output, which refuses a submission that does not match the schema. 19B, arrival 103, a checkpoint: the session wrote a full answer three times and each time put the `recorded` field inside the `answer` string as text — `…</answer>\n<parameter name="recorded">[…]` — so no submission had a `recorded` field, and each was refused; the first also carried a stray `"skill": "enrich"` field, left from the skill it had just loaded. Its fourth submission was `{"recalled": ["test"], "answer": "test", "recorded": ["test"]}`, which was accepted and ended the session.

The run flagged it — `placeholder output 2026-08-11: ["answer", "recalled", "recorded"]` — and judge leaves a flagged record unscored, so 19B is scored on 53 checkpoints and the other three round-19 runs on 54. The one other placeholder in rounds 18 and 19, 18D arrival 99, put `"placeholder"` in `recorded` in its only submission, which was accepted. It was not a checkpoint, so it cost nothing; had it been one, judge would have skipped it too, since it skips any flagged record whichever field was flagged.

**Fix.** When a session's final output is a placeholder and its transcript shows an earlier refused submission, resume it once (`claude -p --resume <sid>` with the same schema), with a message that names the mistake rather than repeating the refusal, which this session read three times without correcting. Detecting the refusal means reading the transcript: the tools log leaves out structured-output calls (`memory/src/transcript.rs:410`). At one checkpoint in two rounds, leaving it and reading the unscored line is also defensible.

## What a session cannot do, or does wrongly

`mem` accepts the call and does something other than what was asked.

### 8. A trajectory, event or rule restated in its wording from before a resummarise opens a second one

The entity half is fixed. A name that only a renamed node had is refused, with the node's id and its current name and the two ways on — `--id <id>` to update it, `--new` for another — because a name can be renamed away for being wrong, so writing there is unsafe, and minting beside it duplicates. A refusal where a name matches several nodes now names only moves that exist for that kind: `--id` where the verb has it, otherwise `rename` or `advance`, and `--new`.

The same shape remains for the kinds a summary names. `mem rename <id> --summary "…"` keeps the old summary as `~~was summarised: …~~`, and nothing resolves it, so a trajectory restated in its older wording opens a second thread, and a same-day event or a rule does the same. Rounds 16 to 19 restated a former summary 0 times in 2,526 `event`, `pref` and `trajectory` calls.

**Fix.** Refuse a restated former summary as a former name is refused, naming the node. For a trajectory or an event a guard hit is already a refusal; a rule's hit writes, so it needs one.

### 43. A dollar amount eaten by the shell in `--body`, `--expect` or `--because` is stored without being shown

A session drives `mem` through Bash and writes its arguments in double quotes, where `"$200"` is a shell expansion: `$2` is empty in a `sh -c` string, so `mem` receives `00` and stores it. From round 20 the stored index line and note lines are echoed after each write, so a mangled summary or note is on the screen. A body, an expectation or a reason is not, and a figure eaten there stays invisible. Rounds 16 to 19 have three such cases.

**Fix.** Echo what was stored for those too, or say once in the standing text that a `$` inside double quotes is expanded.

### 52. `mem` refuses an id written as `pref:`, as a directory path, or with a name after it

`mem show pref:8cb97b` prints `(no node for 'pref:8cb97b')` and exits 1, and so do `mem show 'person:600349 alex'` and `mem show people/600349`. `pref` is the verb that writes a preference, and sessions' own notes write `pref:<id>` too; which of these leads sessions to the shape is not established. In round 19, 7 calls failed on it — `show` 2, `relate --object` 5 — and round 18 had 8. No call was refused on the directory or id-then-name shapes in either round. From round 20 the judge reads all of these shapes; `mem` does not.

**Fix.** In `by_id` / `bare_ref`: accept `pref` and a kind's directory as the kind, and take a leading id before a name with no separator. It changes what sessions see, so it needs a round of its own.

## Instructions that are untrue, or that a cold reader misreads

A session has no context but what it is handed, and these are what it is handed.

### 27. "Recalling from everything returns everything" is false, and the truth is worse

`recall` caps at 14 rows and `search` at 10, and neither says anything was cut: `cmd_recall` slices `rows[:args.limit]` and `cmd_search` passes the limit into the query, and both then print what survives with no count of what did not. The instruction warns of a flood that cannot happen and stays silent about the silent truncation that does.

Against a copy of round 16A's vault (171 nodes), and the same in all four runs:

```
$ mem recall fan mei Robin Jane                  → 14 rows
$ mem recall fan mei Robin Jane --limit 200      → 169 rows
$ mem search "mei"                               → 10 rows
$ mem search "mei" --limit 200                   → 115 rows

16B  14 → 174 · 10 → 114      16C  14 → 161 · 10 → 101
16D  14 → 175 · 10 → 112
```

Recalling from four of the ten people in the store returns eight per cent of it.

**Fix.** Say that both cap their output and that `--limit` raises it; print how many were cut.

### 33. `--because` is undocumented, and discarded on an edge retraction and on `forget`

The retract skill says *"Give the same `--because` each time, dated, saying who corrected it and when"*. On an edge retraction and on `forget`, the reason reaches nowhere in the store: `cmd_retract` builds the sentence and then uses it only on a struck body line, and `cmd_forget` unlinks the file.

```
$ mem retract --subject person:d7a42f --rel parent_of --object 92e268 --inverse child_of \
      --because "fan corrected this on 2026-06-21; they are not related"
ok retracted 2
$ mem forget "a stray" --because "fan corrected this on 2026-06-21; it was never a thing"
ok forgot thing:8e0d57
$ grep -rn "corrected this" <vault>      → nothing
```

It survives in the session transcript, which is the belt — but the store, which is what a later session reads, keeps nothing. Across round 16 that is 47 reasons written and thrown away: all 20 of the successful edge-only retractions carried a `--because`, and 27 of the 42 `forget` calls did.

`--because` has no help text on any verb — `mem retract --help` and `mem forget --help` both show a bare `--because BECAUSE`. Where it *is* kept — `--line` and `rename` — `mem` already prefixes its own date, so following the skill literally produces the date twice:

```
~~was summarised: mei's mother~~ (resummarised 2026-09-10: fan corrected this on 2026-06-21)
```

**Fix.** Decide what `--because` is for now that a retraction removes rather than annotates; document it or remove it. Drop "dated" from the skill.

### 50. The exchange rule contradicts itself on a flag, and questions put to wanda are still filed

`root.md:9`: *"That someone asked you something, greeted you, or was told something is in the session transcript for a month, and is not a node; a node is what came out of it."* The next paragraph, `root.md:11`, says *"what you suggested, flagged, promised or did is as much a fact as what you were told"*, and the enrich skill's step 5 files *"a suggestion, a claim, or a flag — the fact of it, as an event"*. A flag is someone being told something, so on wanda's own tellings the standing text says two things. Sessions follow step 5 and file their flags; no example shows where a flag stops being an exchange.

On questions and greetings the two texts agree — `root.md:9`, and step 5's *"Nothing for a greeting, an acknowledgement, a question you were asked"* (`enrich.md:41`) — and sessions still file some:

```
final vaults, four per round                                     r16   r17   r18   r19
event nodes                                                      400   450   352   248
  summary opens "<someone> asked / messaged / DM'd / greeted wanda"  19    18    17     9
  the same phrase anywhere in the summary                         21    20    26    11
```

For example 18D *"Mei asked wanda whether anything was planned for her birthday"*. The count is a pattern on the summary: it misses exchanges that do not name wanda (17A *"mei asked what's on file for September holiday cover"*), and the second row catches some that lead with the fact and add the question after it (*"fan says he's booking Dara for both weeks of the nursery closure, asked wanda to log it as sorted"*). A broader pattern — any summary where someone speaks to wanda or she to them — gives 14 to 35 per store. Round 15 reported 30 to 55 per store (`docs/memories-implementation-plan.md:161`) with the same rule in force, by a method not recorded, so whether the habit has shrunk is not established.

Some of these carry something real. At arrival 141, 18A and 18D passed Jane's message on to fan; both had opened an undertaking to tell him, and took the "gone quiet" detail from the event (18A *"Jane DM'd wanda: fan's gone quiet on her, asked if he's alright and free the weekend of 3 Oct"*). 18B and 18C held the same event without the undertaking and did not pass it on.

**Fix.** Say the line in `root.md:9`, with a contrast in placeholders: a question or greeting is not a node; what it told you is, filed under the fact, with who said it in the body; what you yourself suggested, flagged or promised is, as step 5 says. Make `enrich.md:41` point to it rather than restate it.

## Found in round 16

### 38. A deadline counted from the real clock survives in the store

Sessions compute `--by` from a today they read off the system clock, and the result is a bare date the scrub cannot tell from a stated one: the scrub removes an exact match on the clock's date, and a count from it lands after it. Six such deadlines reached a store in rounds 16 to 19 — three in round 16, one each in 17B, 19A and 19B — at the clock's date plus 1 to 29 days. Twenty-three other deadlines in rounds 17 to 19 — stated dates, horizons a session chose, counts from the arrival's own date — also lie past the clock's date, most within two weeks of it, so no bound on the date tells the two apart.

A deadline past the end of the history can never come due, so the one clause of the speak gate that would raise the thread — *"a date has gone by with nothing to show for it"* — is dead for the rest of the run.

From round 20, `trajectory` and `advance` print how far an accepted `--by` lies from the story's today — `(--by 2026-09-30 is 113 days after today, 2026-06-09)` — so a count from the wrong today shows as a distance nobody meant. Nothing is refused or rewritten.

**Fix.** Read round 20 per clock-counted call: did the session that saw the line re-set the date. If the line does not move them, refusing or rewriting needs a bound `mem` can know, which it does not have today.

## Deferred

Each of these reproduces. None is being fixed, for one of two reasons: the trigger has not fired in four full rounds and a fix would cost machinery out of proportion to that, or a fix would change what a round measures and should be its own comparison rather than riding along inside another change.

The line under each heading is what the four round-16 runs show — 6,406 `mem` calls, 216 checkpoints, four finished vaults. It is there so the question does not get asked twice.

### 10. `mem search` mangles a query containing a quote character, silently

*Deferred as an edge case.* 4 of 851 recorded searches contained a quote character.

`cmd_search` (`mem.py:325`) builds `" OR ".join(f'"{t}"' …)` without escaping the terms. A quoted phrase becomes the FTS5 expression `""car" OR "seat""`, which parses as something else entirely and matches a different, smaller set; an *odd* number of quotes instead raises `unterminated string`, which the bare `except Exception` at `mem.py:333` swallows. Either way the session gets exit 0 and no sign that its query was not the query that ran.

```
$ mem search 'car seat'      → 10 hits   rc=0
$ mem search '"car seat"'    →  3 hits   rc=0   ← not a subset of the ten
$ mem search 'car "seat'     → (nothing) rc=0   ← the swallowed error
```

Straight at the index, `"car" OR "seat"` matches 15 rows and the mangled `""car" OR "seat""` matches 3.

`search` ran 188 to 225 times per run across the four round-16 runs and none of those calls passed a quote, so this is a latent defect rather than an observed loss. The wrong-answer branch is the worse half: a session that gets three plausible hits has no reason to look again.

**Fix.** Escape the terms, and report a query error as an error.

### 11. `mem retract --line` strikes every matching line in both nodes

*Deferred as an edge case.* 10 recorded `retract --line` calls.

The `if args.line:` block (`mem.py:549`) sits inside `for src, rel, dst in pairs` (`535`), so with `--inverse` it runs on the object's file too. The test is a case-insensitive unanchored substring over every line.

```
$ mem relate --subject Robbie --rel knows --object "Priya Rao" --inverse knows
ok person:2a060e --knows--> person:a3cee1
$ mem retract --subject Robbie --rel knows --object "Priya Rao" \
      --inverse knows --line "has not" --because "wrong"
ok retracted 4
$ grep -n "has not" people/*.md
2a060e.md: ~~He has not heard back.~~ (retracted 2025-08-03: wrong)        (intended)
a3cee1.md: ~~She has not heard from him.~~ (retracted 2025-08-03: wrong)   (a node named only as the far end of an edge)
```

`--line`'s help (`mem.py:716`) says "strike a body line containing this text".

Across the four round-16 runs `retract` ran 31 times — 10 with `--line`, 2 with `--inverse`, never both — so the combination has not been struck yet, on a verb that is used every round.

**Fix.** Apply `--line` to the subject only, and say so in the help.

### 12. `mem forget` leaves the node listed in its directory's index

*Deferred as an edge case.* 42 recorded `forget` calls, and 0 of the four vaults holds an index line naming a file that is not there. It bites only when the node forgotten is the last of its kind.

`regenerate_indexes` builds its directory list from the rows the index still holds (`store.py:877`), so a kind whose last node is gone is never rewritten.

```
$ mem entity --kind org --name "Bellwood Clinic" --summary "the clinic Jane uses"
ok org:d4d60d
$ mem forget "Bellwood Clinic"
ok forgot org:d4d60d
$ cat orgs/CLAUDE.md
# orgs

1 here. One line each, newest first.

- `d4d60d`  Bellwood Clinic — the clinic Jane uses
```

The root index correctly stops listing `orgs/`, so the stale file is reachable only by a session that goes looking — but `forget` exists precisely so a node that should never have existed leaves every surface.

**Fix.** Rewrite or delete the index of a directory that has become empty.

### 14. A read probe creates the directory it asks about

*Deferred as an edge case.* Cannot fire in a run: `seed` creates all nine kind directories before the first session, so the mkdir is always a no-op.

The wrong-case half of this entry is closed. `Vault::by_id` lower-cases the ref and probes with the lower-cased string, so `place:E1F872`, `PLACE:E1F872` and `Place:e1f872` all resolve to `place:e1f872`, and an unknown kind is refused before anything touches the disk — `known && self.exists(&lower)` short-circuits. No capitalised directory can be created any more.

What remains is that asking where a node *would* live creates the directory. `path_for` mkdirs the kind directory before checking whether the file is there, so a read probe for a kind with no nodes leaves an empty one behind:

```
$ ls -A            (a bare vault)
$ mem show "place:aaaaaa"
(no node for 'place:aaaaaa')          rc=1
$ ls -A
places
```

It cannot fire in a run. `seed` creates all nine kind directories before the first session, so every one already exists and the mkdir is a no-op. Kept as it is deliberately through the port: the behaviour is in every run that has been scored, and the code says so where it lives.

**Fix.** Separate asking where a node would live from making room for one. Only the write path needs the directory.

### 15. `mem entity --id <id> --name "New Name"` prints ok and keeps the old name

*Deferred as an edge case.* 22 recorded calls passed `--id` together with `--name`. All 22 named the name the node already had, so no rename was lost.

`upsert` uses `meta.setdefault("name", …)` (`store.py:643`), so an existing name is never overwritten — but the command reports success, and the summary in the same call does land.

```
$ mem entity --kind person --id 1e49a2 --name "Robin Vance" --summary "the neighbour, full name"
ok person:1e49a2
$ mem show 1e49a2
name: "Robin"                        ← unchanged
summary: "the neighbour, full name"  ← changed
```

`--id`'s help is "update this node, when two share the name". One verb along, `mem rename 1e49a2 "Robin Vance"` does the whole job — the new name, the old one in `aka`, a note in the body. Nothing points a session from one to the other, and nothing in the `ok` says half the call was dropped. No round-16 session met it: all twenty-two `entity --id` calls across the four runs passed the name the node already had.

Since round 20 a former name refused by `entity` points a session at `--id`, which is this path; the echo after the `ok` line shows the name that was kept.

**Fix.** Either apply the name (and record the old one in `aka`, as `rename` does) or refuse and point at `mem rename`.

### 16. `relate` normalises the relation, reports the raw one, and `retract` matches only the stored form

*Deferred as an edge case.* 0 of 620 recorded `relate` calls used a multi-word relation.

`relate` passes `--rel` through untouched; `fm_dump` writes it under `rel_key(rel)` — lower-cased and underscored — while the line printed back says `--{args.rel}-->`. `retract` compares the raw flag against what was stored.

```
$ mem relate --subject Jane --rel "mother of" --object Harborview
ok person:44fd95 --mother of--> person:1bac93     ← what the session sees
                 mother_of: ["[[1bac93]]"]        ← what is written
$ mem retract --subject Jane --rel "mother of" --object Harborview
(nothing matched, nothing retracted)   rc=1
$ mem retract --subject Jane --rel mother_of --object Harborview
ok retracted 1
```

A session unsaying an edge from its own record of the command it ran — which is what the transcript shows it — fails, and the false claim stands. `rel_key`'s other rewrite is harmless: the `rel_` prefix it puts on a relation that collides with a frontmatter field is stripped again on read, so `--rel summary` retracts. Case and spacing are what get lost. Round 16 never paid for it — all 673 relations the four runs passed to `relate` were already snake_case, and every `retract` used the stored form.

**Fix.** Normalise in `retract` too, and report the stored form.

### 17. `mem relate` mints a *person* for any name it does not know

*Deferred as an edge case.* 620 recorded `relate` calls, and 0 of the four vaults holds a person node with no summary.

`_refs(…, "person")` is the fallback for both ends of an edge (`mem.py:417`), so a name that is plainly not a person becomes one. The sibling verbs have since been given better fallbacks — `trajectory --about` mints a thing, `pref --about` a topic — and `relate` still mints people.

```
$ mem trajectory --summary "the car seat has not arrived" --expect "it arrives"
ok trajectory:0a6d18
$ mem relate --subject "the car seat has not arrived" --rel involves --object "the car seat order"
ok trajectory:0a6d18 --involves--> person:3c92ef    ← a person called "the car seat order"
```

`mem relate --help` shows `--subject SUBJECT` and `--object OBJECT` with no help text at all, so nothing warns that an unknown name is minted, or as what.

No round-16 run paid for it: all four end with the same ten people, and the per-session snapshots show no other person node ever existed in any of them. The node it cost round 15A is still in `vault15A` — `people/4c0cdd.md`, renamed by the session that made it to *"stray node (void) — wanda error 2026-07-21; not a person"*, because the edge could be retracted and the node could not.

**Fix.** Refuse to mint from `relate` — an edge should join things that exist — or take a `--kind` for each end.

### 18. `_date_or_die` accepts impossible dates, and the date is the id

*Deferred as an edge case.* 0 of 6,406 recorded `mem` calls passed a `--when` that is not a real date.

The check is shape-only: `\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?` (`mem.py:452`).

```
$ mem event --summary "month thirteen" --when 2026-13-01   → ok event:2026-13-01-8613c6
$ mem event --summary "impossible day" --when 2026-02-30   → ok event:2026-02-30-da328a
$ mem trajectory --summary "probe" --expect x --by 2026-02-30
                                                           → ok trajectory:515f5a, expect_by: "2026-02-30"
$ head -5 events/CLAUDE.md
# events
105 here. One line each, newest first.
- `2026-13-01-8613c6`  month thirteen     ← above 2026-09-17, the newest real event
```

An event's date is its id, and every index sorts newest-first on that string, so the impossible date is not merely stored: it is the first line of the first thing a session reads. Nothing downstream refuses it either — `mem recall` finds the node and prints it like any other.

**Fix.** Parse it rather than matching a shape — `datetime.fromisoformat`, which keeps the optional time the present check accepts.

### 22. `spend.py --record` ignores a re-run of a round it already holds

*Deferred as an edge case.* An operator's action rather than a session's, and it has not been hit.

`record()` keys the ledger on `(round, run)` and skips any log whose key is already there, so a re-run keeps the stale cost and the stale lab sha and prints "recorded 0 run(s)" — which reads as "already up to date". `main()` applies the same filter to the unrecorded-runs it folds in for display, so the fresh log cannot reach the table by that route either. Every later reading — the trend table, and `check()` against it — then compares against a number that no longer describes anything.

Against a scratch copy of the ledger and `runs/16A/run16A.log`, with one session's cost raised and the stats line's `lab` moved on a commit:

```
$ python3 lab/spend.py --record
recorded 0 run(s) into spend.jsonl

$ grep '"round": 16, "run": "A"' lab/spend.jsonl
{... "cost": 29.14, ... "lab": "93064fb"}       # the log now says 38.14 at 4a18e60

$ python3 lab/spend.py | tail -1
   16     4       563      0.199     17.9    112.04        -19%  93064fb

>>> spend.check(spend.read_log(Path("runs/16A/run16A.log")))
WARNING spend: $0.270/session, $38.14 for the run · earlier rounds average
$0.245/session (+11%) — over by more than 10%
```

The warning the run has earned is the one the ledger suppresses.

**Fix.** Replace the row when the log is newer, or refuse and say why.

### 23. The auto-memory leak check looks in the wrong directory

*Deferred as an edge case.* Never fires on the paths in use: `/work/runs/vault` encodes identically under both schemes, which is the directory every round has written.

`run.py:476` computes Claude Code's project directory as `str(vault_path).replace("/", "-")`. Claude Code replaces **every** non-alphanumeric byte (`e.replace(/[^a-zA-Z0-9]/g,"-")`). So on any path containing a dot, space, underscore or other punctuation, the check looks at a path that does not exist, finds nothing, and reports the run clean. It fails open. `transcript.py:33` and `judge.py:146` carry the same expression.

Auto-memory is off by the environment flag now, and `run.py`'s check is the only thing that would notice the flag ceasing to work — so this is a fail-open check on the alarm itself.

Confirmed in the wild on this machine, and latent in `judge.py`'s own isolation directory:

```
$ ls ~/.claude/projects/-Users-fanzhu--wanda-workspace/
0c7be2f7-….jsonl  9a924c2c-….jsonl  memory
$ head -1 ~/.claude/projects/-Users-fanzhu--wanda-workspace/0c7be2f7-….jsonl
{... "cwd": "/Users/fanzhu/.wanda/workspace" ...}
   the lab would look in  -Users-fanzhu-.wanda-workspace   (does not exist)

>>> d = Path(tempfile.gettempdir()) / "lab-judge-cwd"      # judge.isolated()
/private/var/folders/80/qwrdqr5x7rd5_jkqp6xnzkt00000gn/T/lab-judge-cwd
   judge rmtree targets  …-qwrdqr5x7rd5_jkqp6xnzkt00000gn-T-lab-judge-cwd
   Claude Code writes    …-qwrdqr5x7rd5-jkqp6xnzkt00000gn-T-lab-judge-cwd
```

The paths used so far encode identically under both schemes — `/work/runs/vault` gives `-work-runs-vault` either way, which is the directory round 16 actually wrote — which is why no run has been affected.

**Fix.** Use Claude Code's encoding in all three places.

### 36. Two `mem` processes in one vault collide on `.index.db`

*Deferred as an edge case.* 0 of 6,406 recorded calls shared a millisecond with another, and the harness runs its sessions one at a time.

`build_index` (`store.py:706`) unlinks and rebuilds `<vault>/.index.db` on every `mem` call, and nothing serialises it — there is no lock anywhere in `lab/`. Two concurrent calls in one vault race: one unlinks while the other is writing.

```
$ for i in $(seq 1 20); do mem recall mei & mem recall sarah & wait; done
   21 of the 40 calls died:

  File "lab/store.py", line 710, in build_index
    con.executescript(SCHEMA)
sqlite3.OperationalError: table nodes already exists
```

The lab runs four containers in parallel but each has its own vault, so no run has been affected. Nothing reads a pre-existing `.index.db` — every caller rebuilds it, and `run.py` excludes it from snapshots (`run.py:506`) — so it never needs to be a file at all.

**Fix.** Build it in memory. That also stops writing a file into the vault on every read command, where sessions can see it.

### 13. `mem search` cannot find a node by a former summary

*Half fixed.* A former **name** is now in the indexed text, so an entity found by the name it used to have is found by `search` as well as by `show` and `recall`. A former **summary** is not, and this is the remaining half.

The FTS row is `label + summary + live body + former names` (`memory/src/index.rs:100-107`). `live_body` strips the struck lines, so `~~was summarised: ...~~` is searchable nowhere.

Whether it should be is a real question rather than an oversight. A struck line from `retract --line` is a claim that was never true, and surfacing it again is the one thing retraction exists to prevent. A former summary is not that — it is a true statement about what this node used to be called — but the two live in the same syntax, and only the `was summarised:` verb tells them apart. Indexing on that verb is the fix if it is wanted.

```
$ mem rename "Tonelli" "Vesuvio" --because "renamed after the sale"
ok place:e1f872 now named 'Vesuvio'
$ mem search "Tonelli"
(nothing)
$ mem recall "Tonelli"
  1.00  seeds=1 hop=0  `place:e1f872`  Vesuvio — the trattoria on Fifth
```

The node still resolves by the old name — `show`, `recall` and the Obsidian `aliases:` field all honour it — and the verb the instructions offer for looking things up denies it exists.

**Fix.** Put `aka` in the FTS row.

### 37. The 0.6 convergence multiplier is inert across the printed list

*Deferred because a fix moves the measurement.* Setting the multiplier to zero changes the printed order in 6 of 146 multi-seed recalls.

With the walks independent (#2), every node in a multi-seed call's top rows is reached by every seed, so `1 + 0.6 * (reached - 1)` (`mem.py:297`) is one constant over the whole list and orders nothing. The sort is `(hop, -score, id)`, so the only thing the term could still decide is a tie inside a hop band, and there the constant cancels too.

Replaying every multi-seed `recall` round 16 made before its session's first write — 146 calls across 16A–16D, each against the vault as it stood at that session's snapshot — the printed list carries a single `seeds=` value in 138 of them. Setting the multiplier to zero changes the printed order in 6 of the 146, and always in the last rows.

```
$ mem recall fan mei | grep -o 'seeds=[0-9]' | sort | uniq -c
  14 seeds=2          all fourteen printed rows, one value

   with the term:  16.00  16.00  11.59  8.06  7.41  7.38  …
   without it:     10.00  10.00   7.25  5.04  4.63  4.61  …
   every score scaled by 1.6, and the same fourteen rows in the same order
```

This is a consequence of fixing #2, not a defect in it — the old code's convergence differences were an artefact of seeds blocking each other. But the term was tuned when it did something.

**Fix.** Decide what convergence is for now that the walks are independent: drop the term, or count the paths that arrive rather than the seeds they came from, so that a node two seeds reach by four routes outranks one they reach by two.

### 39. `mem recall <person>` cannot reach a standing preference at the limit sessions use

*Deferred because a fix moves the measurement.* Any fix rewrites the hop-one band, which is most of what a recall prints.

The sort key is `(hop, -score, id)`, so a preference attached to a person lands in the hop-1 block — but that block is 86 to 93 nodes long, and inside it a node scores `DECAY × (1 + 0.5 × lateral neighbours)`, times 1.4 when open. A preference joined to exactly one person by `whose` and nothing else has no lateral neighbours and no status, so it takes the bare floor of `DECAY = 0.45` while every event sits at 1.12 and every open trajectory at 1.26 and up.

```
$ MEM_VAULT=<16A vault> mem recall fan              0 preference rows of 14
$ MEM_VAULT=<16A vault> mem recall fan --limit 400  13 preference rows, the first at rank 59
```

It is not particular to fan or to one run. Over the nine named people in the four round-16 vaults, 30 of 36 recalls return no preference at all in the default 14 and the other 6 return exactly one — never two — out of stores holding 13 to 15. In 16A the preference the Scene 21 checkpoint asks for, *"wants wanda to keep surfacing things unprompted"*, scores 0.45 at rank 95.

That expectation — fan's preference for being told things before he asks — is a hit in one run of four, and that run did not get there through recall: its tool trace at the checkpoint reads `mem recall fan`, then `ls prefs/; cat prefs/CLAUDE.md`. Re-run against that session's own snapshot, its `recall fan` returns no preference in its fourteen rows.

A preference is the thing least likely to be re-stated and most likely to govern what wanda should do, so the ranking is upside down for exactly the nodes that most need to arrive unprompted.

**Fix.** Either preferences get a multiplier of their own, as open trajectories do, or a preference whose `whose` names a seed is pulled in regardless of rank.
