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

### 41. A `mem` call whose reader leaves is absent from the log, and now silently

`log` runs at `mem.rs:986`, after the command returns. A session that writes `mem show <id> | head -20` gets what it asked for and `head` then closes the pipe; `mem` dies mid-output and the call is never written to `LAB_MEMLOG`.

Measured across round 17 before the `SIGPIPE` fix, by matching every `mem show <ref> | head` in the transcripts against that session's own log entries:

```
   36  `mem show <ref> | head` invocations, across 28 sessions
   12  of them absent from the log, across 10 of 564 sessions
6,819  mem calls logged in the round
```

One piped call in three. It is not random: it is exactly the idiom a session reaches for on a long node, so the calls that vanish are the reads of the biggest nodes.

Until now the loss announced itself — the process died on a Rust panic, and the panic was in the transcript. That is what made the count above possible, and the fix for that panic has removed it: `mem` now dies quietly, so a call lost this way leaves no trace anywhere. **The number above cannot be retaken.**

The cost is that the mem log is the one record of what a run did with the store. `rebuild.py` replays from it, so a rebuild replays a sequence the run did not make. Every count of the form "N of 6,819 recorded calls" — including every "never observed" in the Deferred section below — is computed from it.

**Fix.** Write the log entry before the output is produced rather than after, or give `mem` an output path that treats EPIPE as a reason to stop rather than to die. The first is smaller and loses the exit code; the second keeps it.

## What a session cannot do, or does wrongly

`mem` accepts the call and does something other than what was asked.

### 8. After a rename, the old name mints a duplicate — and then resolves to neither

`Vault.by_name` (`store.py:563-575`) matches a node by its `aka`, so a former name still resolves. `mem._existing` (`mem.py:189`) — the duplicate guard behind `entity`, `event`, `pref` and `trajectory` — compares only the current label. So the name that `rename` promised would still work is exactly the name that creates a second node:

```
$ mem entity --kind place --name "Tony's" --summary "the trattoria on Fifth"
ok place:50a1dc
$ mem rename "Tony's" "Vesuvio"
ok place:50a1dc now named 'Vesuvio'
$ mem entity --kind place --name "Tony's" --summary "we ate here again"
ok place:4d7b22
$ mem show "Tony's"
("Tony's" is more than one node: place:4d7b22 (we ate here again);
 place:50a1dc (the trattoria on Fifth). Say which, by id.)
```

No `--new` was passed. The same happens to a trajectory restated in its original wording after a resummarise: two `[open]` threads for one undertaking, both in the directory index. This is the failure the standing instructions call the costliest — *"Two files for one person is the failure that costs most"* (`store.py:350`) — and the tool causes it.

No round-16 run ended with a duplicate of this shape. Those sessions rename by id and re-state by id, which hides the defect rather than removing it.

**Fix.** One `Vault.candidates(kind, name)` that matches label *or* `aka`, called by both `by_name` and `_existing`, so the two cannot drift again. An `aka`-only match should say so rather than silently update: *"Tony's is now place:50a1dc, named Vesuvio"*.

### 43. A dollar amount inside a double-quoted shell string reaches the store with its first digit eaten

A session drives `mem` through Bash, and it writes its arguments in double quotes. `"$200"` is a shell expansion: `$2` is the second positional parameter, empty in a `sh -c` string, so what `mem` receives is `00`. Nothing is quoted wrongly from the shell's point of view and nothing errors, so `mem` stores a figure that is off by a factor of ten and prints `ok`.

The call that did it, from 16D's mem log — note the body, where the session spelled the number out in words and it survived:

```
event --summary "fan texted Robin about the 00 hall-booking deposit"
      --body "fan (DM, 2026-06-02): texted Robin about the two hundred from the hall booking…"
```

Three sessions noticed and repaired the summary — 16D twice, 17D once — logging *"fixing dollar sign eaten by shell expansion"*. Repairing the summary does not repair what was written from it afterwards, and 16D's final vault still states the wrong amount in live body text, unstruck, in two nodes:

```
trajectories/a1f9bc.md:19  confirmed the 00 hall-booking deposit is still outstanding
trajectories/a1f9bc.md:20  fan says Robin's away until 9 Sep; no point chasing the 00 before then
trajectories/d3a0a0.md:14  fan texted Robin; deposit is 00 from the hall booking
```

The store is not wrong about something it was never told. It is wrong about a figure it was told correctly, and it says so as plainly as it says anything else.

