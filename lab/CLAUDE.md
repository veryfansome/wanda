# The memory lab

A scripted household history — `docs/recall-corpus.md`, 141 arrivals across 45 scenes — is replayed through headless Claude Code sessions, one session per arrival, each with a vault of markdown notes as its working directory and a `mem` CLI as its only way into the store. 54 of the arrivals are checkpoints carrying expectations, and `judge.py` scores what each of those sessions recalled. Four runs of one configuration make a round. `docs/memories-implementation-plan.md` says what this is for and what it has established.

Procedure lives in skills, invoked deliberately. `run-lab-round` launches a round and watches it through; `score-lab-round` judges one, and covers replay and rebuild; `read-lab-round` reads a judged round per expectation and checks how the sessions behaved; `check-lab-leak` reads everything a session can reach for anything of the history in it. This file is the reference and the invariants.

## The files

| file | what it is |
|---|---|
| `corpus.py` | Parses the history into arrivals — date, channel, speaker, text, scene, position — and prints them as JSON. Shifts every date so the last one lands short of today |
| `judge.py` | Model-as-judge scoring over a run's results, each checkpoint against the store it met. Separates relevance (`should` / `should not`) from discipline (`budget`) |
| `lint.py` | Checks each checkpoint against the history before it — is this still a fair test. Its structural pass catches an arrival or an expectation that did not parse, a checkpoint with nothing to measure, and a marker that can drift |
| `rebuild.py` | Rebuilds a run's store from its recorded `mem` calls under the current code, using the original vault as the id oracle, and lists every call whose exit code now differs |
| `build.py` | Compiles the crate, stages the three binaries a session can reach into `lab/bin/`, resolves `memory/templates/` and any `LAB_VARIANT` into `lab/bin/templates/`, and prints the revision. Runs in `builder`, because that is where the toolchain is |
| `obsidian.py` | Writes a vault's `.obsidian/graph.json`: one colour group per kind, the indexes filtered out. A harness-built vault has this already |
| `spend.py` | What a round costs, round over round, from Claude Code's per-session estimate; `--record` appends to `spend.jsonl` |
| `runs/spend.jsonl` | The cost ledger, one line per finished run. Under `runs/` with the readings, so clearing that clears the trend |

What runs a round is Rust, in two crates. `memory/` is the store and the `mem` CLI a session drives — markdown with YAML frontmatter, a derived SQLite/FTS index, the generated `CLAUDE.md` surfaces, and the projection of a session's transcript that `mem session` shows. `lab/harness/` is the instrument: `run` replays the arrivals one session each, snapshotting the store before every one, and `replay` puts one arrival to a frozen store N times to split a recall failure from a capture failure. The Python left here reads a finished run, or prepares one.

The `mem` verbs are `recall search show entity event relate pref trajectory advance rename forget retract session help`.

## The layout

`lab/` is the instrument and `runs/<name>/` holds every reading. That split is what lets the container be handed single files rather than a directory: a run writes only into its own `runs/<name>/`, which is the one writable thing mounted.

`runs/` is gitignored whole, so nothing a run produces is committed. These are smoke tests — run them, read them, act on what they show, and let the plan document record the finding.

A run launched with `--vault /work/runs/vault --out /work/runs/report16A.md` and `LAB_RUN=16A` leaves:

```
runs/16A/report16A.md                    the report: each checkpoint's recall, answer and trace
runs/16A/report16A-results.jsonl         one record per checkpoint — judge.py and `replay` read this
runs/16A/report16A-sessions.jsonl        one record per arrival: its session id, answer, what it recalled
runs/16A/report16A-tools.jsonl           every tool call, derived from the transcripts
runs/16A/report16A-mem.jsonl             every mem invocation, logged by mem itself
runs/16A/report16A-snaps.git             a bare repo, one commit per session
runs/16A/vault/                          the store as the run ended — from --vault, NOT --out
runs/16A/transcripts/-work-runs-vault/   every session's transcript, mounted out of the container
runs/16A/run16A.log                      the run's own stderr, and its last line is the run's cost
```

