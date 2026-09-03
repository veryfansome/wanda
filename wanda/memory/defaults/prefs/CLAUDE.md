---
kind: write-spec
---
# prefs/

How the owner wants things handled. Each note is one policy area (`mail-dispositions.md`, `filing.md`, `reply-style.md`); each claim is one rule, written once, with `about::` edges to every sender or subject it governs.

Rules that decide what happens to email carry an `owner-said::` edge to the Slack message where the owner said it; without that edge a rule can describe, never dispose.

A rule that stops being true is superseded, not edited in place, so the history of what the owner asked for stays legible.

<!-- wanda:begin index -->
<!-- wanda:end index -->