Nothing `mem` can check after the fact distinguishes a mangled `00` from a real one. What it can do is show its work: `ok event:2026-06-02-c6f33a` says nothing about what was stored, so a session has no cheap way to see the loss. Printing the summary back on the `ok` line would have made every one of these visible at the moment it happened.

**Fix.** Echo the stored summary on the `ok` line of every verb that writes one.

## Instructions that are untrue, or that a cold reader misreads

A session has no context but what it is handed, and these are what it is handed.

### 25. The instructions list no way to write anything

*"`mem` is how you read the graph and write to it. Run `mem help` for the full list. The ones you will want:"* is followed by `recall`, `search`, `show`, `session`, `session --with`, `retract`, `rename`. **Not one verb that creates a node**, though the first and largest standing rule is *"Record what is true, greedily … write it down"*.

Verb counts from round 16 (four runs, 563 sessions, 6,406 calls):

```
show 2390  search 851  relate 620  recall 518  event 480  help 469
session 388  advance 231  entity 141  trajectory 119  rename 71  pref 55
forget 42  retract 31
```

The unlisted verbs that create nodes — `event`, `entity`, `trajectory`, `pref` — account for 795 calls; `relate`, which makes the edges, another 620. The two writers that got a line each, `retract` and `rename`, account for 102. **469 of 563 sessions ran `mem help` exactly once**, and none ran it twice: doing as the text says, and paying a tool round-trip per session to learn what the list could have told them.

**Fix.** List the verbs that create nodes. This is the one change here that should reduce tokens rather than add them.

### 26. The 80-character summary cap is stated nowhere a session reads it

*"A node has a one-line summary, which is all any index shows"* gives a model no number, and `mem` refuses rather than truncates. The cap appears in the per-verb `--help` and in the refusal message, but not in `mem help` — which is `ap.print_help()`, the subcommand list and nothing else — and not where the summary is described. Each failure costs a round-trip and a rewrite.

Over round 16's four runs, 6,406 `mem` calls: 175 exited non-zero, 79 of them writes, and **72 of the 79 were over-length summaries** — 20, 22, 16 and 14 across the four runs. Forty-eight of the seventy-two are within twelve characters of the cap:

```
81 81 81 81 81 81 81 82 82 82 82 82 82 82 82 82 83 83 83 83 83 83 83 84 84 84
85 85 85 85 85 86 87 87 87 87 87 87 88 89 89 89 90 90 92 92 92 92 …  145  163

$ python3 lab/mem.py help | grep -c 80
0
```

**Fix.** State the number where the summary is described.

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

### 28. "Names resolve wherever an id does" is false for `mem session`

The listing writes `mem session <id>`; the paragraph below it says names resolve wherever an id does, and then defines an id as the six-hex code in front of an index line. Composing the three as written gives `mem session <node id>`, which cannot work: `mem session` takes a Claude Code session UUID and nothing else.

```
$ mem session event:2026-07-07-9a0ded
(no session 'event:2026-07-07-9a0ded': the transcript is gone, or the id is not one)   rc=1
$ mem session fan
(no session 'fan': the transcript is gone, or the id is not one)                       rc=1
```

Round 16 pays for it in three runs of four. Five calls passed a node id where the session id goes — `session cefb2e` (a trajectory), `session event:2026-04-12-7a4edb`, `session event:2026-06-30-ec2c83`, `session event:2026-07-07-9a0ded`, `session --last 1 2026-08-15-401bf2` — and one passed `--with 665b8d`, person:fan's id, to a flag that wants a name. No session in any run passed a bare name.

**Fix.** Write the argument as `<session>`. The line already says where one comes from — a node's `made:` — so the placeholder is the whole of it.

### 29. "When it does, open a trajectory for it" reverses the rule

> Ask of anything new whether it is the end of something or the middle of it.
> Most information is mid-sequence: it implies something that has not happened
> yet. When it does, open a trajectory for it.

Three "it"s carrying two referents. A cold reader takes the nearest antecedent — *something that has not happened yet* — making it *"when the implied thing happens, open a trajectory"*, which is fluent and exactly backwards: a trajectory records what is outstanding, and one opened afterwards is never open at all.

```
$ sed -n '318,320p' lab/store.py
Ask of anything new whether it is the end of something or the middle of it.
Most information is mid-sequence: it implies something that has not happened
yet. When it does, open a trajectory for it.
```

**Fix.** *"When something implies an outcome that has not arrived, open a trajectory for it."*

### 31. Enrich step 4 tells a session to do what the cap refuses

The step says to put a constraint in the thread's own summary via `mem rename <id> --summary`. A real summary plus a constraint is over 80 characters:

