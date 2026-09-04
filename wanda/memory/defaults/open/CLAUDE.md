---
kind: write-spec
---
# open/

Commitments and follow-ups with a date. File name `YYYY-MM-DD-<slug>.md` where the date is `check_by`. Frontmatter carries `check_by` and `about` (the subject it belongs to). The item's tier is derived from how it was opened, never declared: a `tier:` key typed here is inert.

Items open themselves from conversations and close themselves: seven days past `check_by` with nothing new, an item lapses to `retired/open/`. Only items from conversations or from the owner appear in the always-loaded "due soon" list; items opened from email content never do.

<!-- wanda:begin index -->
<!-- wanda:end index -->