The mem log has one line per invocation: the arguments as `mem` received them, after the shell, its exit code, and the date and session it ran under. From round 20, `"cut": true` marks a call whose reader left before the output ended, as `head` does, and the call still ran to the end; a `--help` answer is logged as `help` with the verb in its arguments; and a call the argument parser refused has exit code 2, which `mem` itself never returns. Earlier logs have none of these — about a hundred calls a run in round 19, and a few piped ones — and `help` there is only the bare listing.

`judge.py` puts `scored16A.md` beside them. `rebuild.py` writes to `runs/16A_debug/vault` — beside the run, never inside it, because a run's directory is mounted whole into whatever session runs against it next.

## The three services

`lab` runs the sessions under test. It mounts three binaries read-only — `mem`, `run`, `replay` — the three templates at `lab/bin/templates`, which is where each binary looks for them, plus `runs/$LAB_RUN` at `/work/runs` and `runs/$LAB_RUN/transcripts` at `/home/lab/.claude/projects`, and its working directory is `/work/runs`. The history, the expectations, `.git`, every earlier round and every earlier round's transcripts are not mounted, so nothing inside can reach them by any spelling. `LAB_RUN` defaults to `scratch` rather than failing, so a forgotten one lands in its own directory instead of a round's.

`tools` mounts the whole repo at `/work` and is for what only reads a finished run: judge, lint, rebuild, spend. None of them starts a session against a vault, so the repo costs nothing. Judge, rebuild and obsidian read the store through `memory`, a Python module built from the Rust crate. They look for it in `lab/bin`, beside the templates the round resolved — which is where the module reads the vault's standing texts from.

`builder` is the build, on the stock Rust image rather than the lab image. Rebuilding the lab image also pulls a newer Claude Code CLI, which is most of a session's standing context, so a crate change and a CLI change would arrive together and a reading that moved would not say which moved it. It compiles into `target-linux/` and stages what it produced into `lab/bin/`, which is the one command a round needs:

```
LAB_REV=$(docker compose run --rm -T builder python3 /work/lab/build.py) || exit 1
```

The same command on a host checkout builds for the host. That is useful for the module and useless for the three binaries, which only ever run in the lab image — where one built for another platform fails to exec, loudly, at the first session.

The home directory in the image is empty. Nothing of the host's `~/.claude` gets in — no global `CLAUDE.md`, no `settings.json`, no plugins, no plugin skills. A session still sees Claude Code's own built-in skills, because those ship in the CLI; that is the tool, not a leak.

## The invariants

Every one of these was a defect first.

