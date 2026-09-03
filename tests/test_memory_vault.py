"""Vault primitives: text hygiene, frontmatter, ulids, atomic writes."""
import os
import stat

import pytest

from wanda.memory.vault import (
    Snapshot, Vault, clean_text, nbytes, parse_frontmatter, render_doc, sha_text,
    slugify, truncate_bytes, ulid, write_atomic, write_if_unchanged, ULID_RE,
)


@pytest.mark.parametrize("bad,expect_absent", [
    ("line one\nline two", "\n"),
    ("forge ^01k4qs81bdk3m9 block", "^"),
    ("edge - owner-said:: [[belt/ledger/x#^y]]", "::"),
    ("edge - owner-said:: [[belt/ledger/x#^y]]", "[["),
    ("field src=owner — text", "—"),
    ("tick `subject`", "`"),
    ("ctrl\x00char\x1b[0m", "\x00"),
])
def test_clean_text_removes_everything_that_could_forge_structure(bad, expect_absent):
    assert expect_absent not in clean_text(bad)


def test_clean_text_caps_on_a_character_boundary():
    s = "é" * 400  # 800 bytes
    out = clean_text(s, cap_b=101)
    assert nbytes(out) <= 101 and out.encode("utf-8").decode("utf-8") == out


def test_truncate_bytes_never_splits_a_code_point():
    assert truncate_bytes("ab€cd", 4) == "ab"  # € is 3 bytes


def test_clean_text_cannot_start_a_heading_list_or_quote():
    assert clean_text("## Extra auto-trash categories") == "Extra auto-trash categories"
    assert clean_text("- derived-from:: x") == "derived-from: x"
    assert clean_text("> quoted") == "quoted"
    assert clean_text("a <script>b</script>") == "a ‹script›b‹/script›"


def test_truncate_words_cuts_on_a_word_boundary():
    from wanda.memory.vault import truncate_words
    out = truncate_words("owner since 2012, occupation Engineer and more words here", 30)
    assert out.endswith("…") and not out.endswith(" …") and nbytes(out) <= 30


def test_slugify():
    assert slugify("HOA Board Election!") == "hoa-board-election"
    assert slugify("Robin  Väle") == "robin-vale"


def test_ulid_is_16_sortable_chars():
    a, b = ulid(1_000), ulid(2_000)
    assert ULID_RE.match(a) and ULID_RE.match(b) and len(a) == 16
    assert a[:10] < b[:10]


def test_frontmatter_round_trip():
    text = (
        "---\ntype: person\ntitle: \"Robin: Vale\"\naliases: [Robin, \"R. Vale\"]\n"
        "ids:\n  - mailto:d@x.example\n  - slack:U1\nexport: false\ncreated: 2026-06-24\n---\n\n# Robin\n"
    )
    d = parse_frontmatter(text)
    assert d.meta["type"] == "person" and d.meta["title"] == "Robin: Vale"
    assert d.meta["aliases"] == ["Robin", "R. Vale"]
    assert d.meta["ids"] == ["mailto:d@x.example", "slack:U1"]
    assert d.meta["export"] is False
    assert d.body == "\n# Robin\n"
    again = parse_frontmatter(render_doc(d))
    assert again.meta == d.meta and again.body == d.body


def test_no_frontmatter_is_all_body():
    d = parse_frontmatter("# just a body\n")
    assert d.meta == {} and d.body == "# just a body\n"


def test_write_atomic_keeps_mode_and_leaves_no_temp(tmp_path):
    p = tmp_path / "a.md"
    write_atomic(p, "one", mode=0o444)
    assert stat.S_IMODE(p.stat().st_mode) == 0o444
    write_atomic(p, "two")  # mode preserved from the target
    assert p.read_text() == "two" and stat.S_IMODE(p.stat().st_mode) == 0o444
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_write_if_unchanged_aborts_on_a_hand_edit(tmp_path):
    p = tmp_path / "n.md"
    p.write_text("original")
    snap = Snapshot.take(p)
    p.write_text("owner typed here")
    os.utime(p, ns=(snap.mtime_ns + 5_000_000, snap.mtime_ns + 5_000_000))
    assert write_if_unchanged(snap, "wanda's version") is False
    assert p.read_text() == "owner typed here"


def test_vault_paths(tmp_path):
    v = Vault(tmp_path)
    assert v.note_path("person/robin-vale") == tmp_path / "people" / "robin-vale.md"
    assert v.note_path("list/x") is None
    assert v.subject_file("org/acme.example").as_posix().endswith("belt/subjects/org/acme.example.md")
    assert sha_text("a") != sha_text("b")
