---
name: check-code-comments
description: Before a commit, read every comment and docstring in the files it touches and report the ones a first-time reader would not be served by
---

# What this is for

A comment here gives the reason the code is the way it is. That is worth writing and worth keeping. What creeps in beside it is the story of how the reason was arrived at, detail from the day it was written, and wording that reads well to whoever wrote it and to nobody else.

The reader to write for is someone opening the file for the first time, who was not present for any of it.

# The three questions

Ask each of a comment, in this order. Any one of them failing is a finding.

**1. Would a reader of this code be worse off without it?**

Cut a measurement taken on one occasion, a round or run name, a path on one machine, a commit hash, a date, and the history of a decision when only its current reason matters. Keep the reason.

> `Deprecated. Run once, on the round-14 store, 237 nodes.`

Every fact in that is true and none of it helps someone reading the function. What they need is that it converts an id shape nothing still uses.

**2. Would a first-time reader follow it?**

Vocabulary particular to this project is fine where the file is about it, or where it is explained as it is used. It is not fine dropped bare in the middle of a function.

> `# the belt, where the run left it`

`transcript.rs` opens by saying what the belt is, so it can use the word. A comment three files away cannot.

**3. Is it said the shortest true way?**

A fact stated by negating something else, a clause that repeats the line above it, a flourish at the end of an otherwise plain sentence, a sentence that could lose half its words.

> `This is a reading, not a grep.`

Say what it is. What it is not is rarely the useful half.

# What does not count

**The reason is the point, and length is not the test.** A long comment that says why a constant is what it is, what a rule protects against, or which invariant a line holds up, is doing its job. Do not report it for being long.

Specifically keep:

- a named failure the code prevents, including one that has happened
- a constant whose value is not self-evident, with where it came from
- a difference between languages, versions or platforms that the code works around — these are the least guessable and the most expensive to rediscover
- an invariant a later edit could break without noticing

Do not report a comment for being informal, for using a dash, or for having a voice.

# Reporting

One finding per comment: the file and line, the comment quoted, which of the three questions it fails, and a replacement written out. A proposed rewrite is the useful part — a report that only says "too detailed" leaves the work undone.

Say plainly when there is nothing to report.