- **The vault is its own git repo.** Claude Code resolves `CLAUDE.md` and auto-memory from the enclosing repo, not from the working directory, and `--setting-sources project` does not change that. Without `git init` in the vault, sessions load this project's memory index and write to it. `run`, `replay` and `judge.py` all do it.
- **Auto-memory is off.** Claude Code instructs every session, headless included, to keep a persistent memory of its own in a directory that is not the vault — a second memory system beside the one under test, in her context at every turn. `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` removes the block, and `run`, `judge.py` and `lint.py` all set it. `run` also clears that directory before every session and checks it once the run ends; note that the end check can only see the last session's writes, so it catches a persistent leak, not a single one.
- **The history is parsed outside the container and the arrivals are piped in.** `corpus.py` emits date, channel, speaker, text, scene and position, and nothing else — `run` never sees `should`, `should not` or `budget`. Because the arrivals arrive on stdin they are not a file anything inside can read. That is why `-T` is required and `-d` cannot be used: a detached container gets no stdin and the run blocks forever rather than failing.
- **A checkpoint is judged against the store it met**, not the vault as the run ended. `judge.py` materialises each record's own snapshot from the snaps repo and renders the recalled nodes from that. Scoring against the end state reads later sessions' writes back into the checkpoint. A candidate repo counts only if it holds that record's commit, so a wrong `--snaps` or a stale repo under the right name cannot quietly send a whole pass to the fallback vault.
- **The expectations are joined back by the arrival's position, against a history digest.** A scene name is shared by several arrivals and a date moves with the anchor, so neither identifies one. `judge.py` refuses to score records whose history digest does not match `--corpus`, rather than producing a partial tally that reads as a whole one.
- **`--out` decides every derived path except the vault.** The results, sessions, tools and mem logs and the snapshot repo all take its stem, so two runs of one configuration never collide. The vault comes from `--vault`, and `run` deletes whatever is there at startup. Always pass both.
- **A run records the position, not the arrival.** The run directory is mounted whole at `/work/runs` and the vault is inside it, so everything a run writes as it goes is one `ls ..` from a session's working directory. The sessions and results logs carry `input_id` and what came back; `prior` carries the positions of the earlier arrivals in the scene and what wanda answered them; the snapshot messages carry the position alone. `judge.py` joins the arrival back from the history, against the anchor and the history digest the record names, the same way it already joined the expectations, and `replay` takes the arrivals as `--inputs` and refuses a payload built at another anchor. Records written before this carry their own and are left alone. `report<run>.md` still carries scene and text and is written after the last session ends.
- **A run directory is used once.** A run refuses to start where anything already exists, because what lands in a run directory afterwards is a scored report and a finished vault, and the directory is mounted whole into any session run against it later. Two exceptions: `transcripts`, which is a mount point and has to exist first, and an empty file — the run command sends its own stderr to a log in here, and the shell creates that before the container starts.
- **The transcript is the belt, and the vault holds no copy.** Claude Code keeps it at `~/.claude/projects/<vault path, slashes to dashes>/<session id>.jsonl` for `cleanupPeriodDays`, thirty by default, and then it is gone. What the vault holds is the pointer: `MEM_SESSION` goes in as `made:` on every node written. `mem session <id>` projects it back.
- **The tool log is derived from each session's transcript, not collected by a hook.** A `PostToolUse` hook only fires for a call that succeeded, and an attempt that was refused is the one thing such a log exists to show. There is no hook and no `settings.json` in the vault any more.
- **Every path in the child's environment is absolute.** The session's cwd is the vault, so a relative `MEM_VAULT` silently creates a second vault inside the first. Nothing errors; the writes go somewhere the scorer never looks.
- **The session's environment is `MEM_VAULT`, `MEM_DATE`, `MEM_SESSION`, `LAB_INPUT`, `LAB_MEMLOG`.** `LAB_INPUT` is an opaque hex key, only ever compared for equality, so nothing in the environment says which arrival this is or where it falls in the sequence.
- **Three more variables exist, and none of them is a session's.** `MEM_TEMPLATES` names the directory the vault's standing texts ship from, for running a binary straight out of a cargo target directory rather than out of `lab/bin` where they sit beside it; a template that cannot be read is an error, not an empty instruction. The other two are `rebuild.py`'s, and both make a replay reproduce what a run did rather than what today would do: `MEM_REAL_DATE` gives each recorded call the date it originally ran on, because the scrub takes today's date out of what a session passes and a replay a day later would take out something else; `MEM_ORACLE_ORDER` gives the order each session minted its ids in, so two replays of one run produce the same ids and a difference in behaviour can be told from a difference in the draw.
- **The simulated date goes in `--append-system-prompt`.** In the user prompt it reads as something the user believes and loses to the real date, which reaches the session as an attachment on the first user turn. `mem` also rewrites the real date out of every string a session passes, at one choke point after parsing — an exact match on today and nothing near it, because a near-miss window caught the story's own dates instead. `corpus.py` shifts the history by whole weeks and always lands it short of the anchor, never level with it, so a date equal to the system date was read off the clock rather than taken from the story.
- **Both `--tools` and `--allowedTools`.** The first exposes them, the second grants them. With only the first, sessions run to completion under `--permission-mode dontAsk` and write nothing.
- **`mem recall` is a function of the store and the set of refs, and it was not.** Every set the walk iterates is now iterated in sorted order, each seed walks with its own `hop_of`, and the node id is the sort's last key so the order is total. Before that, CPython's per-process string hashing made the same command on the same store return a different answer run to run.
- **`run` checks at startup that the projection reads back what the prompt writes.** The prompt a session is handed and the parser that reads it out of a transcript are one shape written twice; edit them together, or the harness refuses to start.
- **The path is the id.** `events/2026-09-07-e6799d.md` is `event:2026-09-07-e6799d` — the directory is the kind, the stem is the rest, and nothing in the file repeats either. Ids are opaque and minted at creation; names are labels that resolve by lookup, and a name two nodes carry is refused with the candidates shown.
- **The texts a session reads in its vault are files, not constants.** `memory/templates/root.md` becomes the vault's root `CLAUDE.md`; `enrich.md` and `retract.md` become its skills. They sit outside `lab/` because the memory system ships them wherever it runs, and the lab is one consumer. An experiment never edits them: `lab/variants/<name>/` holds only the files that differ, `LAB_VARIANT` selects it, and `build.py` resolves the two into `lab/bin/templates/`, recording each result's sha256 in `BUILD.json`. A report then names the texts its sessions met by content rather than by commit, and two runs of one round can carry different variants.
- **A session is handed a binary, not source.** A comment or a docstring is prose written by someone who knows the history, and the only way to keep it out of reach is for it not to be there. A compiler drops both; string literals survive, stored end to end, which is what `check-lab-leak` reads. Never mount a source file into `lab`. The three binaries go in one directory because each looks for the others, and for the templates, beside itself.
- **`LAB_REV` is given, not asked for, and it comes from the build.** `git rev-parse` cannot answer inside the container, and a report that cannot name the code it came from is not evidence of anything. `build.py` compiles, then prints the revision and nothing else, and the run command takes it from there, so the only way to have a revision is to have compiled and what compiled is what mounts. It also records the digest of every source file it compiled, so a build followed by an edit — which leaves a tree whose revision still reads clean — is a finding rather than a surprise. Assign it, check the status, then export — `export LAB_REV=$(...)` on one line reports export's status and not the build's, so a failed build would run yesterday's binaries under today's label. `run` exits rather than warning when `LAB_REV` is missing or `unknown`, and it checks before claiming the run directory, so an abort does not spend a directory that is used once. A tree with edits in it builds as `<rev>-dirty`, naming something nobody can check out.
- **The standing instructions are wanda's, not this corpus's.** `root.md` and both skills ship with any vault, so they carry no names and no worked examples — an example in a permanent instruction is a prior on every arrival. Whether a passage carries something particular to this history is a judgement, and `check-lab-leak` is what makes it — reading everything a session can reach, against the history. No mechanical check stands in for it: the ones that tried guessed at prose, and a check that half-works on a question like this is worse than none. What a run reads of its own tooling it counts itself, and reports as `peeked`.
- **Session persistence is on for wanda's sessions and off for the judge's and lint's.** Hers are the belt; theirs are nobody's.
- **Model: `claude-sonnet-5`**, set in `run`, `judge.py` and `lint.py`. Every number in the docs was produced with it.

