"""Build what a session is given, so the container is handed a binary.

    docker compose run --rm -T builder python3 /work/lab/build.py

Three binaries are mounted into a session's container, with the texts that
ship into a vault beside them. A binary carries no comments and no docstrings,
so the prose that explains the instrument is absent rather than policed. What
a reader can still find is string literals, and `lint.py` reads those out of
the file exactly as anything else would.

This runs in `builder`, the stock Rust image, because it compiles. The lab
image is left alone: rebuilding that also pulls a newer Claude Code CLI, which
is most of a session's standing context, so a crate change and a CLI change
would arrive together and a reading that moved would not say which moved it.

The revision goes to stdout, so the run command takes it from here rather than
from `git rev-parse`. That is the whole staleness guard: the only way to get a
revision is to compile, and what compiled is what mounts.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
BIN = ROOT / "bin"
# the texts that ship into a vault. They are the product's, not this harness's,
# so they live beside it and the outer harness reads the same files.
TEMPLATES = REPO / "memory" / "templates"
# an experiment changes one of them without editing it: a variant holds only
# the files that differ, and is resolved over the defaults here, where what
# ran can be seen and hashed rather than inferred
VARIANTS = ROOT / "variants"
# the three mounted into a session's container, and nothing else. `mem` is the
# CLI a session drives; `run` and `replay` put arrivals to it. Each finds the
# other two beside itself, which is why they mount into one directory.
BINARIES = ("mem", "run", "replay")
# the store as a Python module, for judge, rebuild and obsidian. Not mounted
# into a session's container — it is built here because this is where the
# toolchain is. cargo names a cdylib for the platform; Python imports it under
# the name it is given, so it is staged as `memory.so` on both.
MODULE_BUILT = ("libmemory.so", "libmemory.dylib")
MODULE = "memory.so"
# what the binaries were built from, so lint can refuse to speak for a build
# whose source has moved on since
SOURCE_DIRS = ("memory/src", "lab/harness/src")
SOURCE_FILES = ("Cargo.toml", "Cargo.lock", "memory/Cargo.toml", "memory/build.rs",
                "lab/harness/Cargo.toml")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def sources() -> dict[str, str]:
    """Every file the binaries are compiled from, by digest.

    A build and an edit afterwards leave a tree whose revision still reads
    clean, because the revision was taken when the build ran. This is what
    catches that."""
    found = {}
    for d in SOURCE_DIRS:
        for f in sorted((REPO / d).rglob("*.rs")):
            found[str(f.relative_to(REPO))] = sha(f)
    for f in SOURCE_FILES:
        p = REPO / f
        if p.is_file():
            found[f] = sha(p)
    return found


def revision() -> str:
    """The commit, and whether the tree it was taken from had edits in it.

    A round run against uncommitted changes is still a round worth taking, but
    its report has to say so: `93064fb-dirty` names something no one can check
    out, which is the honest answer when the code is not in the history."""
    def git(*args: str) -> str | None:
        """None when git could not answer, which is not the same as a clean
        tree: rev-parse needs only refs while status also needs a readable
        index, so they fail apart, and reading a failed status as `clean`
        would print a checkoutable revision for a tree that has edits in it."""
        r = subprocess.run(("git", *args), cwd=REPO, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    head = git("rev-parse", "--short", "HEAD")
    status = git("status", "--porcelain")
    if head is None or status is None or not head:
        return ""
    return f"{head}-dirty" if status else head


def compile_all() -> Path | None:
    """Everything, before anything moves. A half-written bin/ is a round
    against a mixture of old and new, and no revision describes that."""
    target = Path(os.environ.get("CARGO_TARGET_DIR") or REPO / "target")
    # the compiler writes the source path of every panic site into the file,
    # absolute. Without this the binaries a session can read carry the layout
    # of whichever machine built them, down to the home directory.
    home = os.environ.get("CARGO_HOME") or str(Path.home() / ".cargo")
    # the crate roots go too, not only the path above them: what is left is
    # `bin/run.rs`, which still says where a panic happened without naming the
    # directory it happened in. `harness` is a word about the experiment, and
    # lint would otherwise pass it only because a source path is excluded.
    flags = " ".join(filter(None, [
        os.environ.get("RUSTFLAGS", ""),
        # both spellings: cargo gives rustc a path relative to the workspace
        # root for a member crate and an absolute one for everything else
        *(f"--remap-path-prefix={p}=" for d in ("lab/harness", "memory")
          for p in (f"{REPO / d}/src", f"{d}/src")),
        f"--remap-path-prefix={REPO}=",
        f"--remap-path-prefix={home}=deps",
    ]))
    r = subprocess.run(
        ["cargo", "build", "--release", "--workspace", "--features", "memory/python"],
        cwd=REPO, env={**os.environ, "RUSTFLAGS": flags})
    if r.returncode != 0:
        return None
    return target / "release"


def main() -> int:
    head = revision()
    if not head:
        print("git cannot name this tree, so a build here could not be traced "
              "back to code; run this in the builder service", file=sys.stderr)
        return 2

    variant = os.environ.get("LAB_VARIANT", "")
    vdir = VARIANTS / variant if variant else None
    if vdir is not None and not vdir.is_dir():
        print(f"no variant at {vdir}", file=sys.stderr)
        return 2
    defaults = sorted(TEMPLATES.glob("*.md"))
    if not defaults:
        print(f"no templates at {TEMPLATES}", file=sys.stderr)
        return 2
    if vdir is not None:
        stray = [f.name for f in vdir.glob("*.md") if not (TEMPLATES / f.name).is_file()]
        if stray:
            # a variant file no default matches is a misspelling that would
            # otherwise be resolved over nothing and silently not apply
            print(f"{vdir} has {', '.join(stray)}, which no default matches",
                  file=sys.stderr)
            return 2

    # before the compile, not after: an edit made while it runs would otherwise
    # be recorded as what produced the binary. Recorded early, lint reports a
    # build that no longer matches its source, which is the safe direction.
    src_digests = sources()
    built = compile_all()
    if built is None:
        print("the crate did not build", file=sys.stderr)
        return 1
    module = next((n for n in MODULE_BUILT if (built / n).is_file()), None)
    absent = [n for n in BINARIES if not (built / n).is_file()]
    if module is None:
        absent.append(" or ".join(MODULE_BUILT))
    if absent:
        print(f"the build left no {', '.join(absent)} in {built}", file=sys.stderr)
        return 1
    want = {n: n for n in BINARIES} | {module: MODULE}

    BIN.mkdir(exist_ok=True)
    # staged inside bin/, which is gitignored: a build killed before cleanup
    # leaves the directory behind, and anywhere else that makes the tree dirty
    # and every later build reports a revision nobody can check out
    # Docker creates a missing bind source as a directory, and moving onto one
    # puts the file inside it and calls that success. Checked for all of them
    # before any of them moves: a half-replaced bin/ is a round against a
    # mixture of old and new, and no revision describes that.
    blocked = [as_ for as_ in want.values() if (BIN / as_).is_dir()]
    if blocked:
        many = len(blocked) > 1
        print(f"{', '.join(str(BIN / b) for b in blocked)} "
              f"{'are directories' if many else 'is a directory'}. Docker makes one "
              f"when the lab service is started before a build; remove "
              f"{'them' if many else 'it'} and build again, or the mount is empty "
              f"and nothing says so.", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(dir=BIN) as tmp:
        staged = Path(tmp)
        for n in want:
            shutil.copy2(built / n, staged / n)
        for n, as_ in want.items():
            os.replace(staged / n, BIN / as_)

    # the effective templates, resolved and written whole, so the container is
    # given one directory and nothing in it has to be worked out at run time
    eff = BIN / "templates"
    if eff.is_file():
        eff.unlink()
    eff.mkdir(exist_ok=True)
    # the directory is mounted whole, so anything left in it is handed to a
    # session — including a file no check looks at
    for f in eff.iterdir():
        if f.is_file():
            f.unlink()
        else:
            shutil.rmtree(f, ignore_errors=True)
    used: dict[str, str] = {}
    for d in defaults:
        src = vdir / d.name if vdir is not None and (vdir / d.name).is_file() else d
        text = src.read_bytes()
        (eff / d.name).write_bytes(text)
        used[d.stem] = hashlib.sha256(text).hexdigest()

    stamp = {
        "rev": head,
        "binaries": {n: sha(BIN / n) for n in BINARIES},
        "module": sha(BIN / MODULE),
        "sources": src_digests,
        # which texts a session actually met, by content rather than by commit:
        # a variant is selected at build time and a report has to name it
        "variant": variant,
        "templates": used,
    }
    (BIN / "BUILD.json").write_text(json.dumps(stamp, indent=2) + "\n")

    # anything from an earlier build that is no longer mounted must not be left
    # behind to be mounted: the lab's mount list names files, and a stale one
    # under a name still on that list would be handed to a session
    keep = {*BINARIES, MODULE, "BUILD.json", "templates"}
    for stale in BIN.iterdir():
        if stale.name in keep:
            continue
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        else:
            stale.unlink()

    # stdout is the revision and nothing else: the run command substitutes it
    print(head)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
