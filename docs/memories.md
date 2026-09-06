# Memory

## What this is

Memory is the project. Everything else is a thin wrapper around Claude Code.

The measure of the feature is two things: how rich the information it can hold is, and how effectively that information comes back in whatever situation wanda finds itself in. Those two properties drive the rest of the system. Email processing is an application of memory, not the thing memory serves.

Rich is about the variety of things memory can represent and connect, not the depth of any one file. "Jane and I went to lunch at the pizza place" is two people, a place, and an event that ties the three together. A system that can only keep notes about senders cannot hold that at all.

Effective recall is what makes those connections pay. Reaching one of them should reach the others: when the person mentions the pizza place months later, recalling the place brings back the lunch, and the lunch brings back Jane. Recall traverses what is connected rather than matching words against a query.

Recall does not have to be right to be worth having. It will not be right every time, and building as though it had to be would push the whole system back toward caution. What we are maximizing is how much useful information comes back, and, when what comes back is partial, whether it sets up an interaction that teaches wanda something.

Someone says they went to the pizza place for lunch. wanda may not be able to tell which place, but the person's own node and the words in an earlier event description may still reach the lunch with Jane, and that is enough to ask "the place you ate at with Jane?" The question is a good response on its own, and it is also the moment the restaurant might finally get a name. Recall that half works, followed by a question, leaves memory better than it found it. That makes clarifying questions structural rather than politeness.

The same tolerance applies to what is stored. A memory does not need rigorous evidence standing behind it. People hold impressions whose source they long ago forgot and act on them perfectly well, and asking every memory to justify itself would cost more than being occasionally wrong does. This is a lossy system that corrects itself through use, so the question about any mechanism is whether a mistake it makes is cheap to notice and cheap to fix, not whether it can be prevented.

Initiative is what makes that worth building. An assistant that acts only when asked leaves the person carrying the part that actually costs them: knowing what to ask about, and when. The work worth taking over is the emotional labor. Anticipating what is needed. Tracking commitments before they come due. Noticing when something expected has not arrived, and raising it first.

That is also what demands the most of memory. Answering a question needs retrieval, and the question tells you what to retrieve. Anticipating needs wanda to know what to look for when nobody has prompted it, which is a much harder thing to recall well. Every interaction below where wanda speaks first is a claim about how good recall has to be.

Anticipation has a mechanism, and this is the crux of it. Every time wanda sees something new, during a triage or in the middle of a conversation, the question is not only what it is but where it sits. Is this the end of something, or the middle of it? Does it imply a next step, by wanda or by the person? Most information is mid-sequence, and treating it as terminal is how an assistant ends up merely filing things.

That is a demand a store of facts does not meet. Facts sit still; the things worth anticipating are in motion. An order is placed, then shipped, then arrives, or does not. A project stalls. A message waits on a reply that has not come. A commitment approaches its date, passes it, and comes round again next year. Memory has to carry the state and the expected trajectory of those, so that a new piece of information can be recognized as advancing one, completing one, or breaking one. Nearly every interaction below is an instance of this.

Some of what initiative takes is not a memory feature in any narrow sense. A scheduler. A list of things still pending. A way to send a message. They belong here anyway, by a simple test: if the alternative is that the person has to remember it, or set themselves a reminder to deal with it, then it is part of the work we are taking over.

That inverts how the previous attempt was built. It grew out of the mail pipeline, and its center of gravity ended up being restraint: what a session must not see, what the model must not be trusted to decide, how a prompt injection is contained. Those are real concerns, and this project keeps taking them seriously, but the answer to them is almost never going to be to make memory smaller, dumber, or harder to reach. When a risk appears, the first question is what mechanism lets wanda keep the capability safely, not what capability to give up.

## How we are working on this

No design yet. First we grope out the contours of what actually passes between a person and wanda: what they say, what they expect back, and what wanda would have to know, remember, or connect for the answer to be good. Design starts once that section stops surprising us.

The section below is the working area. Add freely, in any shape. Half-formed is fine; an interaction nobody knows how to implement is more useful here than one that fits an implementation we already have in mind.

## User ↔ wanda interactions

A rough template, worth ignoring whenever it gets in the way:

