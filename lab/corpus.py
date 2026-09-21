"""Parse a markdown history file into a list of entries.

Fields are `date | channel | speaker | text`. A line starting `-` is an
ordinary entry; a line starting `?` is an entry too, flagged so callers can
treat it specially. Indented `key: value` lines below a `?` attach to it. A
`## ` heading names the scene the entries under it belong to.

Dates are authored absolute so the file stays readable, and every one of them
is shifted at parse time so the last date in the file lands on the anchor —
today, unless a caller pins it. Nothing is fixed to a calendar date, so the
distance between the story and the present is the same every time instead of
growing by a day each day.

A date fixed in the text and a date computed when the code runs are two
different timelines, and a relative reference — "yesterday" — resolved against
the wrong one lands far away from the entry it belongs to. Shifting everything
together leaves only one timeline to resolve against.

Prose has to move with the timeline or it desynchronises from it, and the text
refers to dates in words. Those references are written as markers naming the
authored date and how to render it:

    {{2026-12-11|dom}}   the 11th
    {{2026-11-02|dm}}    2 November
    {{2026-11-02|dmy}}   2 November 2026
    {{2026-10-27|iso}}   2026-10-27
    {{2026-12-11|mo}}    December     (the month of a thing: anchor to the thing's date)
    {{2026-11-01|month}} November     (a bare month naming no entry, so it has nothing to drift from)
    {{2026-10-27|d}}     27th         (a bare ordinal, for "the 26th and 27th")

A `mo` marker must be anchored to the date of the thing it names, not to the
first of the month. The shift is a whole number of weeks, so a month anchored
to the first and an entry later in that month land in different months whenever
a week's shift puts a month boundary between them; anchoring both to the same
date keeps them together. `month` is for the few references that name a month
and no entry; lint checks that every `mo` anchors to an entry or to a date the
file names.

The authored date stays visible to a reader, and the rendered form follows the
shift, so a reference in prose and the entry it refers to can never drift apart.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

MARKER = re.compile(r"\{\{(\d{4}-\d{2}-\d{2})\|(dom|dm|dmy|mo|month|d|iso)\}\}")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _render(d: date, style: str) -> str:
    if style == "dom":
        return f"the {_ordinal(d.day)}"
    if style == "dm":
        return f"{d.day} {d.strftime('%B')}"
    if style == "dmy":
        return f"{d.day} {d.strftime('%B %Y')}"
    if style in ("mo", "month"):
        return d.strftime("%B")
    if style == "d":
        return _ordinal(d.day)
    return d.isoformat()


def shift_text(text: str, delta: timedelta) -> str:
    """Render every marker in a line against the shifted timeline."""
    def one(m: re.Match) -> str:
        return _render(date.fromisoformat(m.group(1)) + delta, m.group(2))
    return MARKER.sub(one, text)

LINE = re.compile(r"^([-?])\s+(\d{4}-\d{2}-\d{2})\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(.+)$")
EXPECT = re.compile(r"^\s+(should not|should|budget):\s*(.+)$")


@dataclass
class Input:
    date: str
    channel: str
    speaker: str
    text: str
    scene: str
    is_checkpoint: bool = False
    should: list[str] = field(default_factory=list)
    should_not: list[str] = field(default_factory=list)
    budget: int = 5

    @property
    def thread(self) -> str:
        """`thread:<name>` is a Slack thread everyone in it reads, wanda
        included; the name ties its messages together."""
        return self.channel[7:] if self.channel.startswith("thread:") else ""


def parse(path: Path, delta: timedelta | None = None) -> list[Input]:
    """Entries in file order. Scenes may share dates and overlap deliberately;
    sort by date (below) so a consumer reads a coherent history.

    `delta` shifts every date and every prose marker. Callers parsing more than
    one file must compute it once across all of them and pass the same value, or
    the files land on different timelines — use `anchor_delta`."""
    out: list[Input] = []
    scene = ""
    d = delta or timedelta(0)
    for raw in path.read_text().splitlines():
        if raw.startswith("## "):
            scene = raw[3:].strip()
            continue
        m = LINE.match(raw)
        if m:
            mark, datestr, channel, speaker, text = m.groups()
            when = (date.fromisoformat(datestr) + d).isoformat()
            out.append(Input(when, channel, speaker, shift_text(text.strip(), d),
                             scene, mark == "?"))
            continue
        m = EXPECT.match(raw)
        if m and out and out[-1].is_checkpoint:
            key, val = m.groups()
            if key == "budget":
                out[-1].budget = int(val.strip())
            else:
                items = [shift_text(s.strip(), d) for s in val.split(";") if s.strip()]
                (out[-1].should_not if key == "should not" else out[-1].should).extend(items)
    return out


def replay_order(inputs: list[Input]) -> list[Input]:
    """Chronological, with a stable tiebreak so a `?` entry sorts after the
    plain entries it shares a date with: it is meant to be read with everything
    else said that day.

    A thread is the exception: its messages stay in file order whether or not
    they are `?` entries, because a later message in an exchange can be the one
    that matters and the message after it not, and a thread read out of order is
    a different exchange."""
    def key(p):
        i = p[1]
        return (i.date, 0 if i.thread else int(i.is_checkpoint), p[0])
    return [i for _, i in sorted(enumerate(inputs), key=key)]


def anchor_delta(paths: list[Path], anchor: date | None = None) -> timedelta:
    """How far to move the timeline so its last date lands on the anchor.

    Computed across every file at once: files that end on different dates would
    each be shifted by a different amount, stacking their timelines on top of
    each other."""
    last = max(date.fromisoformat(m.group(2))
               for p in paths for raw in p.read_text().splitlines()
               if (m := LINE.match(raw)))
    # a whole number of weeks, so every weekday survives the shift. A line that
    # names a weekend about a date authored on a Saturday becomes impossible if
    # an arbitrary shift lands that date on a Friday — a contradiction nobody
    # wrote. Landing up to six days short of the anchor costs nothing next to
    # that.
    raw_delta = ((anchor or date.today()) - last).days
    # and always short of the anchor, never level with it. One shift in seven
    # is an exact number of weeks, which would put the last entry on the day
    # the run happens — and a date the story contains that equals the system
    # date is the one thing `mem`'s scrub treats as read off the clock.
    slack = raw_delta % 7 or 7
    return timedelta(days=raw_delta - slack)


def digest(paths: list[Path]) -> str:
    """What the history was when the arrivals were taken from it. A position
    only means something against that: insert one line and every position
    after it names a different arrival, which nothing downstream could see."""
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def payload(paths: list[Path], anchor: date | None = None) -> dict:
    """The run's inputs, and the anchor they were shifted to.

    Each input carries the position it will be replayed in. That position is
    what a result is keyed on afterwards: a name is not unique — several
    entries share one — and a date moves with the anchor, so neither can say
    which entry a record came from."""
    delta = anchor_delta(paths, anchor)
    inputs = replay_order([i for p in paths for i in parse(p, delta)])
    return {
        "anchor": (anchor or date.today()).isoformat(),
        "history": digest(paths),
        "inputs": [{"id": n, "date": i.date, "channel": i.channel,
                    "speaker": i.speaker, "text": i.text, "scene": i.scene,
                    "is_checkpoint": i.is_checkpoint}
                   for n, i in enumerate(inputs, 1)],
    }


def expectations(paths: list[Path], anchor: date) -> dict[int, Input]:
    """The same entries by the same position, for whoever scores the results.
    Shifted to the anchor the run recorded, because an expectation names dates
    and they moved with it."""
    delta = anchor_delta(paths, anchor)
    inputs = replay_order([i for p in paths for i in parse(p, delta)])
    return {n: i for n, i in enumerate(inputs, 1)}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="the inputs of a history, as JSON")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--anchor", default="", help="the day the last entry lands on; default today")
    a = ap.parse_args()
    out = payload([Path(f) for f in a.files],
                  date.fromisoformat(a.anchor) if a.anchor else None)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
