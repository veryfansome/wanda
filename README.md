# wanda

Wiki-Augmented Nodal Digital Assistant — a daemon, run in Docker on the household's Mac, that watches Slack (and iCloud mail, when triage is on) and dispatches headless `claude -p` sessions to handle them, with a memory of everything it has been told.

**What it does:**

- **Triages incoming iCloud mail**, when `WANDA_EMAIL_TRIAGE` is on; the Docker setup turns it off. Messages needing your attention become Slack posts (each thread is a task — reply in it and wanda spawns an agentic claude session that resumes across replies). Unwanted mail is moved to Trash, but only after clearing harness-side guards and only once you flip enforcement from `shadow` to `live`. Trash/ignore decisions land in a daily digest thread. wanda never sends email.
- **Talks in Slack, and remembers.** A DM, a group DM, `@wanda` in a channel, or a reply in a thread wanda is in, wherever only the people in `WANDA_SLACK_OWNER_USER_IDS` can read it: a fresh session in wanda's vault (`memory/`) takes what was said, told who said it, where, who reads the answer, and what was said before it there. The session records what it learned with `mem` and answers only when it has something to say — most messages get no reply, by design.

## Architecture

```
[IMAPClient thread]──┐   (call_soon_threadsafe)      ┌─> actions/mailbox (UID MOVE to Trash)
[Slack SocketMode]───┼─> asyncio Event queue ─> processor ─> actions/slack (task threads, digest)
[future watchers…]───┘                          │        └─> runner (claude -p subprocess)
                                    store.py (sqlite WAL — source of truth; Slack is the UI)
```

Triage is a one-shot batched `claude -p` call with `--json-schema`-enforced verdicts and **no tools** — the harness executes all side effects. Memory sessions run when wanda is addressed (a mention, a DM, or a message in a thread it is in), never resumed, in the vault with the lab's prompt (`wanda/vault.py`). A conversation's sessions run one after another, and each takes every message that arrived there since the one before, up to when it starts; across conversations, up to `WANDA_MEMORY_SESSIONS` (one or two) run at once. An email task's session is created on the owner's first reply in its thread and resumes with `--resume` after that.

## Setup

