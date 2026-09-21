"""What a round costs, round over round, and a warning when one runs hot.

Every session's cost comes from Claude Code's own per-session estimate, which
`run.py` prints into the run log as `cost=`. On the subscription that number
was invisible; on usage credits it is the bill. A round is four runs of the
corpus, so what is compared is cost per session — a corpus that grows makes
a round dearer without anything running hotter.

    python3 lab/spend.py            the table, from run logs and the ledger
    python3 lab/spend.py --record   add this machine's finished runs to the ledger

The ledger, `runs/spend.jsonl`, is one line per finished run. It sits with the
readings and is not committed, so clearing `runs/` clears the trend with it:
run logs are cleaned up between rounds, and a trend that only exists while
the logs do is not a trend. A run prints its own cost on its last stderr line;
this reads the logs, and `check()` puts one run against the ledger's earlier
rounds and warns above the threshold.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEDGER = ROOT.parent / "runs" / "spend.jsonl"
# every reading lives under runs/<name>/; the ledger stays with the instrument
RUNS = ROOT.parent / "runs"
WARN_OVER = 0.10   # a run more than this above the prior rounds' average is flagged

RUN_LOG = re.compile(r"run(\d+)([A-Z])\.log$")
SESSION = re.compile(r"^\[(\d+)/(\d+)\].*turns=(\d+) cost=([\d.]+)")


def read_log(path: Path) -> dict | None:
    """One run's totals from its log, or None if the run has not finished."""
    m = RUN_LOG.search(path.name)
    if not m:
        return None
    sessions = turns = 0
    cost = 0.0
    stats = None
    for line in path.read_text().splitlines():
        s = SESSION.match(line)
        if s:
            sessions += 1
            turns += int(s.group(3))
            cost += float(s.group(4))
        elif line.startswith("{") and '"elapsed_s"' in line:
            try:
                stats = json.loads(line)
            except json.JSONDecodeError:
                pass
    if stats is None:
        return None
    return {"round": int(m.group(1)), "run": m.group(2), "sessions": sessions,
            "inputs": stats.get("inputs", 0), "errors": stats.get("errors", 0),
            "turns": turns, "cost": round(cost, 2), "elapsed_s": stats.get("elapsed_s", 0),
            "lab": stats.get("lab", ""), "date": date.fromtimestamp(path.stat().st_mtime).isoformat()}


def ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]


def record() -> int:
    """Every finished run log not yet in the ledger, appended."""
    have = {(r["round"], r["run"]) for r in ledger()}
    added = 0
    for path in sorted(RUNS.glob("*/run*.log")):
        row = read_log(path)
        if row and (row["round"], row["run"]) not in have:
            with LEDGER.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            added += 1
    return added


def rounds(rows: list[dict]) -> list[dict]:
    """Per-round aggregates, in round order."""
    by: dict[int, list[dict]] = {}
    for r in rows:
        by.setdefault(r["round"], []).append(r)
    out = []
    for n in sorted(by):
        rs = by[n]
        sessions = sum(r["sessions"] for r in rs)
        cost = sum(r["cost"] for r in rs)
        out.append({"round": n, "runs": len(rs), "sessions": sessions, "cost": cost,
                    "per_session": cost / sessions if sessions else 0,
                    "turns_per_session": sum(r["turns"] for r in rs) / sessions if sessions else 0,
                    "lab": rs[0].get("lab", "")})
    return out


def check(run_row: dict, prior: list[dict] | None = None) -> str:
    """One run against the average of every earlier round in the ledger.
    Returns the line to print; a WARNING line when the run is over."""
    prior = [r for r in (prior if prior is not None else ledger()) if r["round"] < run_row["round"]]
    per = run_row["cost"] / run_row["sessions"] if run_row["sessions"] else 0
    agg = rounds(prior)
    if not agg:
        return f"spend: ${per:.3f}/session, ${run_row['cost']:.2f} for the run; no earlier rounds to compare"
    avg = sum(a["per_session"] for a in agg) / len(agg)
    delta = (per - avg) / avg if avg else 0
    line = (f"spend: ${per:.3f}/session, ${run_row['cost']:.2f} for the run · "
            f"earlier rounds average ${avg:.3f}/session ({delta:+.0%})")
    if delta > WARN_OVER:
        line = "WARNING " + line + f" — over by more than {WARN_OVER:.0%}"
    return line


def main() -> int:
    ap = argparse.ArgumentParser(description="what a round costs, round over round")
    ap.add_argument("--record", action="store_true", help="add finished runs to the ledger")
    args = ap.parse_args()
    if args.record:
        print(f"recorded {record()} run(s) into {LEDGER.name}")
    rows = ledger()
    # runs on this machine that are finished but not yet recorded still show
    have = {(r["round"], r["run"]) for r in rows}
    for path in sorted(RUNS.glob("*/run*.log")):
        row = read_log(path)
        if row and (row["round"], row["run"]) not in have:
            rows.append(row)
    agg = rounds(rows)
    if not agg:
        print("no finished runs")
        return 0
    print(f"{'round':>5}  {'runs':>4}  {'sessions':>8}  {'$/session':>9}  {'turns/s':>7}  {'$ round':>8}  {'vs earlier':>10}  lab")
    for i, a in enumerate(agg):
        earlier = agg[:i]
        avg = sum(e["per_session"] for e in earlier) / len(earlier) if earlier else None
        vs = f"{(a['per_session'] - avg) / avg:+.0%}" if avg else "—"
        flag = " WARNING" if avg and (a["per_session"] - avg) / avg > WARN_OVER else ""
        print(f"{a['round']:>5}  {a['runs']:>4}  {a['sessions']:>8}  {a['per_session']:>9.3f}  "
              f"{a['turns_per_session']:>7.1f}  {a['cost']:>8.2f}  {vs:>10}  {a['lab']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
