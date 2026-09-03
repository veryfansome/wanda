"""L2 note parsing and rendering: marker regions, claims, edges, write-specs."""
from pathlib import Path

from wanda.memory.notes import (
    Claim, Edge, claim_sha, new_note, parse_note, parse_writespec, render_region, WriteSpec,
)

SAMPLE = """---
type: person
title: Robin Vale
ids:
  - mailto:robin.vale@fairview-hoa.example
created: 2026-06-24
---

# Robin Vale

<!-- wanda:begin claims -->

Board secretary for [[orgs/california-meadows-hoa]]; runs ballots. ^c1
- tier:: session
- derived-from:: [[belt/ledger/2026-06-10#^01k2bb03aa4m7q0a]], [[belt/ledger/2026-08-26#^01k3zz40acp2n1bb]]
- about:: [[orgs/california-meadows-hoa]]

Reachable at rvale@pmcompany.example. ^c4
- owner-said:: [[belt/ledger/2026-09-02#^01k4qs81bdk3m9cc]]
- supersedes:: [[people/robin-vale#^c0]]

Out of office 2026-09-02 to 2026-09-21. ^c6
- until:: 2026-09-21

## History
> [!note]- Folded claims, kept for provenance

Reachable at robin.vale@fairview-hoa.example. ^c0
- superseded-by:: [[people/robin-vale#^c4]]

<!-- wanda:end claims -->

## Notes
Alex's own text stays here.
"""


def test_parse_claims_edges_and_history(tmp_path):
    p = tmp_path / "robin-vale.md"
    p.write_text(SAMPLE)
    n = parse_note(p)
    assert n.title == "Robin Vale" and n.meta["ids"] == ["mailto:robin.vale@fairview-hoa.example"]
    assert [c.block for c in n.claims] == ["c1", "c4", "c6", "c0"]
    c1 = n.get("c1")
    assert c1.targets("derived-from") == [("belt/ledger/2026-06-10", "01k2bb03aa4m7q0a"), ("belt/ledger/2026-08-26", "01k3zz40acp2n1bb")]
    assert c1.targets("about") == [("orgs/california-meadows-hoa", "")]
    assert c1.value("tier") == "session"
    assert n.get("c6").value("until") == "2026-09-21"
    assert n.get("c0").folded and not n.get("c4").folded
    assert n.get("c4").targets("supersedes") == [("people/robin-vale", "c0")]
    assert "Alex's own text stays here." in n.post
    assert n.next_block() == "c7"


def test_render_round_trips(tmp_path):
    p = tmp_path / "robin-vale.md"
    p.write_text(SAMPLE)
    n = parse_note(p)
    out = n.render()
    p.write_text(out)
    again = parse_note(p)
    assert [(c.block, c.text, c.folded) for c in again.claims] == [(c.block, c.text, c.folded) for c in n.claims]
    assert again.get("c1").edges == n.get("c1").edges
    assert again.post == n.post and again.meta == n.meta


def test_owner_typed_line_in_region_becomes_a_pinned_claim(tmp_path):
    text = SAMPLE.replace("Out of office 2026-09-02 to 2026-09-21. ^c6\n- until:: 2026-09-21\n",
                          "Out of office 2026-09-02 to 2026-09-21. ^c6\n- until:: 2026-09-21\n\nHe prefers text messages.\n")
    p = tmp_path / "n.md"
    p.write_text(text)
    n = parse_note(p)
    owner = [c for c in n.claims if c.minted]
    assert len(owner) == 1 and owner[0].text == "He prefers text messages." and owner[0].block == "c7"


def test_note_without_region(tmp_path):
    p = tmp_path / "plain.md"
    p.write_text("# Just a title\n\nSome prose.\n")
    n = parse_note(p)
    assert not n.had_region and n.claims == [] and n.title == "Just a title"


def test_claim_sha_ignores_wikilink_targets():
    a = claim_sha("Board secretary for [[orgs/california-meadows-hoa]]; runs ballots.")
    b = claim_sha("Board secretary for [[orgs/cal-meadows-hoa]]; runs ballots.")
    c = claim_sha("Board secretary for [[orgs/cal-meadows-hoa]]; runs elections.")
    assert a == b != c


def test_new_note_and_render_region():
    n = new_note(Path("people/x.md"), "person", "X Person", ids=["mailto:x@y.example"], created="2026-09-03")
    n.claims.append(Claim("c1", "Knows things.", [Edge("derived-from", "belt/ledger/2026-09-03", "0123456789abcdef")]))
    out = n.render()
    assert out.startswith("---\ntype: person\ntitle: X Person\nids:\n  - mailto:x@y.example\ncreated: 2026-09-03\n---\n")
    assert "<!-- wanda:begin claims -->" in out and "## Notes" in out
    assert "Knows things. ^c1\n- derived-from:: [[belt/ledger/2026-09-03#^0123456789abcdef]]" in out
    assert render_region([]).count("wanda:") == 2


def test_writespec_parse_and_render(tmp_path):
    p = tmp_path / "CLAUDE.md"
    p.write_text("---\nkind: write-spec\n---\n# people/\n\nOne note per human.\n\n<!-- wanda:begin index -->\n- a\n- b\n<!-- wanda:end index -->\n")
    ws = parse_writespec(p)
    assert ws.prose == "# people/\n\nOne note per human." and ws.index == ["- a", "- b"]
    ws.index = ["- c"]
    p.write_text(ws.render())
    again = parse_writespec(p)
    assert again.prose == ws.prose and again.index == ["- c"] and again.meta.get("kind") == "write-spec"
    # The owner's prose is preserved byte for byte; caps apply where it is loaded.
    ws.prose = "x" * 5000
    assert parse_writespec(p, ws.render()).prose == "x" * 5000