```
### <short name>
Person: what they say or do, if applicable
wanda: what a good response looks like
Needs:
- what wanda has to know, remember, connect or notice for that response
- one per line
```

An interaction may begin with wanda instead of the person. Replace the first line with `Notices:` and say what made wanda speak up unprompted.

Seeds from the conversation that started this doc. They are examples of the *kind* of thing that belongs here, not a starting design. Edit or delete them.

### Standing disposition by category, not by sender
Person: "Trash all political mailers."
wanda: applies this policy to this person's email triages from then on, to senders it has never seen before.
Needs:
- a notion of a category that outlives any one sender
- a way for that category to reach the classifier at triage time, via recall

### A rule that depends on a date
Person: "Trash event reminders once the event has passed."
wanda: keeps the reminder while the event is ahead, drops it after - maybe follow up with the applicable user to ask about it.
Needs:
- knowing today's date (should come free with Claude Code)
- extracting an event date, and re-examining a message after its first look
- future task scheduling

### A rule that depends on an unrelated fact
Person: "Trash the Kidzventures newsletter, unless there is a gap in childcare coverage."
wanda: holds the newsletter while a gap is open, drops it otherwise.
Needs:
- a fact about childcare that lives somewhere else in memory entirely
- recall that reaches it while triaging a piece of mail

### Something learned in conversation, used much later
Person: mentions in passing that a person prefers texts to email.
wanda: months later, acts on it without being reminded.
Needs:
- noticing a durable fact inside ordinary conversation
- filing it where it will be found
- surfacing it in a situation that looks nothing like the one where it was learned

### Being asked why
Person: "Why did you trash that?"
wanda: names what it knew and where that came from.
Needs:
- provenance kept alongside the fact
- triage decision and reasoning saved as a memory, on the conveyor belt maybe, so it can be forgotten eventually if not needed
- recall of the reasoning path, not just the conclusion

<!-- add below -->

### Adds entities
Person: "Jane and I went to lunch at the pizza place"
wanda: adds named entities greedily, when encountered during tasks or user interactions
Needs:
- standing behavior nudge
- files under hierarchical structure under people, place, group, etc. if clear what's being referred to, or the conveyor belt if unclear
- maybe follows up to clarify which Jane? which pizza place?

### Journals
Person: "Jane and I went to lunch at the pizza place"
wanda: adds journal entry to conveyor belt greedily, when encountered during tasks or user interactions
Needs:
- standing behavior nudge
- promotion from conveyor belt to hierarchical structure under events if important.
- links events to relevant entities

### Tracks orders
Notices: order placed email during triage
wanda: begins waiting for a delivery completed message for the ordered item
Needs:
- todo list capable of tracking pending items
- expectations for how long something should be in a pending state, so escalation can happen on unexpected delay

### Tracks social obligations 1
Person: "I forgot to message mother for her birthday"
wanda: notes when the date is and schedules a recurring task to remind the user, or suggest message to send, even sending it if approved
Needs:
- future task scheduling
- ability to send emails or other messages as user

### Tracks social obligations 2
Notices: notices a personal message from real person
wanda: notifies user after triage and sets a scheduled task to check if user follows up, potentially suggesting an appropriate response, even sending it if approved
Needs:
- future task scheduling
- ability to send emails or other messages as user

### A relationship learned from one person, used with another
Person: "slack user U0XYZ is my wife"
wanda: records it once; the first time she talks to wanda, it already knows who she is to him, without her having said so
Needs:
- one relationship stored once, both readings rendered
- knowing whether a reverse reading exists at all, since not every relation inverts
- a session knowing who it is talking to, and whose memories are in front of it

### Whose policy applies
Notices: triaging her mailbox, while he has a standing "trash all political mailers" disposition
wanda: applies her dispositions to her mail, not his
Needs:
- a standing disposition belongs to the person whose mail it governs
- recall seeded on the mailbox's owner rather than on whoever last stated a policy
- a way to tell a household-wide preference from a personal one

