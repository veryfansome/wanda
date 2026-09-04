---
kind: write-spec
---
# How wanda files things

This vault is wanda's memory. Markdown is the truth; everything else is derived from it.

- `people/` — one note per real person, keyed by name once known, by full email address until then.
- `orgs/` — companies, schools, boards, mailing lists. A sender that is a role address (`noreply@`, `info@`) is an org.
- `topics/` — ongoing projects, issues and events with a beginning and an end. Prefer an existing topic; run `wanda memory recall` before minting one.
- `prefs/` — how the owner wants things handled. One policy lives once here and names the subject it was filed under; never copy a policy onto a sender's note.
- `open/` — commitments with a date. Every open item needs a `check_by` date and lapses on its own.
- `belt/` — the fast lane: `ledger/` is append-only evidence, `subjects/` is regenerated hourly. Never hand-edit; only `git` restores a deleted line.

Claims go between the `wanda:begin claims` and `wanda:end claims` markers. Everything under `## Notes` is the owner's and is never touched. Every claim carries the edges that justify it, so "why does wanda think this" is always one hop away.

<!-- wanda:begin index -->
<!-- wanda:end index -->