## Adding or changing a scene

The history is one file. `docs/recall-corpus.md` holds the arrivals and the expectations, it is not mounted into any session, and nothing about a scene belongs anywhere else — not in a comment explaining why a function exists, not in a docstring's worked example, not in a template. A comment gives the reason for the code; the scene that prompted it is the one thing it must not give.

A scene can leak into three files of prose — `memory/templates/root.md`, `enrich.md` and `retract.md`, which a session reads on every arrival — and, thinly, into the string literals in the binaries. Nothing else a session can reach comes from outside its own run.

In order, after editing the corpus:

```
python3 lab/corpus.py docs/recall-corpus.md | head -3            # does it still parse
docker compose run --rm -T builder python3 /work/lab/build.py   # the checks read what is built
python3 lab/lint.py --structural-only                            # free, exact, and the gate
```

`corpus.py` drops a line it cannot parse rather than failing, so the first catches an arrival or an expectation whose shape is slightly wrong and would otherwise be silently absent from the history. The second matters because the leak reading below reads the built templates and binaries: against a stale build it answers about something that is not running. The third checks the history itself: an arrival or an expectation that did not parse, a checkpoint with nothing to measure, dates and markers that can drift. It writes findings to `lab/lint.md`, prints a count, and exits non-zero on anything broken; notes are listed and do not fail it. It does not read the binaries, the templates or the mount list; whether anything a session can reach carries the history is the `check-lab-leak` skill's reading.

Two questions remain that no mechanical check answers. Both need a model:

```
python3 lab/lint.py --corpus docs/recall-corpus.md               # costs money
```

asks whether each checkpoint is still a fair test against the history that now precedes it — scenes share entities, and a new one can quietly resolve or contradict an older one. The `check-lab-leak` skill asks whether anything a session reads has become particular to these scenes, including a scene described in other words.
