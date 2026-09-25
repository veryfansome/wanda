---
name: enrich
description: Before I finish, I link what I wrote this session to what was already here
---

# Before I finish

I have recorded what this arrival contained, mostly as new nodes. A new node with no edges to what was already here can be found only by someone who already knows to look for it, and in the next session I will not. A rule saved without an edge to the thread it governs is the costliest case: reading the thread in a later session, I have no way to know the rule is there.

I do this once, at the end, after recording.

1. I look at what I wrote this session.

2. For each thing, I ask what already in the store it bears on: the people in it, the thing it is about, the thread it advances or constrains. I look before I assume there is nothing. `mem search <word>` finds nodes by text; `mem recall <name>` walks out from what I name; `mem show <id>` reads one.

3. Where there is a relation, I write the edge:

       mem relate --subject <id> --rel <relation> --object <id>

   The relations that earn their place:
   - `constrained_by` — a thread and the rule that governs it: a request to keep it from someone, an instruction about how to handle it, a preference about that kind of thing
   - `advances` — an arrival and the thread it moves along
   - `same_as` — one thing under two names, and only when they are truly one thing. Two threads about one matter are not
   - several threads about one matter stay separate, because each carries its own state, and all of them `involves` one `topic:` node for the matter. I make the topic if it does not exist — `mem entity --kind topic --name "<the matter>" --summary "<one line>" --body "<the rest>"` — and relate to it by id. Recall converges on it, so when I find one thread I find the others
   - `involves` — who or what a thing is about, when the edge is missing

4. If a thread is subject to a constraint — someone asked for it to be kept from someone, or said how it should be handled — I put that in the thread's own summary as well (`mem rename <id> --summary "..."`). The index shows summaries; it does not show bodies or edges. Reading only the index in a later session, I must still see it.

5. What I myself said and did. The exchange is in the transcript for a month, but the store holds nothing of it unless I file it, and asked in a later session what I suggested, or what I undertook to do, I have only the store to go on. These notes are mine and in my voice, so what I did, I write in the first person, with "I" as the one who did it. I ask of the answer I am about to give, and of what I did this session:

   - a suggestion, a claim, or a flag — the fact of it, as an event, involving whoever it was for:

         mem event --summary "I <suggested what, to whom>" --body "<the rest>" --participants <name>

   - an undertaking, something I said I would do by some time — a trajectory, with `--by` the date and `--about me` as well as the person, because my own commitments are the one thing that links to my own node:

         mem trajectory --summary "I <undertook what>" --body "<the rest>" --expect "<what would close it>" --by <YYYY-MM-DD> --about me,<name>

   - something I did on an undertaking advances that trajectory (`mem advance <id> --note "I <did what>"`), and is not a second node.

   Nothing for a greeting, an acknowledgement, a question I was asked, or the fact that I filed things: the transcript has those. I never put `involves` on an event to my own node — an edge from everything I ever said would make me the hub of the whole store, and the "I" is what marks a note as mine. The node gets `made: <session>` by itself; `mem session <that>` shows the exact words later.

I do not link everything to everything. An edge that says nothing a reader would not already assume is noise. Two or three good edges a session is normal. None is a reason to look again, not a result.
