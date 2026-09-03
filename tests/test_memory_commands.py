"""Owner commands: parsing, minting, refusal, and the message→line check."""
from types import SimpleNamespace

from tests.conftest import mk_obs
from wanda.memory import commands as C
from wanda.memory import index as ix
from wanda.memory.ledger import append
from wanda.memory.notes import Claim, Edge, new_note
from wanda.memory.vault import write_atomic
from wanda.store import Store

TODAY = "2026-09-03"


def ctx(text, user="U_OWNER", channel="D1", ts="10.1", sender=""):
    return C.Context(channel=channel, ts=ts, user=user, text=text, task_sender=sender)


def test_parse_command():
    assert C.parse_command("rule priya@x.example trash").verb == "rule"
    assert C.parse_command("<@UBOT> Rule k4").args == ["k4"]
    assert C.parse_command("k7").args == ["k7"], "a bare offer ref in the digest thread"
    assert C.parse_command("rule <mailto:a@b.example|a@b.example> ignore").args[0].startswith("<mailto:")
    assert C.parse_command("ruler of the world") is None
    assert C.parse_command("hi wanda, rule this") is None
    assert C.parse_command("rule trash").args == ["trash"], "email-thread shorthand parses; handle() needs a sender"
    assert C.is_command("attest people/x#c1")
    assert not C.is_command("attest people/../../etc/passwd#c1")


def test_rule_forms(tmp_path, vault):
    store = Store(tmp_path / "w.db")
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, ix.DictTrust(), TODAY)
    owners = ["U_OWNER"]
    m = C.handle(ctx("rule Priya.Nash@Example.org trash stale list"), conn, store, owners)
    o = m.observations[0]
    assert (o.src, o.op, o.facet, o.subject) == ("owner", "rule", "mail-disposition", "person/priya.nash@example.org")
    assert o.text == "trash mail from priya.nash@example.org: stale list" and o.cause == "slack:D1:10.1"
    assert "Rule recorded" in m.reply
    m = C.handle(ctx("rule riversidelanguageacademy.org trash"), conn, store, owners)
    assert m.observations[0].subject == "org/riversidelanguageacademy.org"
    m = C.handle(ctx("rule person/robin-vale prefers text over email"), conn, store, owners)
    assert m.observations[0].facet == "preference" and m.observations[0].subject == "person/robin-vale"
    # Inside an email task thread, `rule trash` targets that email's sender.
    m = C.handle(ctx("rule trash", sender="noreply@sunnybrook.example"), conn, store, owners)
    assert m.observations[0].text == "trash mail from noreply@sunnybrook.example"
    # An offer ref mints exactly the templated text, and takes the offer.
    ref = store.add_offer("disposition", "org/sunnybrook.example", "ignore", "ignore mail from noreply@sunnybrook.example")
    m = C.handle(ctx(f"rule {ref}"), conn, store, owners)
    assert m.observations[0].text == "ignore mail from noreply@sunnybrook.example" and store.get_offer(ref)["taken_at"]


def test_refusals(tmp_path, vault):
    store = Store(tmp_path / "w.db")
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, ix.DictTrust(), TODAY)
    assert not C.handle(ctx("rule x@y.example trash"), conn, store, []).ok
    assert "Only the configured owners" in C.handle(ctx("rule x@y.example trash", user="U_KID"), conn, store, ["U_OWNER"]).reply
    # Prose that merely starts with a verb word is not a command at all.
    for text in ("rule nonsense-target trash", "forget it, thanks", "rule of thumb: keep it simple", "pin down a time", "attest to that!"):
        assert C.parse_command(text) is None, text
    assert not C.handle(ctx("forget it, thanks"), conn, store, ["U_OWNER"]).ok
    # A well-formed subject the owner names may be new: their word mints it.
    m = C.handle(ctx("rule topic/kitchen-remodel keep receipts in the topic note"), conn, store, ["U_OWNER"])
    assert m.ok and m.observations[0].subject == "topic/kitchen-remodel"
    assert not C.handle(ctx("attest nonsense"), conn, store, ["U_OWNER"]).ok
    assert not C.handle(ctx("attest people/nobody#c1"), conn, store, ["U_OWNER"]).ok


def test_attest_pin_forget_reference_claims(tmp_path, vault):
    u = "01k4qm2f7a9x3g01"
    append(vault, mk_obs("person/x@y.example", "Sends invoices.", "2026-09-01", cause="m:1", ulid=u))
    n = new_note(vault.root / "people" / "x@y.example.md", "person", "x@y.example", created=TODAY)
    n.claims.append(Claim("c1", "Sends invoices.", [Edge("derived-from", "belt/ledger/2026-09-01", u)]))
    write_atomic(n.path, n.render())
    store = Store(tmp_path / "w.db")
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, ix.DictTrust(), TODAY)
    m = C.handle(ctx("attest people/x@y.example#c1"), conn, store, ["U_OWNER"])
    assert m.observations[0].op == "attest" and m.observations[0].ref == "people/x@y.example.md#^c1"
    m = C.handle(ctx("pin people/x@y.example.md#^c1"), conn, store, ["U_OWNER"])
    assert m.observations[0].op == "pin"
    m = C.handle(ctx("forget people/x@y.example#c1"), conn, store, ["U_OWNER"])
    ops = {o.op: o for o in m.observations}
    assert set(ops) == {"retire", "veto"}
    assert "key:person/x@y.example|mail-pattern" in ops["veto"].ref, "a veto suppresses the cause, not just the claim"


def test_expected_for_message_recomputes_what_a_message_may_have_minted(tmp_path, vault):
    store = Store(tmp_path / "w.db")
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, ix.DictTrust(), TODAY)
    allowed = C.expected_for_message("rule priya@x.example trash", conn, store)
    assert allowed == [("rule", "person/priya@x.example", "mail-disposition", "trash mail from priya@x.example")]
    assert C.expected_for_message("hi wanda how are you", conn, store) == []
    assert C.expected_for_message("rule trash", conn, store, task_sender="s@x.example")[0][3] == "trash mail from s@x.example"
