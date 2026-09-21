"""Rebuild a run's store from its recorded `mem` calls, with the current code.

A run leaves `<stem>-mem.jsonl`, one line per `mem` invocation in the order
the sessions made them — verb, argv, exit code, which input — and
`<stem>-sessions.jsonl`, which input ran under which session id. Replaying
the argv against a fresh vault, with the same simulated date and session id
each call had, rebuilds the store the run built, under whatever `mem` and
`store` now do: a new frontmatter, a changed rule, a fix. What the sessions
decided is kept; only the tool's behaviour is new.

Ids are the one thing a replay cannot reproduce by itself — they are minted
at random, and a session's later calls name the ids its earlier calls got —
so the original vault is the oracle: a node of the same kind and name, made
in the same session, gets the id it had. Every call whose exit code differs
from the recorded one is listed, since that is where the current code
disagrees with what the run did.

    python3 lab/rebuild.py --from runs/15A/vault15A --out /tmp/vault15A_debug
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# the store, built from the Rust crate. `lab/bin` is where the build puts it,
# beside the templates it reads the vault's standing texts from.
sys.path.insert(0, str(ROOT / "bin"))
import memory as S  # noqa: E402


def compare(a: Path, b: Path) -> int:
    """Two rebuilds, call for call and file for file.

    This is the gate a reimplementation has to pass: build the same store from
    the same recorded calls, print the same thing doing it. Exit codes are the
    weakest part of that — recall exits 0 whatever order it ranks in — so what
    is compared here is what a session would actually have read."""
    def load(d: Path, suffix: str):
        f = d.parent / f"{d.name}-{suffix}"
        if not f.is_file():
            print(f"no {f}; run a rebuild with --out {d} first", file=sys.stderr)
            raise SystemExit(2)
        return f

    ca = [json.loads(l) for l in load(a, "calls.jsonl").read_text().splitlines()]
    cb = [json.loads(l) for l in load(b, "calls.jsonl").read_text().splitlines()]
    ma = json.loads(load(a, "files.json").read_text())
    mb = json.loads(load(b, "files.json").read_text())
    fa, fb = ma["files"], mb["files"]
    if ma.get("mem") != mb.get("mem"):
        print(f"built by {ma.get('mem')} and {mb.get('mem')}")
    if ma["built_on"] != mb["built_on"]:
        print(f"built on different days ({ma['built_on']} and {mb['built_on']}): "
              f"mem rewrites the system date out of what it is passed, so any "
              f"difference below may be the clock rather than the code")

    if len(ca) != len(cb):
        print(f"different call counts: {len(ca)} and {len(cb)}")
    out_diff = [(x, y) for x, y in zip(ca, cb)
                if (x["rc"], x["out"], x["err"]) != (y["rc"], y["out"], y["err"])]
    print(f"calls: {len(ca)} replayed, {len(out_diff)} differ")
    for x, y in out_diff[:20]:
        what = []
        if x["rc"] != y["rc"]:
            what.append(f"rc {x['rc']}->{y['rc']}")
        if x["out"] != y["out"]:
            what.append("stdout")
        if x["err"] != y["err"]:
            what.append("stderr")
        print(f"  {x['i']:>5} {x['cmd']:<10} {' '.join(what)}  {' '.join(x['argv'][:4])[:60]}")
    if len(out_diff) > 20:
        print(f"  … {len(out_diff) - 20} more")

    only_a = sorted(set(fa) - set(fb))
    only_b = sorted(set(fb) - set(fa))
    moved = sorted(k for k in set(fa) & set(fb) if fa[k] != fb[k])
    print(f"files: {len(fa)} and {len(fb)}; {len(moved)} differ, "
          f"{len(only_a)} only in the first, {len(only_b)} only in the second")
    for k in (moved + only_a + only_b)[:20]:
        print(f"  {k}")
    return 1 if (out_diff or moved or only_a or only_b) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="rebuild a run's store from its recorded mem calls")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), default=None,
                    help="two directories a rebuild has written to; compare them "
                         "call for call and file for file, and rebuild nothing")
    ap.add_argument("--from", dest="src", help="the run's vault, e.g. runs/15A/vault15A")
    ap.add_argument("--out", default="",
                    help="where to build; default is <run>_debug/vault beside the run, "
                         "because a run's own directory is mounted whole into any "
                         "session run against it later")
    ap.add_argument("--transcripts", default="", help="the run's transcript dir, for `mem session` calls")
    ap.add_argument("--mem", default="", metavar="PATH",
                    help="the mem to replay with; default is the one the build staged. "
                         "Point it at another build and the diff says how the two differ")
    args = ap.parse_args()
    if args.compare:
        return compare(*(Path(x).resolve() for x in args.compare))
    if not args.src:
        ap.error("--from is required unless --compare is given")

    src = Path(args.src).resolve()
    # the run's logs sit beside its vault, named from `--out`'s stem. The vault
    # is not named after the run any more, so they are found rather than
    # composed; one run to a directory makes that unambiguous.
    memlogs = sorted(src.parent.glob("*-mem.jsonl"))
    if len(memlogs) != 1:
        print(f"expected one *-mem.jsonl beside {src}, found {len(memlogs)}", file=sys.stderr)
        return 2
    memlog = memlogs[0]
    stem = memlog.name[: -len("-mem.jsonl")]
    sessions = src.parent / f"{stem}-sessions.jsonl"
    for p in (memlog, sessions):
        if not p.exists():
            print(f"missing {p}", file=sys.stderr)
            return 2
    # the run's transcript directory. Claude Code names it after the vault's
    # path inside the container, which has not always been the same one, so
    # the single directory beside the run is more reliable than any name this
    # could compose.
    def belt() -> str:
        root = src.parent / "transcripts"
        kids = sorted(d for d in root.glob("-*") if d.is_dir()) if root.is_dir() else []
        return str(kids[0]) if len(kids) == 1 else str(root / "-work-runs-vault")

    transcripts = args.transcripts or belt()

    out = Path(args.out).resolve() if args.out else \
        (src.parent.parent / f"{src.parent.name}_debug" / "vault").resolve()
    if out.exists():
        import shutil
        shutil.rmtree(out)
    out.mkdir(parents=True)
    session_of = {json.loads(l)["key"]: json.loads(l)["session"]
                  for l in sessions.read_text().splitlines() if l.strip()}
    calls = [json.loads(l) for l in memlog.read_text().splitlines() if l.strip()]

    # the order each session minted its ids in, read off its transcript: the
    # `ok <id>` lines mem printed back, in sequence. Two nodes of one name in
    # one session are told apart by this
    order: dict[str, list[str]] = {}
    tdir = Path(transcripts)
    if tdir.exists():
        for sid in set(session_of.values()):
            f = tdir / f"{sid}.jsonl"
            if not f.exists():
                continue
            ids: list[str] = []
            for line in f.read_text().splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "user":
                    continue
                for b in (d.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        c = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                        ids += [x for x in re.findall(r"\bok ((?:person|place|org|group|thing|topic|event|preference|trajectory):[0-9a-f-]+)", c)
                                if x not in ids]
            order[sid] = ids
    order_path = out / ".oracle-order.json"
    order_path.write_text(json.dumps(order))

    def ran_on(c: dict) -> tuple[str, str]:
        """The date and session a recorded call ran under. Older logs carry
        neither and have a key shaped `NNN|date|speaker` to read them out of."""
        k = c.get("input_key", "")
        parts = k.split("|")
        date = c.get("date") or (parts[1] if len(parts) > 2 else "")
        return date, c.get("session") or session_of.get(k, "")

    # the build under test, which is the one the lab mounts unless another is
    # named
    mem_path = Path(args.mem).resolve() if args.mem else ROOT / "bin" / "mem"
    if not os.access(mem_path, os.X_OK):
        print(f"{mem_path} is not there to run; build first", file=sys.stderr)
        return 2
    mem_cmd = [str(mem_path)]

    def mem_digest() -> str:
        """What is about to run, by content. Every call loads it afresh, so
        rebuilding it while a replay is going leaves a store half built by
        each — which no output says, and which looks like a real difference."""
        return hashlib.sha256(mem_path.read_bytes()).hexdigest()[:16]

    mem_before = mem_digest()

    vault = S.Vault(out, oracle=S.Vault(src), oracle_order=order)
    S.seed(vault, ran_on(calls[0])[0] if calls else "")
    S.regenerate_indexes(vault)

    # flags the tool no longer has, stripped from recorded calls so that a
    # recording made under the old tool still replays under the new one;
    # the value is how many arguments each took
    REMOVED = {"relate": {"--permanence": 1, "--source": 1},
               "trajectory": {"--closes": 1, "--actor": 1}, "event": {"--actor": 1}}
    # what each call printed, normalised so that two runs under different
    # output directories still compare. Exit codes alone assert almost
    # nothing: recall exits 0 whatever it ranks, and recall is a third of the
    # corpus, so without this the harness passes a port that reordered
    # every list a session read.
    def norm(text: str) -> str:
        return text.replace(str(out), "<vault>").replace(str(src), "<oracle>")

    trace_path = out.parent / f"{out.name}-calls.jsonl"
    trace = trace_path.open("w")
    mismatches, n = [], 0
    for c in calls:
        key = c["input_key"]
        ran_date, ran_session = ran_on(c)
        argv, gone = [], REMOVED.get(c["cmd"], {})
        skip = 0
        for a in c["argv"]:
            if skip:
                skip -= 1
                continue
            if a in gone:
                skip = gone[a]
                continue
            argv.append(a)
        c = {**c, "argv": argv}
        # the clock this call actually ran under, so the date scrub removes
        # what it removed then rather than what it would remove today
        env = {**os.environ, "MEM_REAL_DATE": str(c.get("ts", ""))[:10],
               "MEM_VAULT": str(out), "MEM_DATE": ran_date,
               "MEM_SESSION": ran_session, "MEM_ORACLE": str(src),
               "MEM_ORACLE_ORDER": str(order_path), "MEM_TRANSCRIPTS": transcripts,
               # the standing text a vault is given, for whichever mem is running
               "MEM_TEMPLATES": str(ROOT.parent / "memory" / "templates")}
        env.pop("LAB_MEMLOG", None)
        p = subprocess.run([*mem_cmd, *c["argv"]],
                           capture_output=True, text=True, env=env, cwd=str(out))
        n += 1
        trace.write(json.dumps({"i": n, "key": key, "cmd": c["cmd"], "argv": c["argv"],
                                "rc": p.returncode, "out": norm(p.stdout),
                                "err": norm(p.stderr)}) + "\n")
        if p.returncode != c["rc"]:
            mismatches.append((key, c["cmd"], c["rc"], p.returncode,
                               (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]))
        if n % 200 == 0:
            print(f"  {n}/{len(calls)} calls, {len(mismatches)} mismatches", file=sys.stderr)

    trace.close()
    order_path.unlink(missing_ok=True)
    S.regenerate_indexes(vault)
    S.write_graph_config(out)
    # every file a session could read, by content. The generated CLAUDE.md
    # indexes are included because a session reads those before anything
    # else; the sqlite index is derived and is not.
    files = {str(f.relative_to(out)): hashlib.sha256(f.read_bytes()).hexdigest()[:16]
             for f in sorted(out.rglob("*.md"))}
    # the real date is an input: mem rewrites the system date out of what a
    # session passes, so two rebuilds either side of midnight are not
    # comparable and a reader has to be able to see that
    vault_path_out = out.parent / f"{out.name}-files.json"
    mem_after = mem_digest()
    vault_path_out.write_text(json.dumps(
        {"built_on": date.today().isoformat(), "mem": f"{mem_path.name}:{mem_before}",
         "files": files},
        indent=1, sort_keys=True) + "\n")
    if mem_after != mem_before:
        print(f"the implementation changed while this ran ({mem_before} -> {mem_after}): "
              f"the store is part one and part the other, and this result says nothing",
              file=sys.stderr)
    before = {nid for nid, _, _ in S.Vault(src).nodes()}
    after = {nid for nid, _, _ in vault.nodes()}
    # the differences, in a form two implementations can be diffed on. The
    # count moves with the corpus and says nothing by itself; what a port has
    # to reproduce is this set, call for call.
    diff_path = out.parent / f"{out.name}-diff.json"
    diff_path.write_text(json.dumps(
        [{"key": key, "cmd": cmd, "was": was, "now": now, "tail": tail[0][:160]}
         for key, cmd, was, now, tail in sorted(mismatches)], indent=1) + "\n")

    print(f"replayed {n} calls from {memlog.name} with {mem_path.name}: {len(mismatches)} exit codes differ")
    for key, cmd, was, now, tail in mismatches[:40]:
        print(f"  {key}  {cmd:<10} was rc={was} now rc={now}  {tail[0][:110]}")
    if len(mismatches) > 40:
        print(f"  … {len(mismatches) - 40} more")
    print(f"differences written to {diff_path}")
    print(f"{n} calls traced to {trace_path}; {len(files)} files hashed to {vault_path_out}")
    print(f"nodes: {len(before)} in {src.name}, {len(after)} in {out.name}; "
          f"{len(before & after)} with the same id, {len(after - before)} new, {len(before - after)} missing")
    for nid in sorted(before - after)[:10]:
        print(f"  missing: {nid}")
    for nid in sorted(after - before)[:10]:
        print(f"  new:     {nid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