### Asking the person a fact is about
Notices: two accounts of the same relationship disagree, and the person it concerns is reachable on Slack
wanda: asks her directly rather than only asking whoever raised it
Needs:
- reaching the subject of a fact, not only its source
- judging what the asking reveals, since asking whether she is his sister tells her that he said so
- landing the answer on the right node, minting the correction and dropping the wrong edge

### One memory, two people asking
Person: she asks about something he told wanda weeks ago
wanda: answers from it, and says where it came from
Needs:
- one store serving the household rather than a store per person
- provenance attached, so the model can weigh whether it is hers to hear
- telling a household member from a stranger

## Parked: ideas raised, not decided

Recorded so they are not lost, deliberately not worked out.

- **Hierarchies of CLAUDE.md index files.** Both triage and agent sessions are Claude Code, which loads CLAUDE.md by directory, without anyone querying for it. Sessions therefore run with the vault as their working directory; that is a requirement of the hierarchy doing any work at all, not an option. That makes the hierarchy the push half of recall, and it splits by depth: the top level carries the standing instructions that have to be in force before anything happens, and the levels below carry ambient context for wherever the session is working. A CLAUDE.md is capped, say at 200 lines, which makes these files indexes rather than storage and forces a top-level one to split into sub-indexes as it fills. That pressure is also how wanda comes to own the taxonomy: where a file splits is a decision about what kind of thing it holds, so the categories emerge from use instead of from a schema anyone declared. People, places, groups, events and projects are a starting set, not a fixed one. A well-shaped hierarchy deserves careful thought rather than being a byproduct.
- **The conveyor belt.** Better read as a class of wanted behaviors than as one mechanism: capture anything without deciding where it goes, hold what cannot be filed yet, let what nobody needed fade, keep enough recent material for a pattern to be visible, and promote what has settled into the hierarchy. Those may not all turn out to be the same thing, and their natural lifetimes differ, so expect the belt to split rather than to be tuned to one window. A rhythm needs only enough journal to see its period repeat a few times, which for a weekly call is weeks.
- **Behavior nudges.** Standing instructions about how wanda acts by default, kept as memories rather than written into code: record entities greedily, journal what happens. Two things earn one: behavior the model would not adopt on its own, and behavior the person has given feedback about. Guarding against something the model already does well is neither, so there is no nudge about asking questions naturally until someone says wanda asked badly. A person wanda knows nothing about gets no nudge either: the model's own judgment is the starting point, and their first correction is what creates their first preference. They keep a judgment call with the model instead of hardening it into a harness rule. Feedback is assumed to be explicit for now: reading irritation out of text is unreliable, and there is little reason to guess while wanda can simply be told.
- **Where a nudge lives, and how it comes back.** A nudge that modulates behavior wanda already has can be a memory like any other, recalled by a pass for etiquette and preferences that runs alongside the recall for whatever is being discussed. That pass has nothing mentioned to land on, so its seeds are the person, the kind of task, and where the conversation is happening. What earns an interruption belongs here rather than in a global rule: how much a person wants to hear from wanda is theirs, learned from what they have said and recalled through their own node. A nudge that makes wanda do something it otherwise would not is different: its absence is silent, nothing would prompt the lookup, so it has to be ambient. Those are standing top-level CLAUDE.md instructions, and the disposition at the heart of anticipation is one of them: ask of anything new whether it is terminal or mid-sequence.
- **Backoff rather than deciding a scope.** Someone saying "stop asking me so much" is most likely asking for a break, not declaring a policy, so wanda takes the narrowest reading and eases off now. The feedback sits on the conveyor belt and shapes behavior for as long as it is there. Repetition needs no special handling: fresh feedback keeps landing, so a standing irritation stays in force without anyone deciding that it should. When it rolls off, the default behavior comes back. Only an explicit request for something permanent promotes a preference off the belt into a standing memory. Asking outright is also fair, and often better than guessing. The return to default is silent: announcing it would be an interruption in its own right, and would put the mechanism in front of the person instead of the behavior.
- **Frontmatter carries the metadata.** Whatever a memory file needs to be found, ranked, trusted, or related should be encoded in its frontmatter.
- **Recall by convergence, from more than one landing node.** A situation lands on several nodes at once rather than one. A mention of the pizza place lands on the place and on the person speaking. What surfaces is what connects back to more than one of them, so the lunch outranks everything else because it touches both, and it carries Jane in with it. Ranking then falls out of how many paths lead back to the landing set and how far away they are, which is why Jane arrives with the lunch while Jane's unrelated life does not. Landing itself has three routes that fail independently: a resolved reference, full text over descriptions, and the person speaking, who is a node whether or not anything else resolves. When none of them land cleanly the fallback is to ask, and whether a given miss is worth asking about is the model's call.
- **Absence, and rhythms as recurring trajectories.** Absence falls out of a trajectory once one is open: the expectation was set when it opened, so the delivery that never arrives is noticed by the thing already waiting for it. A rhythm needs no separate machinery, being a recurring trajectory; what differs is where its expectation comes from. An order confirmation implies a delivery the day it arrives, while nothing about one lunch implies another, A trajectory should also carry how it can close: by an observation wanda will see, like a delivery notice landing in the inbox, or only by asking, like whether the Monday call happened. wanda sees inbound mail and conversation, so a good many personal trajectories are the second kind, and asking is simply what closing one looks like. A rhythm is either stated outright, "I call Jane every Monday evening on my commute", which opens a crisp trajectory at once, or induced by a scheduled pass looking for repetition in the journal. An induced one is fuzzy in both period and identity: every six to ten weeks rather than the thirtieth, and lunch with Jane, or seeing Jane, or eating at that place. The two routes meet when wanda notices a possible pattern and asks, turning an induced rhythm into a stated one. This is what greedy journaling is really for.
- **Events as the join, and what earns a line in an index.** An event is a node that connects other nodes, tying the who, the what, the when and the where together; an edge could not, having only two ends and no time. That makes events the places where paths converge, so two entities are usually related through an event rather than directly, and how densely events are recorded is most of what decides whether recall finds anything. Getting from the belt to the hierarchy is a transposition: the belt is chronological so an entry is a happening, the hierarchy is topical so the same material becomes a fact about somebody. Because index space is capped, most events never make that trip. What earns a line is the singleton that mattered, or something synthesized from many events, like the rhythm itself. Either way the events under it are not consumed by the promotion.
- **Relationships are one fact with two readings.** Telling wanda that a Slack user is your wife should make you her husband on her page, without anyone saying it twice. Two independent copies would drift, and correcting one would leave no way to tell which is right, so the relationship is recorded once and both readings are rendered from it, which the hierarchy supports by being generated rather than authored. Choosing the word for the reverse is a language problem the model handles well; deciding whether a reverse exists at all is not, since being someone's emergency contact does not make them yours. The payoff is that when she talks to wanda, wanda already knows who she is to you without her having said so, which is the first case where something one person said improves what wanda knows for somebody else. It is still one person's account of the relationship, which matters little for a marriage and more for softer claims, and is an argument for keeping who said it attached rather than for holding it back.
- **Confidence is computed, not declared.** What a claim is worth leans on three things: how old it is, who it came from and how close they stand to what it is about, and what kind of thing it asserts. Jane or the cousin saying the two are cousins beats an acquaintance saying it. This is a ranking input as much as a trust one, so the same numbers that decide what surfaces also decide what wins a disagreement.
- **What is asserted decides how it ages.** An event happened and cannot unhappen, so age never weakens it. A blood relation is permanent once true. A marriage, a job, an address or a preference is transient, true for a period, so an old one is weak evidence about now. "Joe ate at the pizza place" and "Joe likes pizza" are the same shape as sentences and behave completely differently a year later.
- **Correcting a fact.** When a new claim contradicts an old one, compare their confidence, ask when it is close, and on correction mint the new relationship and delete the old edge. Deleting loses nothing, because the statement that corrected it lands on the belt: the graph is the current best picture and the belt is the record of how it got there. A correction is not a state change, though, and the two want different handling. "Jane is my cousin, not my sister" says the old edge was never true. A divorce says the marriage was true and has ended, which closes an interval rather than deleting one, so that events during it still refer to something. A transient relation with a start and an end is the same shape as a trajectory.
- **Asking the person a fact is about.** If Jane is on Slack, wanda can ask her rather than only asking whoever raised the question, which is the first time wanda talks to someone other than the person who prompted it. It carries a disclosure judgment, since asking Jane whether she is your sister reveals that you said she was.
- **Tiering rather than compacting.** Summarizing is fine and often the point: finding a pattern and naming it is synthesis, and a synthesized memory can be more useful than the material under it. What is avoided is compaction, summarizing and then destroying the source, because that is what leaves a late correction nothing to land on. So a summary is minted as its own memory beside its evidence rather than in place of it, and material ages out by tiering: hot, then cold, then deleted. Cold means de-indexed, out of search and out of the landing set, but its edges stay live, so it can no longer come to mind on its own and can still be reached by traversal from a hot node. That is close to how remembering actually feels, and it lets the graph carry far more than could ever be rendered. Age is the belt's way of cooling things; something promoted into the hierarchy, an open trajectory above all, should not go cold merely for being old. Deleting an event is not like deleting a fact, though: events are the joins, so removing one can disconnect two entities that had no other path, which argues for events going last or for deletion leaving a weakened direct edge. A rhythm survives all of it, being a promoted trajectory rather than a summary of its evidence.
- **What recall is for at triage time.** Not classifying the message; the model reads a political mailer as a political mailer without help. Recall supplies who and what the message touches: the mailbox's owner, the sender, and the recent events and open trajectories involving either of them. A standing disposition toward a category comes back the same way a preference does, through the owner's node, which is always a landing node when triaging their mail.
- **Periodic passes.** Several things now need something running on a clock rather than in reaction to a message: promoting what has settled off the belt, checking trajectories whose expected moment has passed, inducing rhythms by looking for repetition in the journal, enriching relationships, and letting the belt roll off. `main` already had this shape, an hourly mechanical pass and a nightly one that spends a model call, and it is the natural home for anything that has to notice something when nobody is asking.
- **An enrichment process.** Something that keeps working on the relationships between memory files, so recall gets better over time rather than only at write time. Conversation is one of its channels, and maybe the best one: a question wanda asks because recall was ambiguous is also how the ambiguity gets resolved.
- **Signatures against tampering.** Hash a memory when it is minted; check the hash at recall; quarantine a mismatch and escalate it to the person rather than silently dropping it.
- **Sending as the user.** Drafting and sending a message on the person's behalf, rather than handing them a draft. Not ruled out in principle, and two interactions above want it, but not where we start.
- **Down-weighting instead of excluding.** A memory derived from an untrusted surface such as email keeps its place, with a lower confidence, presented to a session as untrusted rather than withheld.

