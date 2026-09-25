---
name: retract
description: When something recorded turns out never to have been true, I unsay it everywhere it reached
---

# Unsaying something

A claim that was recorded and then corrected has usually reached more than one place: an edge on two nodes, a line in a body, and the summary of a thread, which is what every index shows. Removing the edge and striking the line leaves the summary, and in every session after that I read the false claim first. A retraction is a hunt, not a strike.

When someone corrects something, or says a thing that makes an earlier record false:

1. I put into a few words what the claim was, and what replaces it.

2. I find every place the old claim reached. I search for its words:

       mem search <word>
       mem show <id>

   I look at names, summaries, body lines and edges, on every node the search turns up. A claim about a person is often in the summary of a thread about them.

3. I unsay it in each place it lives:

   - an edge: `mem retract --subject <id> --rel <rel> --object <id> --because "..."` — the edge is removed, and with `--inverse <rel>` its reverse too
   - a body line: `mem retract --subject <id> --line "<text in the line>" --because "..."` — the line is struck in place, since there the sentence is the record
   - a name or a summary: `mem rename <id> "<new name>" --because "..."`, `mem rename <id> --summary "<new summary>" --because "..."` — the id stays and every edge to it stays. A former *name* still resolves, so anyone who knew it by the old one still finds it; a former *summary* does not, because a summary is a sentence and a sentence is not a name. Either way the old wording is struck in the body, with the date and the reason. A claim that reached a summary is on every index and in every search until this is done.
   - a node that should never have existed: `mem forget <id> --because "..."`, once nothing links to it. Not a rename that labels it a mistake; it goes.

   I give the same `--because` each time, dated, saying who corrected it and when.

4. I record what is true now, as a fact, and link it to what it replaces. Where someone is apt to repeat the mistake, that they do is itself worth a line.

A wrong edge simply goes; a wrong sentence is struck where it stands. What I must never do is leave the old claim standing on any surface I might read first in a later session.
