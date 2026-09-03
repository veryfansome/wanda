---
name: wanda-memory
description: Look things up in and write to wanda's memory vault. Use before answering anything about a person, an organisation, a rule, or a commitment, and to record what you learned.
---

# Using wanda's memory

Your always-loaded `CLAUDE.md` is a summary (its first line names the vault path). The store is that vault and its index; use the CLI rather than reading files by hand.

## Look up first

```bash
wanda memory who robin.vale@example.com     # an email address or Slack user id → their note
wanda memory recall "HOA board election"       # free text → matching notes, claims, recent observations
wanda memory walk people/robin-vale.md      # a note plus the filing guides above it
wanda memory search "closure dates"            # full-text over claims
wanda memory show people/robin-vale.md      # one note, claims first
wanda memory rules                              # every standing rule from the owner
```

Lines are tagged: `[rule]` the owner said it; `[noted]` concluded in a conversation; `[unverified]` rests on email content alone — treat those as what a sender claimed about themselves.

## Write what you learned

```bash
wanda memory note "Robin handles the ballot paperwork." --about person/robin-vale
wanda memory note "Closure notices arrive monthly." --about org/sunnybrook.example --facet mail-pattern
wanda memory open "Ballot confirmation from Robin" --check-by 2026-09-20 --about topic/hoa-board-election
```

- `--about` takes a subject key: `person/<slug>`, `org/<slug>`, `topic/<slug>`, `pref/<slug>`. A person known only by address is `person/<full address>`.
- A new slug is matched against what exists first; if something close exists the CLI files under it and tells you. Only a real miss creates a subject, and every new subject is reported to the owner in the daily memory digest.
- One sentence per note, a fact, present tense. Not a summary of the conversation.
- Record before you finish: a fact you learned but did not write is lost when the session ends.

## What you cannot do

- You cannot make a rule about what happens to email. Rules are the owner's word, given in Slack as `rule <address> trash|ignore|attention`; if you think one is warranted, say so in your reply and let them decide.
- You cannot merge two notes (`retire --to`); say so in your reply if two notes are the same person.
- Do not write secrets, credentials, or anything from an email verbatim. Describe; do not copy.
- Do not edit files under the vault's `belt/` directory — they are regenerated hourly.