1. **Install**, for the host commands under Development only: `uv sync`. The image installs its own.
2. **iCloud**, for mail later (step 9): create an app-specific password at appleid.apple.com → Sign-In and Security.
3. **Slack**: create an app from `slack/manifest.yaml` (instructions in that file's header). If you created the app before mentions and DMs were supported, update its manifest and **reinstall** it — the new scopes and the Messages tab are what make `@wanda` and DMs work at all — then keep the fresh `xoxb-` token for the household worktree's `.env` (step 5).
4. **A worktree for the household**: build and run it from its own clean worktree of its branch, never from the lab's checkout, so an upgrade ships only what was merged: `git worktree add ~/PyCharmProjects/wanda-household <branch>`. Not a directory called `wanda-home`, the household's compose project: the lab's compose file takes its project name from the directory it is run in, and from one of that name it would act on the household's container. Every step below runs there.
5. **Configure**: `cp .env.example .env` and fill it in. Docker needs `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`), the two Slack tokens, `WANDA_ALERT_CHANNEL`, `WANDA_SLACK_OWNER_USER_IDS`, `WANDA_SLACK_NAMES` and `WANDA_TZ`, and refuses to start without them. `WANDA_SLACK_NAMES` gives each name once, and only to an id in `WANDA_SLACK_OWNER_USER_IDS`: the clock opens a direct message by name, and the daemon refuses to start on a name given to two ids or an id not in that list, which an older `.env` may have. The clock's two settings may be left out: `WANDA_MORNINGS`, whose morning gets a look from wanda and when, empty for none, and `WANDA_QUIET_HOURS`, the hours no look starts in. Alerts, a private channel's or fan's DM, are for the people who keep wanda running: each is posted with a mark that keeps it out of what any session is shown. Until a test alert in fan's DM has been seen to stay out of his next session there, a private channel of fan's and wanda's, where nobody mentions her, is recommended: Slack has to hand the mark back, and in his DM an alert whose mark did not come back would reach his sessions as wanda's own earlier words.
6. **Its home**: `mkdir -p ~/wanda-home/transcripts`. The container keeps the vault's snapshots and the transcripts there, and will not start without it; the vault and the run store are Docker volumes (State). Not `~/.wanda`, which host commands use; and not under Documents, Desktop or iCloud Drive, where a macOS permission prompt stalls every Docker mount until someone answers it.
7. **Run**: `docker compose -f compose.wanda.yaml up -d --build`, which also upgrades after a merge into the household's branch. The header of `compose.wanda.yaml` has the rest: logs, `mem`, stopping. Rancher Desktop starts at login, and the container with it. A Mac that restarts on its own, after a power cut, waits at the FileVault unlock screen, and wanda stays down until fan logs in at it; nothing says so meanwhile, and Slack still shows her online.
8. **Check**: `docker compose -f compose.wanda.yaml exec wanda /opt/wanda/.venv/bin/wanda doctor --no-smoke` — both Slack tokens, the alerts channel, who can talk to wanda, the vault, the claude CLI, the database, the clock's settings, each person's last morning look, the timed reminders not given in the last 30 days with who asked and why, and how to see one and give it later, and, since the last start, how many sessions left processes running when they ended and how many `mem` calls were refused because the vault stayed busy, each counted once, in the session that was shown it (the log's `left … process(es) running` lines name the sessions; the refusals a session was shown are in the transcripts). A command Claude Code moves to the background is not counted: one that runs past its 120 s Bash timeout, as a chain of `mem` calls can while the vault is held that long, or one a session gave a timeout under 90 s. Its refusals go to that task's output file in the container's `/tmp`. Every `doctor`, `requeue` and hand-run `mem` goes through `exec` like this, where `mem` reaches the vault under its lock and dates a write today in `WANDA_TZ`, and the run store is read on its volume; nothing on the Mac writes `~/wanda-home` while the container runs. From a terminal, Ctrl-C ends a call made this way. Without one (`-T`, a script, an agent's shell), stopping the client leaves `mem` waiting in the container for the vault, and its write lands when the vault is free, so run those as `docker compose -f compose.wanda.yaml exec -T wanda timeout 60 mem …`.
9. **Mail**, later: email triage is off. Before turning it on, give email task sessions what memory sessions have: the runner's `mark` (`wanda/runner.py`, as `memory_turn` passes it), so that whatever such a session leaves running is ended when it ends; without it, a command one of them started can outlive it. To turn it on, set `WANDA_EMAIL_TRIAGE` to on in `compose.wanda.yaml` and pass `WANDA_ICLOUD_EMAIL`, `WANDA_ICLOUD_APP_PASSWORD` and `WANDA_EMAIL_TRIAGE_SLACK_CHANNEL_ID` the same way, then `up -d`; `exec wanda /opt/wanda/.venv/bin/wanda triage --limit 10` is the dry run, and `WANDA_ENFORCEMENT: live`, added to `compose.wanda.yaml`'s environment as above, moves trash for real. Email task sessions then run the older path, with WebSearch and the container's Slack tokens in their environment. Never run a daemon on the host beside the container: one Slack app, one connection.

## Talking to wanda

| Where | How | Context the session gets |
|---|---|---|
| Channel | `@wanda <question>` | who is in the channel, and whether it is public; its last 12 hours |
| Thread | `@wanda <question>` in the thread; in a thread begun with `@wanda`, any message | who is in the channel; the thread so far: its first message and its newest replies, 50 in all |
| DM | just message it, no mention needed | its last 12 hours |
| Group DM | just message it, no mention needed | who is in it; its last 12 hours |
| Email task thread | reply in the thread wanda opened | the email, plus the session's own history |

