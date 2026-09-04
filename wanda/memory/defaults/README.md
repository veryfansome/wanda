# wanda's memory vault

Open this folder in Obsidian. Everything here is plain markdown and git-tracked; wanda commits on a schedule. Retiring a note leaves a redirect in its place and the body under `retired/`; a lapsed `open/` item is removed once its copy is written there. `wanda memory unretire <path>` brings either back.

- Edit anything in `people/`, `orgs/`, `topics/`, `prefs/`, `open/`. A changed claim line is treated as your word and pinned; text under `## Notes` is never touched. Notes in a subdirectory of those folders are not tracked: wanda walks only the top level of each.
- `belt/` is machine-owned; never hand-edit it, and only `git` brings back a line deleted from it. `ledger/` is append-only evidence; `subjects/` is regenerated hourly from it. To stop a pattern you see there, `forget` the claim it produced on the curated note (in Slack or with `wanda memory forget`), or delete the note — that vetoes everything on it.
- Each directory's `CLAUDE.md` is wanda's write-spec for that directory: what belongs there and how it is named. wanda edits these from the preference claims recorded in `prefs/` — yours, or ones a session concluded, never email content — and reports every change in the digest.
- `wanda memory --help` lists what the CLI can do; `git log` in this folder shows what changed and when.
