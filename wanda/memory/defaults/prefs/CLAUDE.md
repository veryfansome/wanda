---
kind: write-spec
---
# prefs/

How the owner wants things handled. An owner rule lands in `mail-dispositions.md` (what happens to mail) or `preferences.md` (everything else); a named policy area like `filing.md` comes from filing under `pref/<slug>`. Each claim is one rule, written once, with an `about::` edge to the subject it was filed under.

Rules that decide what happens to email carry an `owner-said::` edge to the ledger line that recorded the owner's Slack message; without that edge a rule can describe, never dispose.

A rule that stops being true is superseded, not edited in place, so the history of what the owner asked for stays legible. A new disposal rule for the same sender supersedes the old one by itself; any other rule has to be superseded deliberately.

<!-- wanda:begin index -->
<!-- wanda:end index -->