Only the people in `WANDA_SLACK_OWNER_USER_IDS` start sessions, and each is named to wanda by `WANDA_SLACK_NAMES`. A memory session runs only in a conversation whose every member is one of them; a public channel, which anyone in the workspace can open, counts only while everyone in the workspace is. Anywhere else wanda posts nothing and logs the conversation once. Earlier messages reach the session with their times, at most 20 of the last 12 hours outside a thread, and in a thread its first message and newest replies, 50 in all; wanda's answers to the sessions before are among them, even one posted after a message the session takes. A deleted message that is still waiting for its session is withdrawn from it. An edit may never reach a session: a correction goes in a new message. A session sees an attachment's name, not what is in it. A memory session answers in its report: the harness posts the answer if there is one, posts nothing if it is empty, and posts a short failure note when the session fails or ends without a report. An email task's session posts with `wanda slack post` instead, and if it ends without answering, the harness delivers its result.

The clock starts sessions too, with no message, in fan's or mei's own DM: when an undertaking of hers recorded with a time of day comes due, for the one person who asked for it; and every morning, for each person `WANDA_MORNINGS` names, a look at the day ahead, handed what has come due for them since their last look, which says something only when there is something to say. Quiet hours hold back only the looks. Such a session is shown what woke it, and a look its list, and no earlier messages. It posts the last answer it gave that says something, even when a later turn said nothing, failed or ran out of time, and otherwise nothing: a failure posts no note in the DM, and goes to the alerts. One a restart cuts short before its answer is recorded posts nothing: a reminder it was giving is woken again at the next start while its time is under two hours gone, and a look is not run again that day. A reminder the clock was to give and did not is named in an alert by its id and time, and `doctor` says who asked and why.

`wanda slack` is a normal CLI you can use too:

```
wanda slack history --channel C0123 --limit 50   # recent messages
wanda slack thread --channel C0123 --ts 1712345678.9012
wanda slack post --channel C0123 --text "hello"
wanda slack search "deploy failed"               # needs WANDA_SLACK_USER_TOKEN
wanda slack channels | members | user U0123
```

Email task sessions get the skills in `skills/`, synced into their workspace on every run. Memory sessions load the vault's own skills, `enrich` and `retract`, which every start rewrites from `memory/templates/`. In Docker both come from the image, so an edit to either takes effect with `docker compose -f compose.wanda.yaml up -d --build`.

Nothing wanda reads as her own addresses her or names her from outside: the skills, the messages that open and continue her sessions, her triage rules in `prompts/email_triage.md`, the help `wanda slack` prints, the vault's standing texts in `memory/templates/`, and what the harness posts to Slack in her name. Who she is, how she conducts herself and what she says are in the first person (I, me, my). A procedure — a tool to use, or a step to take such as putting something into a field of her output — is a bare imperative with no pronoun, and a paragraph or list item holding one has no first-person word, so nobody else can be read as giving her the order. A paragraph in her system prompt (`ANCHOR` in `wanda/main.py`) says that the "I" is her.

`tests/test_voice.py` checks the texts, not the posts. It fails if one says "you", or names her as "wanda", "she" or "the bot", anywhere but in code, in quotation marks, in "I am wanda", in "the wanda CLI", and in the help's usage lines and variable names. It also fails if a first-person word appears in the texts that are procedures by where they sit: the email seed's line on how to reply, the triage batch's instruction, each skill's description, slack-reply's Sending and Reading more context sections, and the numbered steps of the follow-up skill. Which other sentence is a procedure is a reading of it, and is not checked.

## Trust assumption

**wanda assumes its Slack workspace is trusted.** Sessions get `Bash`: memory sessions run `mem` with it, and email task sessions run `wanda slack`. A headless session cannot scope Bash to a single command — `--allowedTools "Bash(wanda slack:*)"` is not enforced under `--permission-mode dontAsk`, and every mode that would enforce it blocks on a permission prompt no one can answer. So anyone who can trigger a session can, in principle, reach a shell through prompt injection.

