# wanda

Wiki-Augmented Nodal Digital Assistant — a local macOS daemon that watches event sources (iCloud mail, Slack; more later) and dispatches headless `claude -p` sessions to handle them.

**What it does:**

- **Triages incoming iCloud mail.** Messages needing your attention become Slack posts (each thread is a task — reply in it and wanda spawns an agentic claude session that resumes across replies). Unwanted mail is moved to Trash, but only after clearing harness-side guards and only once you flip enforcement from `shadow` to `live`. Trash/ignore decisions land in a daily digest thread. wanda never sends email.
- **Answers in Slack.** `@wanda` in a channel or thread, or just message it directly — wanda starts an agent session seeded with the recent conversation and replies in place. Sessions resume, so a thread stays a conversation rather than a series of one-shots.

## Architecture

```
[IMAPClient thread]──┐   (call_soon_threadsafe)      ┌─> actions/mailbox (UID MOVE to Trash)
[Slack SocketMode]───┼─> asyncio Event queue ─> processor ─> actions/slack (task threads, digest)
[future watchers…]───┘                          │        └─> runner (claude -p subprocess)
                                    store.py (sqlite WAL — source of truth; Slack is the UI)
```

Triage is a one-shot batched `claude -p` call with `--json-schema`-enforced verdicts and **no tools** — the harness executes all side effects. Agentic sessions exist only for task threads, created lazily on your first reply, resumed with `--resume`.

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
| Channel | `@wanda <question>` | recent channel messages |
| Thread | `@wanda <question>` in the thread | that thread's replies |
| DM / group DM | just message it, no mention needed | recent messages in that conversation |
| Email task thread | reply in the thread wanda opened | the email, plus the session's own history |

Sessions answer with `wanda slack post`, so they can send progress updates or split a long answer. If a session fails without posting, the harness delivers its result instead, so a question never goes unanswered.

`wanda slack` is a normal CLI you can use too:

```
wanda slack history --channel C0123 --limit 50   # recent messages
wanda slack thread --channel C0123 --ts 1712345678.9012
wanda slack post --channel C0123 --text "hello"
wanda slack search "deploy failed"               # needs WANDA_SLACK_USER_TOKEN
wanda slack channels | members | user U0123
```

Agent sessions get the skills in `skills/`, which are synced into the workspace on every run — edit them to change how wanda writes and behaves, no code change needed.

## Trust assumption

**wanda assumes its Slack workspace is trusted.** Agent sessions get `Bash`, because that is how they run `wanda slack`. A headless session cannot scope Bash to a single command — `--allowedTools "Bash(wanda slack:*)"` is not enforced under `--permission-mode dontAsk`, and every mode that would enforce it blocks on a permission prompt no one can answer. So anyone who can trigger a session can, in principle, reach arbitrary shell on this machine through prompt injection.

That is a reasonable trade in a private workspace. It is not, if you ever invite people you don't trust, connect the app to a shared workspace, or let wanda read untrusted external content into a session. Before that, either run the daemon in a container, or set `WANDA_SLACK_OWNER_USER_IDS` to restrict who can trigger it and drop `Bash` from `WANDA_AGENT_ALLOWED_TOOLS` (the harness will then post replies itself).

## Safety model

- Trash guards run in the harness, in fixed order, and are re-evaluated immediately before every move (not just at triage time): never-trash allowlist → confidence ≥ 0.8 → shadow/live switch → hourly+daily rate caps. Caps count **executed moves**, and a capped message is *deferred* until the window reopens rather than discarded. The allowlist fails closed — a `From` header that can't be parsed is treated as protected.
- Move-to-Trash only, never expunge; iCloud keeps trash ~30 days and each digest entry carries the Message-ID for recovery.
- Only Slack user IDs in `WANDA_SLACK_OWNER_USER_IDS` can drive task threads (they command an agent with tools on this Mac). Those agents run `--restricted`, which confines file tools to `~/.wanda/workspace`, and without `WebFetch`, so injected email text has no easy exfiltration path.
- Email content is treated as untrusted everywhere: triage runs with **no tools at all**, all untrusted text is angle-bracket escaped before entering a prompt, and emails are labelled with harness-minted batch ids (`e1`, `e2`, …) rather than their Message-ID — so a crafted header can neither break out of its delimiter nor address a verdict at a different message.
- Untrusted headers are also escaped before reaching Slack, so a subject line can't fire `<!channel>` or render a disguised link.
- Daily run-count and cost circuit breaker pauses all claude invocations when tripped.
- A failed or unparseable verdict fails **closed**: the message surfaces as attention, never as trash.

## State

Everything lives in `~/.wanda/wanda.db` (sqlite, WAL): per-message state machine (`new → triaged → acting → done`, plus `deferred` for rate-capped trash and `error` for messages set aside after repeated failures), IMAP cursor, task↔thread↔session mapping, and a run/cost ledger. Crash recovery replays non-terminal rows; Slack posts carry metadata so recovery never double-posts.

Failures are retried with exponential backoff (8 attempts spanning ~90 minutes) rather than abandoned, so a Slack outage can't swallow an attention email. Anything genuinely given up on is reported by `wanda doctor` and can be returned to the pipeline with `wanda requeue`. Agent answers are only marked delivered once Slack accepts them, so paid work survives a failed post or a restart.

## Development

```
uv run pytest            # unit tests (no network, no claude)
uv run wanda doctor      # live dependency checks
uv run wanda triage      # dry-run triage against your real inbox
uv run wanda requeue     # retry messages that were set aside
```