## Parked: open questions

- What tunes the ranking: how fast distance decays, whether some kinds of edge count for more than others, and how recency weighs against connectedness. Convergence says which things surface; it does not say how much of each signal to use, and a heavily connected node like a spouse will test it.
- Does the taxonomy need a fixed core, or can wanda choose all of it? Splitting under a line cap decides the shape over time, but something has to be true on the first day, before any file has filled.
- What is worth remembering at all, and what should decay?
- What does a signature cover, and what signs it, given the daemon and its sessions share a filesystem?
- How does a new observation attach to a trajectory already in flight, when nothing in it names the earlier one? An order confirmation and its delivery notice may share nothing but a vendor.
- What opens a trajectory, what closes one, and what happens to one that simply never finishes?
- Does the etiquette pass read cold memories? A backoff works because the feedback rolls off and stops shaping behavior, but if rolling off means going cold and cold is still reachable by traversal from the person's node, the pass keeps finding it and the backoff never expires.
- Which of the belt's jobs share a mechanism and which need their own? A backoff expiring, an unresolved reference waiting to be clarified, a journal deep enough to induce a rhythm, and a triage decision kept in case someone asks why all want different lifetimes.
- What is the smallest thing that could be built and lived with, so the shape is learned from use rather than argued about?

## Prior art

The previous attempt is on `main`, and a design taken through two adversarial review rounds is on the `multi-account-plan` branch (`docs/multi-account-plan.md`). Worth mining for the belt, the vault layout and the provenance ideas. Its posture, harness-enforced restraint as the organising principle, is what this document is a reaction to.
