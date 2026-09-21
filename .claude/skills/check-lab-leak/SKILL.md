---
name: check-lab-leak
description: Before a run or a commit, read everything a lab session can reach and report anything carrying specifics of the scripted history
---

# What this is for

The lab replays a scripted history through sessions and scores what they recall. A session must not be able to read that history, or the thing being measured is the measurement.

Nearly all of what a session reads as prose is three files: `memory/templates/root.md`, `enrich.md` and `retract.md`. They are written into every vault and read on every arrival, and the memory system ships them outside the lab too, so nothing in them may be particular to these scenes. They are the substance of this reading.

The binaries mounted so a session can run `mem` carry string literals. Those are in scope and thin. Comments and docstrings are not in scope: a compiler does not put them in the file.

**Nothing else asks this question.** There is no mechanical check standing behind it: one was tried and removed, because deciding whether a passage is about these scenes by matching words guesses, and a check that half-works here is worse than none. Run this skill before a round, and before any commit that touches a template or the harness.

# What counts as a leak

Text a session can reach that carries something **particular to this history**:

- a person, place, organisation or thing from the cast, by name
- a detail of a scene — what happened, to whom, in what order
- an arrival quoted, or paraphrased closely enough to recognise
- a date from the history's own timeline
- a fact that only means something to someone who has read the corpus, including counts of what past runs did

# What does not count

**Behaviour guidance is not a leak, however closely its wording matches an expectation.** The standing instructions and the skills describe how to behave; the corpus's `should` and `should not` describe the same behaviour from the other side. The two converging is the experiment working as designed, not contamination. These three files also ship with the memory system outside the lab, where guidance is the whole point of them.

So a sentence like *"no refusal that reveals there is something to refuse"* is guidance and stays, even though a scene scores exactly that. The same sentence naming a character, or describing the scan appointment it came from, is a leak.

The question is never *does this resemble an expectation*. It is always *does this carry something only this history could have supplied*.

Also not leaks: ordinary domain vocabulary (node, edge, vault, recall, trajectory, arrival); format examples using placeholder names or dates outside the history's span; the harness describing its own mechanics without describing the history.

# What a session can reach

Do not assume — derive it.

1. The mounted binaries: `docker compose config --format json` and read `services.lab.volumes`. Every file bound into the container is in scope. A compiler drops comments and doc comments but keeps string literals, stored end to end with no separators — so `strings -n 6 <file>` gives runs of many literals joined, not one at a time. Read them that way and judge the prose. If a source file is on that mount list, stop: it carries the comments the build exists to leave behind.
2. The templates: `lab/bin/templates/*.md` if a build has run — that is the effective set, the product defaults with whatever variant the round selected resolved over them, and what the binaries read from beside themselves — and `memory/templates/*.md` otherwise. `root.md` becomes the vault's root `CLAUDE.md`; `enrich.md` and `retract.md` are written into `.claude/skills/`. `lab/bin/BUILD.json` names the variant and gives each one's sha256, so say which set you read. Also what `mem help` and `mem <verb> --help` print.
3. The run's own directory, mounted at `/work/runs`, with the vault inside it — so everything the run writes as it goes is one `ls ..` from a session's working directory. Its own past is by design; the history is not, and a run writes each arrival's position rather than its text, scene or speaker. Check that nothing from another round is reachable, and that nothing written there has started carrying the history again.

The history to check against is `docs/recall-corpus.md`. Read it, then read the surfaces above, then report.

# Reporting

One finding per passage: the file and line, the passage quoted, what specific thing it carries, and which part of the history that came from. Say plainly when there is nothing.

Do not report a passage because its wording resembles an expectation. When unsure whether something is guidance or a detail, report it and say which way you lean and why.

# Checking this skill still works

This skill is prose, so its behaviour drifts as the wording changes. Three revisions serve as a test set:

- `93064fb^` — before corpus content was removed from the mounted files. They hold 32 lines carrying cast names and quoted arrivals; a run should name most of them. This revision predates the build step, so the mounts are `.py` and the reading is of source.
- `HEAD` — clean. A run should report nothing, and in particular should **not** flag the confidentiality paragraph in `root.md`, which is guidance.
- `e46a8a6` — the commit that removed corpus content from the instructions. Its diff is a worked example of the distinction above.

Check out a point, run the skill, compare. When the wording here changes, run all three again.
