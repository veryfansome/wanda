# wanda memory — follow-up (decisions taken)

**Shipped on `memory-polish` already:** B1 (Z10, option B) as `9b1a781`, B2 (preference offers, option C) as `16a7b3b`, and the `.cowork` removal as `ff61168`. Everything else below is still outstanding.

Owner decisions made in session on 2026-09-03, recorded here so they survive the conversation they were made in. Struck-through rows have shipped; the rest are outstanding.

Rows name files and symbols rather than line numbers: the numbers drift with every commit, and these rows outlive them.

## A. Untrusted content containment

The governing decision: **the sender's prose has exactly one legitimate consumer — the sandboxed, Read-only triage classifier.** Nothing else needs it.

| # | Change | Files | Note |
|---|---|---|---|
| A1 | The Slack task post carries `verdict.summary` + `From` + `Subject` + `verdict.reason`. **No body snippet.** | `wanda/actions/slack.py` (`post_task`) | Owner's words: *"no i don't - I can just open my damn mailbox."* Drops `SNIPPET_LIMIT` and the ``` block. |
| A2 | The agent seed carries the verdict, not the body. | `main.py` (`agent_seed_prompt`), `main.py` (`_seed_for`) | `messages.verdict_json` (`store.py`) already persists summary/reason/urgency. |
| A3 | `messages.snippet` is cleared once the row reaches a terminal state — or never stored, carried through the triage batch in memory. | `store.py`, `imap_watcher.py` | After A1+A2 the only reader is `triage.py`, same cycle. Watch the recovery window: `apply_row` re-runs `post_task`, so A1 must land first or a crash between classify and post loses what it wanted to post. |
| A4 | Harden `_strip_html` for the truncation paths. | `imap_watcher.py` (`_strip_html`, and the `parse_failed` fallback in `parse_raw`) | Two verified gaps: (a) the `parse_failed` fallback splices raw MIME with no stripping at all; (b) the script/style regex needs a closing `</script>`, so an unterminated one leaks its body as prose — and the 64 KB partial fetch makes truncation routine, so an attacker just sends a body larger than the fetch window. Lower stakes after A1-A3 but still real. |

## B. Owner authority — the two security items

| # | Change | Files | Note |
|---|---|---|---|
| ~~B1~~ DONE `9b1a781` | **Z10 — withdraw the bare in-thread `rule <action>` form.** Require an explicit address: `rule <address> <action>`. | `commands.py`, `passes.py`, `service.py` | **DECIDED: option B.** Why it closes the hole: `_derive_owner_rules` reads the live rule's target out of the line's *text* (`m = DISPOSITION_RE.match(o["text"]); target = m.group(2)`), and the bare form is the only path where a mutable value reaches that text — `args = [task_sender, *args]` makes `text = rule_text(action, task_sender)`, with `task_sender` read from `messages.from_addr` at verify time. With an explicit address the text is pinned to what the owner typed in Slack. Removes code rather than adding a column. Work: delete the `if args[0].lower() in ACTIONS` expansion, drop the `sender_for_thread` plumbing through `make_owner_verifier` and `handle_command`, update `test_memory_commands.py`, and check `README.md` for the bare form. UX cost, accepted: in a task thread the owner now copies the address from the Slack post's `From:` line. |
| ~~B2~~ DONE `16a7b3b` | **Preference offers are unvalidated.** `commands.py` mints `offer["text"]`/`offer["subject"]` verbatim; the verifier recomputes from the same row and agrees. | `commands.py`, `store.py` | The disposition branch immediately above re-derives from structured fields and refuses on mismatch. **DECIDED: option C**, and made nearly free by the `.cowork` removal, which deleted the only legitimate producer. |
| B3 | **Bind `attest` to the claim ref only.** Drop the `or note_for_subject(o["subject"]) == doc` fallback, which grants `owner_said=1` to a claim of *arbitrary text* on the named note. | `index.py` | **DECIDED.** Owner: *"bind attest to the claim ref only - there aren't any existing attests."* Verify no fixture depends on the fallback. |
| B4 | **Re-mint on message edit.** An owner editing their own Slack command currently invalidates the rule it minted permanently; nothing consults Slack's `edited`/`subtype` signal. | `passes.py` | **DECIDED:** re-mint from the new text, and retire the claim the old line produced. Constraint: the ledger is append-only (`ledger.append` is the only writer), so the old *line* cannot be deleted — only the claim it produced can be retired. |