```
$ mem rename trajectory:3c97bf --summary "Bellwood Dems canvassing weekend (mid-April) -- keep as mei's committee mail, don't bin"
(--summary is 87 characters; the cap is 80. It is what every index shows — say it in a phrase.)   rc=1
```

That call is one a round-16 session actually made. It got the constraint in on the retry by dropping the date the index line had been carrying: the node now says *"Bellwood Dems canvassing weekend -- mei's committee mail, keep not bin"* with *(mid-April)* pushed into `aka`, where no index shows it.

`rename` is the worst-failing verb of round 16 — 17 of 71 calls refused across the four runs, against 8.5% for `event` and under 1% for every read — and all 17 were an over-cap name or summary, between 82 and 117 characters.

**Fix.** Either raise the cap for a constraint or tell the step to replace the summary rather than extend it — noting that a replacement which fits in 80 has to drop something the index line was carrying.

### 32. The enrich skill tells a session to relate to a topic that does not exist yet

*"all of them `involves` one `topic:` node for the matter. Make the topic if it does not exist."* The only command form the step gives is `mem relate`, and it never says `mem entity --kind topic`. Combined with #17, "make the topic" produces a person — and naming the kind inline produces one with the kind in its name, because `_id_shaped` wants a local part of `[a-z0-9-]` and an apostrophe defeats it:

```
$ mem relate --subject trajectory:065af2 --rel involves --object "the September childcare gap"
ok trajectory:065af2 --involves--> person:97bdea        ← a person called "the September childcare gap"
$ mem relate --subject person:d7a42f --rel involves --object "topic:mei's reading"
ok person:d7a42f --involves--> person:90ea7e            ← a person called "topic:mei's reading"
```

The second form is what 16A did on 2026-06-24. It cost the session four `retract` calls and a `forget` to unwind — *"wrong object created by a quoting mistake; meant topic:3bb3ef"*. Every run did eventually reach `mem entity --kind topic` on its own, sixteen successful calls in all, but the skill is not what told them.

**Fix.** Give the `mem entity --kind topic` line.

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

### 34. The enrich skill's first step names a field that exists only in the lab

*"Look at what you wrote this session — the list you are about to put in `recorded`."* `recorded` is a field of the lab harness's output schema, defined in `run.SCHEMA` and asked for by `run.PROMPT`. The skill ships with any vault — `write_session_config` writes it verbatim into `.claude/skills/enrich/SKILL.md` — so outside the lab the reader cannot find it.

```
$ grep -rn 'recorded' lab/run.py | head -2
74:SCHEMA = { … "recorded": {"description": "one line per thing you wrote to memory" …
171:1. Look at what you wrote this session — the list you are about to put in
```

**Fix.** *"Look at what you wrote this session."*

## Found in round 16

### 38. `--by` is exempt from the date scrub, and a deadline computed from the real clock survives in the store

`--by` was exempted as "a date somebody stated and the session copied". Sessions also *compute* it, from a today they read off the system clock, and the result is a bare date the scrub cannot tell from a stated one.

Round 16 ran to a last arrival of 2026-09-10 with the container's clock at 2026-09-18. Every real date in prose was repaired — all four final vaults hold zero occurrences of 2026-09-18 — and two runs still carry a clock-derived deadline, both landing on a wrong today plus a week:

```
16D, arrival 2026-08-05:
  advance trajectory:1dc29a --by 2026-09-25 --note "… Wanda checked in on 2026-09-18 …"
  stored: note repaired to 2026-08-05; expect_by: "2026-09-25"
16C, arrival 2026-07-18:
  trajectory --summary "stair gate … expected next week" --by 2026-09-25 --body "… 2026-09-18 …"
  stored: body repaired to 2026-07-18; expect_by: "2026-09-25"   ← ten weeks apart, still open
```

The same stair-gate thread got 2026-07-25, 2026-08-12 and 2026-08-01 in the other three runs, so the scrub is what separates C from them. Reading the clock is not rare: between 21 and 35 distinct sessions per run put 2026-09-18 into a `mem` argument.

The cost is not the field. A deadline past the end of the history can never come due, so the one clause of the store's speak gate that would raise the thread — *"a date has gone by with nothing to show for it"* — is dead for the rest of the run. Run D's Scene 43 recall, thirty-four arrivals later, reads *"trajectory:1dc29a (mei's mum's health — expect_by 2026-09-25, not yet due)"*.

**Fix.** A date the session computed and a date somebody stated are different things and the flag cannot tell them apart, so either `--by` stops being exempt and a stated deadline near the system date is accepted as collateral, or `mem` refuses a `--by` outside the history's own span.

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
