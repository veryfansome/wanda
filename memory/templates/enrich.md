---
name: enrich
description: Before finishing, link what this session wrote to what was already here
---

# Before you finish

You have recorded what this arrival contained, mostly as new nodes. A new node with no edges to what was already here can be found only by someone who already knows to look for it, and the next session will not. A rule saved without an edge to the thread it governs is the costliest case: a session that reads the thread has no way to know the rule is there.

Do this once, at the end, after recording.

1. Look at what you wrote this session — the list you are about to put in `recorded`.

2. For each thing, ask what already in the store it bears on: the people in it, the thing it is about, the thread it advances or constrains. Look before you assume there is nothing. `mem search <word>` finds nodes by text; `mem recall <name>` walks out from what you name; `mem show <id>` reads one.

3. Where there is a relation, write the edge:

       mem relate --subject <id> --rel <relation> --object <id>

   The relations that earn their place:
   - `constrained_by` — a thread and the rule that governs it: a request to keep it from someone, an instruction about how to handle it, a preference about that kind of thing
   - `advances` — an arrival and the thread it moves along
   - `same_as` — one thing under two names, and only when they are truly one thing. Two threads about one matter are not
   - several threads about one matter stay separate, because each carries its own state, and all of them `involves` one `topic:` node for the matter. Make the topic if it does not exist. Recall converges on it, so a session that finds one thread finds the others
   - `involves` — who or what a thing is about, when the edge is missing

4. If a thread is subject to a constraint — someone asked for it to be kept from someone, or said how it should be handled — put that in the thread's own summary as well (`mem rename <id> --summary "..."`). The index shows summaries; it does not show bodies or edges. A session that only reads the index must still see it.

5. What you yourself said and did. The exchange is in the transcript for a month, but the store holds nothing of it unless you file it, and a later session asked what you suggested, or what you undertook to do, has only the store to go on. These notes are yours and in your voice, so an act with nobody named as doing it is yours. Ask of the answer you are about to give, and of what you did this session:

   - a suggestion, a claim, or a flag — the fact of it, as an event, involving whoever it was for:

         mem event --summary "<what you suggested, to whom>" --participants <name>

   - an undertaking, something you said you would do by some time — a trajectory, with `--by` the date and `--about wanda` as well as the person, because your own commitments are the one thing that links to your own node:

         mem trajectory --summary "<what you undertook>" --expect "<what would close it>" --by <YYYY-MM-DD> --about wanda,<name>

   - something you did on an undertaking advances that trajectory (`mem advance <id> --note "..."`), and is not a second node.

   Nothing for a greeting, an acknowledgement, a question you were asked, or the fact that you filed things: the transcript has those. Never put `involves` on an event to your own node — an edge from everything you ever said would make you the hub of the whole store, and the voice is what marks a note as yours. The node gets `made: <session>` by itself; `mem session <that>` shows the exact words later.

Do not link everything to everything. An edge that says nothing a reader would not already assume is noise. Two or three good edges a session is normal. None is a reason to look again, not a result.
