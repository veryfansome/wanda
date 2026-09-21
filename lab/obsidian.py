"""Open a vault in Obsidian and see the store as wanda organises it.

The notes already carry what Obsidian reads — `links`, `aliases` and `tags`
derived from every node's edges, name and marks on each write — so the graph
draws itself. What this adds is the graph's settings: one colour per top-level
directory, and the generated `CLAUDE.md` indexes left out.

    python3 lab/obsidian.py runs/16A/vault_debug

A vault built by the harness has these already; this is for one that does not,
or for putting them back. Obsidian owns `<vault>/.obsidian/graph.json` and
writes its own state over it, so a vault open at the time has to be reloaded
before the settings take. Nothing else in the vault changes, and sessions never
look in the directory.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# the store, built from the Rust crate. `lab/bin` is where the build puts it,
# beside the templates it reads the vault's standing texts from.
sys.path.insert(0, str(Path(__file__).resolve().parent / "bin"))
import memory as S  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    vault = Path(sys.argv[1]).resolve()
    if not vault.is_dir():
        print(f"no vault at {vault}")
        return 2
    dirs = S.write_graph_config(vault)
    print(f"{vault.name}: {len(dirs)} colour groups ({', '.join(dirs)}), indexes left out")
    print("a vault already open in Obsidian needs a reload before this takes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
