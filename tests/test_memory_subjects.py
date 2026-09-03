"""Subject minting, nearest-match resolution, and recurrence keys."""
import pytest

from wanda.memory.subjects import (
    keys_for, parse_subject, registrable_domain, resolve, subject_from_address, subject_shape,
)


@pytest.mark.parametrize("hdr,expected", [
    ("Robin Vale <Robin.Vale@Fairview-HOA.example>", "person/robin.vale@fairview-hoa.example"),
    ("noreply@sunnybrook.example", "org/sunnybrook.example"),
    ("Riverside <info@mail.riversidelanguageacademy.org>", "org/riversidelanguageacademy.org"),
    ("HOA <alerts@hoa.co.uk>", "org/hoa.co.uk"),
    ("", None),
    ("no address here", None),
])
def test_subject_from_address(hdr, expected):
    assert subject_from_address(hdr) == expected


def test_list_mail_is_an_org_even_from_a_personal_address():
    assert subject_from_address("jane@school.example", list_id="<parents.school.example>") == "org/school.example"


def test_spoofed_local_part_never_lands_on_the_family_key():
    a = subject_from_address("mei.delgado@icloud.com")
    b = subject_from_address("mei.delgado@evil.example")
    assert a != b and a == "person/mei.delgado@icloud.com"


def test_registrable_domain():
    assert registrable_domain("a.b.example.com") == "example.com"
    assert registrable_domain("x.y.co.uk") == "y.co.uk"
    assert registrable_domain("example") == "example"


@pytest.mark.parametrize("s,ok", [
    ("person/robin-vale", True), ("person/robin.vale@x.example", True), ("org/acme.example", True),
    ("topic/hoa-board-election", True), ("Person/X", False), ("person/", False), ("nope", False),
    ("person/mei delgado", False), ("thing/x", False),
])
def test_parse_subject(s, ok):
    assert (parse_subject(s) is not None) is ok


def test_resolve_exact_alias_near_miss():
    existing = {"topic/hoa-board-election", "person/robin-vale", "org/acme.example"}
    aliases = {"topic/election": "topic/hoa-board-election"}
    assert resolve("topic/hoa-board-election", existing, aliases).how == "exact"
    assert resolve("topic/election", existing, aliases).key == "topic/hoa-board-election"
    near = resolve("topic/hoa-election", existing, aliases)
    assert near.how == "near" and near.key == "topic/hoa-board-election"
    typo = resolve("topic/hoa-board-elections", existing, aliases)
    assert typo.how == "near" and typo.key == "topic/hoa-board-election"
    miss = resolve("topic/kitchen-remodel", existing, aliases)
    assert miss.how == "miss" and miss.key == "topic/kitchen-remodel"
    # People are strict: a different person with a similar name is a miss.
    assert resolve("person/robin-vales", existing, aliases).how == "near"
    assert resolve("person/kevin-vale", existing, aliases).how == "miss"
    # An address is exact by construction — never fuzzy-merged.
    assert resolve("person/robin.vale@x.example", existing, aliases).how == "miss"


def test_subject_shape_collapses_dates_and_prefixes():
    assert subject_shape("Re: [Sunnybrook] September closure dates") == subject_shape("FWD: October closure dates")
    assert subject_shape("Invoice #4471") == "invoice #"


def test_keys_for():
    keys = keys_for("org/sunnybrook.example", "mail-pattern", from_addr="noreply@sunnybrook.example",
                    list_id="<news.sunnybrook.example>", subject_hdr="September closure dates")
    assert "key:org/sunnybrook.example|mail-pattern" in keys
    assert "addr:noreply@sunnybrook.example" in keys and "dom:sunnybrook.example" in keys
    assert "list:news.sunnybrook.example" in keys and "shape:closure dates" in keys
