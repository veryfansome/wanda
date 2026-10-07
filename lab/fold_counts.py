"""What the product's memory sessions did with messages added while they
worked, the reading that tells whether FOLD_LIMIT and FOLD_FOR_S in
wanda/main.py fit how the household's sessions take added messages, read
from their transcripts alone, which outlive a rebuild where the daemon's
log does not: per session, the messages it was handed after its opening
one in the product's frame (ADDED_RE, read from the parser `mem session`
reads them with), how many further turns it ran (later user entries
holding text that neither a background command's notice nor the
harness's line after an empty first answer, NOTHING_SENT_OPENS in
wanda/vault.py, began), how many of those opened with more than one
message, which Claude Code takes as one when they wait for the same
turn, and how long they took, from the first further turn's first entry
to the session's last. A line that cannot be read, as the last of a
transcript still being written can be, is passed over.

    python3 lab/fold_counts.py <directory of transcripts> [<memory/src/transcript.rs>]

The parser is this repository's unless another is named; the line's
opening words are always read from this repository's harness."""
import json, re, sys
from datetime import datetime
from pathlib import Path

if len(sys.argv) not in (2, 3):
    sys.exit(__doc__)
if not Path(sys.argv[1]).is_dir():
    sys.exit(f"no directory of transcripts at {sys.argv[1]}")
parser = Path(sys.argv[2]) if len(sys.argv) == 3 else (
    Path(__file__).resolve().parent.parent / "memory" / "src" / "transcript.rs")
src = parser.read_text()
found = re.search(r"static ADDED_RE: .*?Regex::new\((.*?)\)\.unwrap\(\)", src, re.DOTALL)
if found is None:
    sys.exit(f"no ADDED_RE in {parser}: that parser does not read messages added while a session works")
body = found.group(1)
ADDED = re.compile("".join(re.findall(r'r"(.*?)"', body, re.DOTALL)))
# the opening words of the line the harness writes into a session whose
# first answer to a direct message or a mention was empty
harness = Path(__file__).resolve().parent.parent / "wanda" / "vault.py"
opens = re.search(r'^NOTHING_SENT_OPENS = "(.*?)"$', harness.read_text(), re.MULTILINE)
if opens is None:
    sys.exit(f"no NOTHING_SENT_OPENS in {harness}: the harness's line after an empty answer cannot be told")
NOTHING_SENT = opens.group(1)


def texts(c):
    if isinstance(c, str):
        return [c]
    if isinstance(c, list) and c and all(isinstance(b, dict) and b.get("type") == "text" for b in c):
        return [b.get("text") or "" for b in c]
    return []


def at(e):
    return datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))


sessions = took = further = batched = 0
spans = []
for path in sorted(Path(sys.argv[1]).glob("*.jsonl")):
    entries = []
    for x in path.read_text(errors="replace").splitlines():
        try:
            entries.append(json.loads(x))
        except ValueError:
            continue  # a line cut off by a session still writing, or stopped mid-write
    entries = [e for e in entries if isinstance(e, dict)]
    stamped = [e for e in entries if "timestamp" in e]
    handed, turn_starts, opened, several = 0, [], False, 0
    for e in entries:
        a = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
        if e.get("type") == "attachment" and a.get("type") == "queued_command" \
                and a.get("commandMode") == "prompt" and not a.get("isMeta"):
            handed += sum(1 for t in texts(a.get("prompt")) if ADDED.match(t))
        elif e.get("type") == "user" and not e.get("isMeta"):
            said = texts((e.get("message") or {}).get("content"))
            if not said or (e.get("origin") or {}).get("kind") == "task-notification" \
                    or said[0].lstrip().startswith(("<task-notification>", NOTHING_SENT)):
                continue
            if opened:
                turn_starts.append(e)
                several += sum(1 for t in said if ADDED.match(t)) > 1
            handed += sum(1 for t in (said if opened else said[1:]) if ADDED.match(t))
            opened = True
    sessions += 1
    took += handed > 0
    further += bool(turn_starts)
    batched += several
    span = (at(stamped[-1]) - at(turn_starts[0])).total_seconds() if turn_starts and stamped else 0.0
    spans.append(span)
    print(f"{path.stem}: handed {handed}, further turns {len(turn_starts)}, {several} of them opened by more "
          f"than one message, their time {span:.1f} s")
print(f"sessions {sessions}; took a message {took}; ran a further turn {further}; further turns opened by more "
      f"than one message {batched}; further turns' time, longest {max(spans or [0]):.1f} s")
