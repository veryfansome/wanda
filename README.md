# wanda

Wiki-Augmented Nodal Digital Assistant — a local macOS daemon that watches event sources (iCloud mail, Slack; more later) and dispatches headless `claude -p` sessions to handle them.

**What it does:**

- **Triages incoming iCloud mail.** Messages needing your attention become Slack posts (each thread is a task — reply in it and wanda spawns an agentic claude session that resumes across replies). Unwanted mail is moved to Trash, but only after clearing harness-side guards and only once you flip enforcement from `shadow` to `live`. Trash/ignore decisions land in a daily digest thread. wanda never sends email.
- **Answers in Slack.** `@wanda` in a channel or thread, or just message it directly — wanda starts an agent session seeded with the recent conversation and replies in a thread. Sessions resume, so a thread stays a conversation rather than a series of one-shots.
- **Remembers.** A markdown vault at `~/.wanda/memory` (open it in Obsidian) holds what wanda learns about people, organisations, ongoing topics and your preferences. Sessions look things up and write what they learn; triage reads a confined extract; a free hourly pass keeps the derived index fresh and one model call a night distils recurring observations into curated notes (plus one call to revise the filing guides on nights your preferences changed). See [Memory](#memory).

## Architecture

```
[IMAPClient thread]──┐   (call_soon_threadsafe)      ┌─> actions/mailbox (UID MOVE to Trash)
[Slack SocketMode]───┼─> asyncio Event queue ─> processor ─> actions/slack (task threads, digest)
[future watchers…]───┘                          │        └─> runner (claude -p subprocess)
                                    store.py (sqlite WAL — source of truth; Slack is the UI)
```

Triage is a one-shot batched `claude -p` call with `--json-schema`-enforced verdicts and a single read-only tool confined to an empty working directory plus a generated memory extract — the harness executes all side effects. Agent sessions are separate: they run when you address wanda (a mention, a DM, or a reply in a thread it owns), are created lazily on first contact, and resume with `--resume` for the life of that conversation.

## Setup

1. **Install**: `uv sync`
2. **iCloud**: create an app-specific password at appleid.apple.com → Sign-In and Security.
3. **Slack**: create an app from `slack/manifest.yaml` (instructions in that file's header). If you created the app before mentions and DMs were supported, update its manifest and **reinstall** it — the new scopes and the Messages tab are what make `@wanda` and DMs work at all — then copy the fresh `xoxb-` token into `.env`.
4. **Configure**: `cp .env.example .env` and fill it in.
5. **Check**: `uv run wanda doctor` — verifies IMAP login, both Slack tokens, channel membership, the claude CLI (including a live smoke run), and the database.
6. **Try it**: `uv run wanda triage --limit 10` — dry-run classification of recent inbox mail, prints what the daemon would do. No side effects, and it writes to a separate `dryrun.db` so a running daemon can never pick up and act on what it classified.
7. **Run**: `uv run wanda run` — daemon in the foreground (shadow mode by default: trash verdicts are logged as "WOULD trash", nothing is moved).
8. **Go live** once shadow digests look right: set `WANDA_ENFORCEMENT=live` in `.env`.
9. **Install as LaunchAgent**: see the header of `launchd/com.wanda.agent.plist` (a template — it carries a `sed` line to fill in absolute paths, which launchd requires).

## Talking to wanda

| Where | How | Context the session gets |
|---|---|---|
| Channel | `@wanda <question>` | recent channel messages, plus memory of the people and subjects involved |
| Thread | `@wanda <question>` in the thread | that thread's replies |
| DM / group DM | just message it, no mention needed; wanda answers in a thread under your message, like a private channel | recent messages, wanda's last few answers there, and memory |
| Email task thread | reply in the thread wanda opened | the email, what wanda knows about the sender, plus the session's own history |
| Anywhere | `rule <address> trash\|ignore\|attention` (memory owners only) | none — handled by the harness, no session; see [Memory](#memory) |

Sessions answer with `wanda slack post`, and their last post to a conversation must be the complete answer — the harness treats a post there as the answer being delivered. If a session ends without answering, the harness delivers its result instead, so a question never goes unanswered; if it answered and then failed, you get a short note rather than the answer repeated.

`wanda slack` is a normal CLI you can use too:

```
wanda slack history --channel C0123 --limit 50   # recent messages
wanda slack thread --channel C0123 --ts 1712345678.9012
wanda slack post --channel C0123 --text "hello"
wanda slack search "deploy failed"               # needs WANDA_SLACK_USER_TOKEN
wanda slack channels | members | user U0123
```

Agent sessions get the skills in `skills/`, which are synced into the workspace on every run — edit them to change how wanda writes and behaves, no code change needed.

## Memory

Everything wanda remembers lives in `~/.wanda/memory`, a plain-markdown vault (open it in Obsidian; it is a git repo wanda commits to on a schedule). Markdown is the truth; the SQLite index beside it (`~/.wanda/memory.idx`) is a cache you can delete at any time.

Two lanes:

- **The belt (fast).** `belt/ledger/` is an append-only log, one line per observation, written by triage (one optional memo per email, about the sender), by agent sessions (`wanda memory note`), and by you (see below). `belt/subjects/` is regenerated hourly from it: one file per subject that has recurred, machine-owned. Nothing here costs a model call.
- **The curated notes (slow).** `people/`, `orgs/`, `topics/`, `prefs/`, `open/` — one note per person, organisation, ongoing topic, policy area, or dated commitment. Claims sit between `wanda:begin/end claims` markers, each with the edges that justify it (`derived-from`, `owner-said`, `supersedes`, `about`, …). Everything under `## Notes` is yours and is never touched. Once a night, one model call turns observations that recurred (three independent causes over two days) into claims here; the rest is deterministic.

Each directory has a `CLAUDE.md` write-spec — what belongs there, how it is named, when to split. wanda reads them when filing and revises them from your stated preferences (and reports every change). The workspace `CLAUDE.md` every session loads is composed from the root write-spec plus generated blocks (standing rules, due soon, who is in play), capped at 4 KB.

**Trust is provenance, and it is checked, not declared.** Every claim carries a tier: **owner** (you said it in Slack — the harness recorded the message and re-verifies its author), **session** (concluded in a conversation), or **email** (rests on email content alone; tagged `[unverified]`, kept out of the always-loaded file, fenced separately in prompts). Only owner-tier evidence can decide what happens to mail:

```
rule priya.nash@example.org trash          # a triage rule, in any channel or DM
rule sunnybrook.example ignore                  # by domain
rule person/robin-vale prefers texts   # a preference about a subject
rule k4                                   # accept an offer from the digest (a bare `k4` works in the digest thread)
attest people/robin-vale#c4            # raise a claim to your word
pin people/robin-vale#c4               # keep a claim exactly as written
forget people/robin-vale#c4            # retire it and suppress the pattern behind it
unretire people/priya-nash.md              # bring back a note you deleted
```

Only Slack users listed in `WANDA_MEMORY_OWNER_USER_IDS` can do this; a session cannot (it posts as the bot). In a channel the command needs an `@wanda` mention; in a DM or the digest thread it does not. The daemon holds the authority for these lines in its own memory: a rule is applied only by a daemon that received the Slack event or fetched and verified the message, never on the strength of a database row. Editing a note in Obsidian counts as your word too: a changed claim line is pinned and never rewritten; a renamed note keeps its history; a deleted note is retired and the patterns behind its claims are suppressed for a year (`unretire` brings the note back; the suppression stays until you `attest` or re-state what you want).

A daily `🧠 wanda memory` thread in the triage channel reports what changed: new subjects, write-spec rewrites, your hand edits, rules that went live, templated rule offers (`rule kN`), and anything that failed verification.

Useful commands:

```
wanda memory who robin@example.com          # what wanda knows about a sender or Slack user
wanda memory recall "HOA ballots"           # free-text recall
wanda memory walk people/robin-vale.md   # a note with the filing guides above it
wanda memory note "…" --about person/x      # record a fact (sessions do this)
wanda memory open "…" --check-by 2026-09-20 --about topic/x   # a dated commitment
wanda memory search | show | rules | pin | forget | retire | unretire
wanda memory import-cowork ~/.cowork        # one-time import of a previous vault (explicit, idempotent, owner only)
wanda memory hourly | reindex | fsck | digest | status
```

Every tool call a session or the classifier makes is logged to `~/.wanda/logs/tools-YYYY-MM-DD.jsonl` (90 days) by a `PostToolUse` hook, and mirrored to the unified log; `wanda doctor` counts them and shows the first few reads outside the granted roots or shell commands that are not `wanda …`. The log records Bash command lines as typed, so whatever a session puts on a command line ends up in it and in the system log.

## Trust assumption

**wanda assumes its Slack workspace is trusted.** Agent sessions get `Bash`, because that is how they run `wanda slack` and `wanda memory`. A headless session cannot scope Bash to a single command — `--allowedTools "Bash(wanda slack:*)"` is not enforced under `--permission-mode dontAsk`, and every mode that would enforce it blocks on a permission prompt no one can answer. So anyone who can trigger a session can, in principle, reach arbitrary shell on this machine through prompt injection.

That is a reasonable trade in a private workspace. It is not, if you ever invite people you don't trust, connect the app to a shared workspace, or let wanda read untrusted external content into a session. Before that, either run the daemon in a container, or set `WANDA_SLACK_ALLOWED_USER_IDS` to restrict who can trigger it and drop `Bash` from `WANDA_AGENT_ALLOWED_TOOLS` (the harness will then post replies itself).

Memory does not widen this, and its own integrity does not rest on tool scoping. Two guarantees hold even against a session that writes arbitrary bytes into the vault and `wanda.db`:

- **Only the owner's Slack word can tell triage what to do.** A triage rule is derived from an owner-tier ledger line — one that points at a Slack message the daemon itself received, or fetched and verified (author, and that the line is one that message could have minted). That authority is held in the daemon's memory, re-checked daily, and never granted by a database row; a process without it (the CLI) can never produce owner tier. The live rule set is computed from those ledger lines directly, so editing the prefs note's claims or edges cannot enable or disable a rule.
- **Email content can never dispose of mail.** A line written from a shell is attributed by the agent-run windows the daemon recorded, not by anything in the line: while an email task is running, every shell-written line is email-tier, and email-tier can never state a disposition.

Residuals, stated plainly. The softer session-vs-email distinction for *hand-written* vault content (a claim a session types directly into a note, with no ledger evidence) rests on the same trust as an Obsidian edit — wanda treats it as the owner's word and it becomes session-tier, so a session with shell access can get its text into curated notes and the projection tagged `[noted]`. It still cannot reach owner tier or state a disposition. The tool-call log is tamper-evident rather than tamper-proof — a session can edit its own log, which is why lines are mirrored to the unified log.

## Safety model

- Trash guards run in the harness, in fixed order, and are re-evaluated immediately before every move (not just at triage time): never-trash allowlist → confidence ≥ 0.8 → shadow/live switch → hourly+daily rate caps. Caps count **executed moves**, and a capped message is *deferred* until the window reopens rather than discarded. The allowlist fails closed — a `From` header that can't be parsed is treated as protected.
- Move-to-Trash only, never expunge; iCloud keeps trash ~30 days and each digest entry carries the Message-ID for recovery.
- Agent sessions run in `~/.wanda/workspace` with `Bash,Read,WebSearch,Skill` and `--setting-sources project` (which is what loads the skills and the audit hook). They are **not** sandboxed — see [Trust assumption](#trust-assumption) above for what that means and how to lock it down. `WANDA_SLACK_ALLOWED_USER_IDS` restricts who can trigger them; empty means anyone in the workspace.
- Email content is treated as untrusted everywhere: triage runs with **one read-only tool**, `--restricted` to an empty working directory plus a generated memory extract (never the vault, never the repo where `.env` lives); all untrusted text is angle-bracket escaped before entering a prompt; and emails are labelled with harness-minted batch ids (`e1`, `e2`, …) rather than their Message-ID — so a crafted header can neither break out of its delimiter nor address a verdict at a different message. The memory block triage sees carries only structured facts about senders (titles from closed sources, counts, dates) and your rules — never model prose — and travels in the user message, ahead of the emails.
- Untrusted headers are also escaped before reaching Slack, so a subject line can't fire `<!channel>` or render a disguised link.
- A daily run-count breaker pauses all claude invocations when tripped. It exists to stop a runaway loop; wanda runs on a subscription plan, so there is no dollar cap.
- A failed or unparseable verdict fails **closed**: the message surfaces as attention, never as trash.

## State

Daemon state lives in `~/.wanda/wanda.db` (sqlite, WAL): per-message state machine (`new → triaged → acting → done`, plus `deferred` for rate-capped trash and `error` for messages set aside after repeated failures), IMAP cursor, task↔thread↔session mapping, a run ledger, and the small amount of memory state that must outlive the disposable index (claim-line hashes for hand-edit detection, owner-message verifications, rule offers, pending digest lines). Memory itself is markdown in `~/.wanda/memory`. Crash recovery replays non-terminal rows; Slack posts carry metadata so recovery never double-posts.

Failures are retried with exponential backoff (8 attempts spanning ~90 minutes) rather than abandoned, so a Slack outage can't swallow an attention email. Anything genuinely given up on is reported by `wanda doctor` and can be returned to the pipeline with `wanda requeue`. Agent answers are only marked delivered once Slack accepts them, so paid work survives a failed post or a restart.

## Development

```
uv run pytest                          # unit tests (no network, no claude)
WANDA_LIVE_TESTS=1 uv run pytest tests/test_live_cli.py   # probes against the installed claude CLI (spends usage)
uv run wanda doctor                    # live dependency checks
uv run wanda triage                    # dry-run triage against your real inbox
uv run wanda requeue                   # retry messages that were set aside
uv run wanda memory hourly             # run the memory hourly pass by hand
```