That is a reasonable trade in a private workspace. It is not, if you ever invite people you don't trust, connect the app to a shared workspace, or let wanda read untrusted external content into a session. That is why a memory session runs only where everyone who can read the conversation is in `WANDA_SLACK_OWNER_USER_IDS`: nobody else starts a session that holds the household's memory and has a shell, and none starts where anyone else could read its answer. Who can read a conversation is looked up when a message arrives there, again when its session starts, and again before an answer Slack refused at first is posted later, which stays in `wanda.db` unposted if anyone else can read it by then; someone added while a session runs reads its answer. Earlier messages from someone who has since left the conversation can still reach a session, as part of the conversation so far. The daemon will not start with that list empty.

In Docker that shell is the container's. It reaches what is mounted into it (the vault, `wanda.db`, the snapshots, the transcripts), and can change or delete any of it, the snapshots included; and the container's environment, which holds the Slack tokens. A memory session's own environment does not carry them, and its PATH has no `wanda`; a session that went looking could still read them from `/proc`. Its environment does carry `CLAUDE_CODE_OAUTH_TOKEN`, which claude needs, so a session can read the token and use the subscription with it; renew it as under State if that is ever in doubt. A session can also change what later sessions load from the vault's `.claude/`; the snapshots record that directory, and the skills are rewritten at every start. Claude Code also reads `CLAUDE.md` and `.claude/` in the directories above the vault, `/srv/wanda` and `/srv`, which the image keeps closed to the session user.

Secrecy between the people wanda serves is kept by wanda, not by storage: whoever runs the machine can read the vault, through `exec` or in its snapshots, the run store through `exec`, and everything else in `~/wanda-home`. Taking something back ("forget that") changes what wanda goes by, not what is kept: the earlier text stays in the vault, struck through, in every snapshot taken before, and for 30 days in the transcripts.

## Safety model