## C. Retire / unretire simplification

Owner's principle: *"We shouldn't treat memory notes as precious things we're afraid to lose."*

| # | Change | Files | Note |
|---|---|---|---|
| C1 | **Remove `unretire`** — CLI verb, Slack verb, restore path, journal handling. Recovery is "tell wanda again, mint fresh." | `memory_cli.py`, `commands.py`, `passes.py` | **DECIDED.** Deletes the Z12 bug class (unretire-after-`retire --to` producing two notes with one `ids:` set) outright. Also update `README.md`, `SKILL.md`, `defaults/README.md`. |
| C2 | **Unify retire with delete: both suppress.** A bare `retire` currently never calls `_veto_note_claims` (verified: one caller, `passes.py`, the Obsidian-delete path), so a retired note reassembles from the same witnesses. | `passes.py`, `passes.py` | **DECIDED** in principle — retire is a soft delete, retired notes never reach sessions, no undo. Under "no unretire," an unliftable suppression is the intent, not a defect. |
| C3 | **Gate `retire` behind `_in_session`.** Today only `--to` is guarded (`memory_cli.py`), so a session can bare-retire any note — and after C2 that would hand a session a year-long suppression primitive. | `memory_cli.py` | **DECIDED** as a consequence of C2. |
| C4 | **Trim E3's scope** (held out of wave 5). Its veto loop runs over `note.claims`, so it suppresses every witness of every claim including folded History claims and witnesses about *other* subjects. | `passes.py`, `passes.py` | The over-reach is wrong regardless of policy. The companion `:985` reader edit is retroactive — every `line:` key from every past deletion becomes effective on the first pass. |

## D. Recall failure should be visible

| # | Change | Files | Note |
|---|---|---|---|
| D1 | **Empty-index guard.** `if rep.docs == 0 and rep.broken_notes: raise` — a systematic indexing fault must not commit a successfully-empty index. | `index.py` (`rebuild`) | **DECIDED.** |
| D2 | **Give `meta.rebuilt_at` its first reader.** An index with no `rebuilt_at` has never been successfully built, so `conn_ro` treats it as unavailable and the existing marker at `render.py` fires. | `service.py` (`conn_ro`), `index.py` | **DECIDED.** Owner's framing: wanda should say she's drawing a blank rather than render as though she knows nothing. Wave 1 drops two never-read columns but keeps `rebuilt_at`. |
| D3 | Digest line to the owner when the index cannot be built, naming the offending note. | `passes.py` (`_report_flags` / hourly) | Today the only signal is a generic `hourly_failures >= 3` alert that names nothing. |

## E. Retention

| # | Change | Note |
|---|---|---|
| E1 | `messages.snippet` retention — see A3. Easy, because unlike the rest of `G6` the snippet is **not a trust input**. | The only prunes in the tree are `store.prune_slack_events(7)` and `audit.prune(90)`; neither touches `messages`. |
| E2 | The rest of `G6` stays deferred. Every other candidate **is** a trust input: pruning `memory_run_windows` promotes old email-tier lines to session, pruning `memory_owner_checks`/`checked:` demotes owner lines, pruning `memory_shas` makes every claim look owner-edited. | Needs a decision per table with its accepted tier/drift consequence. |

## Dropped

- **The `INDEX_BEGIN`-in-prose question.** Owner: editing the write-specs by hand *"is not an intended usage pattern and we should not over engineer this problem."* Leave `parse_writespec` as is.
- **Keeping `unretire` for the accidental-delete case.** Owner: *"this feels like over engineering."* Superseded by C1.