- Trash guards run in the harness, in fixed order, and are re-evaluated immediately before every move (not just at triage time): never-trash allowlist → confidence ≥ 0.8 → shadow/live switch → hourly+daily rate caps. Caps count **executed moves**, and a capped message is *deferred* until the window reopens rather than discarded. The allowlist fails closed — a `From` header that can't be parsed is treated as protected.
- Move-to-Trash only, never expunge; iCloud keeps trash ~30 days and each digest entry carries the Message-ID for recovery.
- Memory sessions run in the vault with `Read,Glob,Grep,Bash,Skill` and `--setting-sources project` (which loads the vault's CLAUDE.md and its skills); email task sessions run in the data directory's `workspace/` with `Bash,Read,WebSearch,Skill`. Neither is sandboxed beyond the container — see [Trust assumption](#trust-assumption). `WANDA_SLACK_OWNER_USER_IDS` restricts who can trigger them, and the daemon will not start with it empty.
- Email content is treated as untrusted everywhere: triage runs with **no tools at all**, all untrusted text is angle-bracket escaped before entering a prompt, and emails are labelled with harness-minted batch ids (`e1`, `e2`, …) rather than their Message-ID — so a crafted header can neither break out of its delimiter nor address a verdict at a different message.
- Untrusted headers are also escaped before reaching Slack, so a subject line can't fire `<!channel>` or render a disguised link.
- Daily run-count and cost circuit breaker pauses all claude invocations when tripped.
- A failed or unparseable verdict fails **closed**: the message surfaces as attention, never as trash.

## State

The vault, the memory, is the Docker named volume `wanda-home_vault`, mounted in the container at `/srv/wanda/vault`; it is its own git repository with no commits. The run store, `wanda.db`, is the named volume `wanda-home_store`, at `/srv/wanda/store`, where the Mac cannot open it: SQLite's locks do not reach across the Mac's mount, and a read on the Mac while the daemon ran corrupted the store or lost what the daemon wrote after it. To look into it, go through the container: `doctor`, or `docker compose -f compose.wanda.yaml exec wanda /opt/wanda/.venv/bin/python -c 'import sqlite3; ...'` on `/srv/wanda/store/wanda.db`. Under `~/wanda-home`, mounted at `/srv/wanda/home`: `snapshots.git`, a bare repository with a commit of the vault whenever it changed, at a start or after a session; and `transcripts/`, Claude Code's transcripts of the sessions, which `mem session` reads and which are kept for 30 days (the vault's `.claude/settings.json`). Nothing copies them off the Mac. To read the vault on the Mac, in Obsidian for one, clone `snapshots.git` into a directory of its own (`git clone ~/wanda-home/snapshots.git <directory>`, then `git pull` there); that copy is never written back. A clone or a pull writes nothing in `snapshots.git`, so either may run while the container does.

The volumes are deleted by `docker compose -f compose.wanda.yaml down -v`, by `docker volume prune -a` once no container refers to them, and with everything else by a Rancher Desktop factory reset or a switch of container engine; `stop`, `down` and an upgrade keep them. Their loss costs what was written to the vault since the last snapshot, which is taken after every session: what the session running at that moment wrote, and any `mem` write made by hand since; and the run store: answers still waiting to be posted, the day's alerts and run count, which threads began with @wanda, so that a reply in one of those threads reaches her only with @wanda, every time, and the clock's records. Without those:

- a look already run that day can run again before noon, and `doctor` reads "none yet" for each person's last look;
- each person's next look starts from the day before, as a first look does, so a day a failed look handed on reaches no look;
- an open reminder whose time is under two hours gone is woken again, whether or not it was given;
- an open reminder dated yesterday or today whose time is more than two hours gone is kept and alerted as not given, one that was given and left open included;
- `doctor`'s list of reminders not given starts empty, and one not yet named in an alert is gone without a trace;
- a reminder a look listed as still to come, closed or re-dated before its time, is no longer kept as not given.

The snapshots and the transcripts are on the Mac. If the vault holds no node and no `CLAUDE.md` while the run store or `snapshots.git` says it had one, or `snapshots.git` cannot be read, the daemon refuses to start, and alerts, rather than starting an empty one; the empty directories a `mem` read by hand leaves in it do not count. Losing `~/wanda-home` costs the snapshots and up to 30 days of transcripts, and not the vault; once `mkdir -p ~/wanda-home/transcripts` lets the container start, the daemon refuses, and alerts, because the run store says there were snapshots.

To put the vault back as a snapshot had it, after a bad session or into a new volume, with the container stopped and the vault restored in place, through a one-off container that has the mounts and runs as the container's own user:

```
docker compose -f compose.wanda.yaml stop
docker compose -f compose.wanda.yaml run --rm --no-deps wanda git --git-dir /srv/wanda/home/snapshots.git log --stat
docker compose -f compose.wanda.yaml run --rm --no-deps wanda git --git-dir /srv/wanda/home/snapshots.git --work-tree /srv/wanda/vault read-tree -u --reset <commit>
docker compose -f compose.wanda.yaml run --rm --no-deps wanda git --git-dir /srv/wanda/home/snapshots.git --work-tree /srv/wanda/vault clean -d -f -x
docker compose -f compose.wanda.yaml up -d
```

`read-tree` puts back every file the snapshot holds and removes those the last snapshot holds and it does not; `clean` removes the rest, written since the last snapshot, so the vault holds what `<commit>` holds and nothing else. Restoring to `<commit>` undoes every session after it; the snapshots after it stay in the log. `run` makes a deleted volume again, owned by the session user.

To go on without what is lost instead, with the container stopped: after losing `~/wanda-home`, `docker compose -f compose.wanda.yaml run --rm --no-deps wanda git init -q --bare /srv/wanda/home/snapshots.git`, and the next snapshot holds the vault as it stands; to start an empty vault, move `~/wanda-home/snapshots.git` aside on the Mac, `docker compose -f compose.wanda.yaml down`, then `docker volume rm wanda-home_vault wanda-home_store`; the new vault's sessions still read up to 30 days of the old exchanges through `mem session`, unless `~/wanda-home/transcripts/-srv-wanda-vault` is moved aside too. Then `up -d`. After moving, deleting or making anything under `~/wanda-home` on the Mac, wait half a minute before `up -d`: the container's view of that directory can be up to 20 s old.

A start that fails with `could not lock config file .git/config` met a lock file that a stopped git left in the vault's own repository. With the container stopped, remove it through a one-off container, then start again:

```
docker compose -f compose.wanda.yaml stop
docker compose -f compose.wanda.yaml run --rm --no-deps wanda rm /srv/wanda/vault/.git/config.lock
docker compose -f compose.wanda.yaml up -d
```

The rest lives in `wanda.db` (sqlite, WAL), on its volume: per-message state machine (`new → triaged → acting → done`, plus `deferred` for rate-capped trash and `error` for messages set aside after repeated failures), IMAP cursor, task↔thread↔session mapping, and a run/cost ledger. Crash recovery replays non-terminal rows; Slack posts carry metadata so recovery never double-posts.

Failures are retried with exponential backoff (8 attempts spanning ~90 minutes) rather than abandoned, so a Slack outage can't swallow an attention email. Mail genuinely given up on is reported by `wanda doctor` and can be returned to the pipeline with `wanda requeue`, both run with `exec` in the container. Agent answers are only marked delivered once Slack accepts them, so paid work survives a failed post or a restart. An answer Slack keeps refusing is tried at every pass of the mail loop, once a minute with triage off, eight times, so for about eight minutes; then it is given up on, and an alert, at most one a day, names its run and when the run started, never the conversation, which can tell whom it was for. `wanda doctor` lists the answers given up on, newest first, with where each was due; what each said stays in `wanda.db`.

Every session runs on `CLAUDE_CODE_OAUTH_TOKEN`. When Claude Code stops accepting it, every message that starts a session gets "⚠️ my run failed" with Claude Code's reason (what it says for a token it no longer accepts has not been seen; its binary holds "Invalid API key · Please run /login" and an OAuth 401 message naming `claude setup-token`), and every session the clock starts fails into an alert: a look into the day's clock alert, a timed reminder into the reminder alert. To renew it, run `claude setup-token` on the Mac, put the new token in `CLAUDE_CODE_OAUTH_TOKEN` in the household worktree's `.env`, then `docker compose -f compose.wanda.yaml up -d`, which makes the container again with it and keeps the volumes.

The volumes, the images and Docker's build cache share one disk in Rancher Desktop's VM. A start on a full disk stops at the run store: the log, and an alert at most once a UTC day, say `the run store /srv/wanda/store/wanda.db could not be opened or written: …`, and it tries again every minute, so wanda comes back by herself once there is room. A restart while the disk is still full alerts again. A disk that fills while wanda runs raises no alert until a restart: a message that arrives then gets no reply and is lost, since Slack has already been told it arrived and does not send it again, and the log says `Failed to run a request listener: database or disk is full`; an answer whose session ended as the disk filled can be lost too, with `⚠️ I hit an internal error handling that reply.` posted in its place. `doctor` then says that the vault and the run store take no write. `docker system df` shows what fills the disk. Make room by removing by name what is known not to be needed, an old image with `docker image rm` for one, never with a `prune`, which can take the volumes with it.

## Development

```
uv run pytest            # unit tests (no network, no claude)
uv run wanda doctor      # live dependency checks, against ~/.wanda
uv run wanda triage      # dry-run triage against your real inbox
uv run wanda requeue     # retry messages that were set aside
```

These use `~/.wanda`, never the container's `~/wanda-home`.

The runner's cases that read `/proc`, how a session's leftovers are found in the container, skip on the Mac. To run them in the image, with the pytest `uv sync` installed on the Mac, from the household worktree after `up -d --build`:

```
docker run --rm --init --network none -v "$PWD/tests:/t/tests:ro" -v "$PWD/.venv/lib/python3.12/site-packages:/pt:ro" -w /t wanda-home /opt/wanda/.venv/bin/python -c 'import sys; sys.path.append("/pt"); import pytest; sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", "tests/test_runner.py"]))'
```

The Mac's packages go last on the path, so that the image's own, built for Linux, are the ones imported.
