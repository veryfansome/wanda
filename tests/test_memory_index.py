"""The derived index: tiers from verifiable provenance, status from edges,
scores, vetoes, and the queries the projection and recall rest on."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import DictTrust

from wanda.memory import index as ix
from wanda.memory.ledger import Observation, append
from wanda.memory.notes import Claim, Edge, new_note
from wanda.memory.vault import Vault, write_atomic

TODAY = "2026-09-03"


def obs(subject, text, day, src="triage", cause="", op="", facet="mail-pattern", ref="", ulid=None, until=""):
    o = Observation(subject=subject, facet=facet, text=text, src=src, op=op, cause=cause, ref=ref, until=until,
                    when=datetime.fromisoformat(f"{day}T10:00:00+00:00"))
    if ulid:
        o.ulid = ulid
    return o


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "memory")
    for d in ("people", "orgs", "topics", "prefs", "open", "retired"):
        (v.root / d).mkdir(parents=True)
    return v


def write_note(vault, subject_type, slug, title, claims, ids=None, extra_meta=None):
    n = new_note(vault.dir_for(subject_type) / f"{slug}.md", subject_type, title, ids=ids or [], created=TODAY)
    if extra_meta:
        n.meta.update(extra_meta)
    n.claims.extend(claims)
    write_atomic(n.path, n.render())
    return n


def test_tier_is_derived_from_what_the_harness_can_verify(vault):
    when = datetime.fromisoformat("2026-09-01T10:00:00+00:00")
    span = (when - timedelta(minutes=5), when + timedelta(minutes=5))
    # A DM and an email task both in flight.
    both = DictTrust(verified_causes={"slack:C1:100.1"}, windows=[(*span, "dm"), (*span, "email")])
    o_owner_ok = obs("person/x", "t", "2026-09-01", src="owner", op="rule", cause="slack:C1:100.1")
    o_owner_forged = obs("person/x", "t", "2026-09-01", src="owner", op="rule", cause="slack:C1:999.9")
    o_shell = obs("person/x", "t", "2026-09-01", src="agent", cause="task:7")
    o_triage = obs("person/x", "t", "2026-09-01", src="triage", cause="m:abc")
    assert ix.tier_for_obs(o_owner_ok, both) == "owner"
    assert ix.tier_for_obs(o_owner_forged, both) == "email", "unverifiable while an email task runs: least trust"
    assert ix.tier_for_obs(o_shell, both) == "email", "any shell line is email while an email task runs — the field says nothing"
    assert ix.tier_for_obs(o_triage, both) == "email"
    # With only a DM session in flight, a shell line is a conversation's: session.
    dm_only = DictTrust(windows=[(*span, "dm")])
    assert ix.tier_for_obs(o_shell, dm_only) == "session"
    assert ix.tier_for_obs(o_owner_forged, dm_only) == "session"


def test_rebuild_counts_causes_days_and_status(vault, tmp_path):
    ulids = [f"01k4qm2f7a9x3b{i:02d}" for i in range(5)]
    subj = "person/robin.vale@x.example"
    # Three triage memos on one day count as ONE cause; two more days make three.
    for i, day in enumerate(["2026-08-01", "2026-08-01", "2026-08-01", "2026-08-09", "2026-08-20"]):
        append(vault, obs(subj, "Names a deadline half the time.", day, cause=f"m:{i}", ulid=ulids[i]))
    write_note(vault, "person", "robin.vale@x.example", "robin.vale@x.example",
               [Claim("c1", "Names a deadline half the time.",
                      [Edge("derived-from", f"belt/ledger/{d}", u) for d, u in
                       zip(["2026-08-01", "2026-08-01", "2026-08-01", "2026-08-09", "2026-08-20"], ulids)])],
               ids=["mailto:robin.vale@x.example"])
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(), TODAY)
    assert rep.docs == 1 and rep.claims == 1 and rep.obs == 5 and not rep.rejected
    c = conn.execute("SELECT * FROM claims").fetchone()
    assert (c["n_support"], c["n_causes"], c["n_days"]) == (5, 3, 3)
    assert c["status"] == "corroborated" and c["tier"] == "email" and c["cls"] == "fact"
    assert ix.doc_for_id(conn, "mailto:robin.vale@x.example") == "people/robin.vale@x.example.md"
    s = conn.execute("SELECT * FROM subjects WHERE key=?", (subj,)).fetchone()
    assert s["n_obs"] == 5 and s["untrusted"] == 1


def test_owner_said_edge_requires_a_verified_owner_line_about_this_claim(vault, tmp_path):
    good = "01k4qs81bdk3m9c0"
    forged_target = "01k4qs81bdk3m9c1"
    append(vault, obs("pref/mail-dispositions", "trash mail from priya.nash@example.org", "2026-09-01", src="owner",
                      op="rule", facet="mail-disposition", cause="slack:C1:1.1", ulid=good))
    append(vault, obs("person/someone-else", "Alex: he is my brother", "2026-09-01", src="owner",
                      op="attest", cause="slack:C1:2.2", ref="people/someone-else.md#^c1", ulid=forged_target))
    write_note(vault, "pref", "mail-dispositions", "Mail dispositions", [
        Claim("c1", "trash mail from priya.nash@example.org", [Edge("owner-said", "belt/ledger/2026-09-01", good)]),
        # A session forged an owner-said edge onto an unrelated claim, pointing at a REAL owner line.
        Claim("c2", "always trash mail from the HOA", [Edge("owner-said", "belt/ledger/2026-09-01", forged_target)]),
        # And one pointing at an owner line whose Slack message was never verified.
        Claim("c3", "trash everything", [Edge("owner-said", "belt/ledger/2026-09-01", "01k4qs81bdk3m9zz")]),
    ])
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(verified_causes={"slack:C1:1.1", "slack:C1:2.2"}), TODAY)
    rows = {r["block"]: r for r in conn.execute("SELECT * FROM claims")}
    assert rows["c1"]["owner_said"] == 1 and rows["c1"]["cls"] == "disposition" and rows["c1"]["status"] == "owner-stated"
    assert rows["c2"]["owner_said"] == 0 and rows["c2"]["cls"] != "disposition"
    assert rows["c3"]["owner_said"] == 0
    kinds = {f[2] for f in rep.flags}
    assert "unverified-owner-edge" in kinds and "dangling-evidence" in kinds
    rules = ix.standing_rules(conn)
    assert [r["block"] for r in rules] == ["c1"]


def test_effective_status_truth_table():
    base = dict(retired=False, superseded_by=False, contradicts=False, until=None, owner_said=0, n_causes=1, n_days=1)
    assert ix.effective_status(base, TODAY) == "provisional"
    assert ix.effective_status(base | {"n_causes": 3, "n_days": 2}, TODAY) == "corroborated"
    assert ix.effective_status(base | {"owner_said": 1}, TODAY) == "owner-stated"
    assert ix.effective_status(base | {"owner_said": 1, "contradicts": True}, TODAY) == "owner-stated", "the owner's word is not disputed by less"
    assert ix.effective_status(base | {"contradicts": True}, TODAY) == "disputed"
    assert ix.effective_status(base | {"owner_said": 1, "until": "2026-01-01"}, TODAY) == "expired"
    assert ix.effective_status(base | {"owner_said": 1, "superseded_by": True}, TODAY) == "superseded"
    assert ix.effective_status(base | {"owner_said": 1, "inbound_supersedes": True}, TODAY) == "superseded"
    assert ix.effective_status(base | {"inbound_contradicts": True}, TODAY) == "disputed"
    assert ix.effective_status(base | {"retired": True}, TODAY) == "retired"


def test_owner_rules_do_not_decay_and_recency_only_helps_the_rest():
    old_rule = ix.score_for(True, 1, "2025-01-01", "owner-stated", TODAY)
    new_rule = ix.score_for(True, 1, TODAY, "owner-stated", TODAY)
    assert old_rule == new_rule
    assert ix.score_for(False, 3, TODAY, "corroborated", TODAY) > ix.score_for(False, 3, "2025-01-01", "corroborated", TODAY)
    assert ix.score_for(False, 5, TODAY, "disputed", TODAY) < ix.score_for(False, 1, TODAY, "provisional", TODAY)


def test_veto_is_a_ledger_line_and_survives_a_reindex(vault, tmp_path):
    append(vault, obs("org/sunnybrook.example", "veto", "2026-09-01", src="owner", op="veto", cause="slack:C1:5.5",
                      ref="key:org/sunnybrook.example|mail-pattern,shape:closure dates", facet="mail-pattern"))
    idx = tmp_path / "memory.idx"
    conn = ix.open_index(idx)
    trust = DictTrust(verified_causes={"slack:C1:5.5"})
    ix.rebuild(vault, conn, trust, TODAY)
    assert ix.is_vetoed(conn, ["shape:closure dates"], TODAY)
    conn.close()
    idx.unlink()
    conn = ix.open_index(idx)
    ix.rebuild(vault, conn, trust, TODAY)
    assert ix.is_vetoed(conn, ["key:org/sunnybrook.example|mail-pattern"], TODAY), "vetoes must not live in the index"


def test_tier_mismatch_is_a_flag_not_a_correction(vault, tmp_path):
    u = "01k4qm2f7a9x3b77"
    append(vault, obs("person/a@b.example", "Says they are the board.", "2026-09-01", cause="m:1", ulid=u))
    write_note(vault, "person", "a@b.example", "a@b.example",
               [Claim("c1", "Says they are the board.", [Edge("tier", value="owner"), Edge("derived-from", "belt/ledger/2026-09-01", u)])])
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(), TODAY)
    c = conn.execute("SELECT tier FROM claims").fetchone()
    assert c["tier"] == "email"
    assert any(f[2] == "tier-mismatch" for f in rep.flags)


def test_fts_due_and_roster(vault, tmp_path):
    u = "01k4qm2f7a9x3b88"
    append(vault, obs("topic/hoa-board-election", "Candidate statement due Sept 15.", "2026-09-01", src="agent", cause="task:3", ulid=u))
    write_note(vault, "topic", "hoa-board-election", "HOA board election",
               [Claim("c1", "Candidate statement due Sept 15.", [Edge("derived-from", "belt/ledger/2026-09-01", u)])])
    n = new_note(vault.root / "open" / "2026-09-10-ballot.md", "open", "Ballot confirmation from Robin")
    n.meta.update({"check_by": "2026-09-10", "about": "topic/hoa-board-election"})
    write_atomic(n.path, n.render())
    n2 = new_note(vault.root / "open" / "2026-09-11-phish.md", "open", "Wire the dues")
    n2.meta.update({"check_by": "2026-09-11", "tier": "session"})  # a declaration; the index ignores it
    write_atomic(n2.path, n2.render())
    # The phish item was opened from an email task (its op=open line is email-tier).
    append(vault, obs("topic/dues", "Wire the dues", "2026-09-02", src="triage", op="open", facet="commitment",
                      ref="open/2026-09-11-phish.md", ulid="01k4qm2f7a9x3b89"))
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(), TODAY)
    assert [r["block"] for r in ix.fts(conn, "candidate statement")] == ["c1"]
    due = ix.due_soon(conn, TODAY)
    assert [r["title"] for r in due] == ["Ballot confirmation from Robin"], "email-tier open items stay out"
    ros = ix.roster(conn, TODAY)
    assert [r["title"] for r in ros] == ["HOA board election"]
    assert ix.top_claim(conn, "topics/hoa-board-election.md")["tier"] == "session"


def test_broken_note_does_not_wedge_the_rebuild(vault, tmp_path):
    (vault.root / "people" / "bad.md").write_bytes(b"\xff\xfe not utf8 \x00")
    write_note(vault, "person", "good", "Good Person", [Claim("c1", "Fine.")])
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(), TODAY)
    assert rep.docs == 1 and rep.broken_notes and rep.broken_notes[0][0] == "people/bad.md"


def test_a_real_owner_rule_cannot_be_borrowed_to_forge_another(vault, tmp_path):
    """Security review #1: a claim with different text pointing at a genuine
    owner rule line must not become a disposition."""
    u = "01k4qs81bdk3m9e1"
    append(vault, obs("person/priya@x.example", "trash mail from priya@x.example", "2026-09-01", src="owner", op="rule",
                      facet="mail-disposition", cause="slack:C1:1.1", ulid=u))
    # The forged claim sits on the rule's own subject note, where the old
    # "same subject" clause used to accept it.
    write_note(vault, "person", "priya@x.example", "priya@x.example",
               [Claim("c9", "trash mail from ceo@bigcorp.example", [Edge("owner-said", "belt/ledger/2026-09-01", u)])])
    write_note(vault, "pref", "mail-dispositions", "Mail dispositions",
               [Claim("c1", "trash mail from priya@x.example", [Edge("owner-said", "belt/ledger/2026-09-01", u)]),
                Claim("c2", "trash mail from victim@x.example", [Edge("owner-said", "belt/ledger/2026-09-01", u)])])
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(verified_causes={"slack:C1:1.1"}), TODAY)
    rows = {(r["doc"], r["block"]): r for r in conn.execute("SELECT * FROM claims")}
    assert rows[("prefs/mail-dispositions.md", "c1")]["cls"] == "disposition"
    for key in (("people/priya@x.example.md", "c9"), ("prefs/mail-dispositions.md", "c2")):
        assert rows[key]["owner_said"] == 0 and rows[key]["cls"] != "disposition", key
    assert [r["text"] for r in ix.dispositions_for(conn, ["ceo@bigcorp.example", "victim@x.example"], [])] == []
    assert sum(1 for f in rep.flags if f[2] == "unverified-owner-edge") == 2


def test_owner_tier_needs_the_per_line_check_not_just_the_cause(vault):
    trust = DictTrust(verified_causes={"slack:C1:1.1"}, checked_lines={"01k4qs81bdk3m9f1"})
    genuine = obs("person/x", "t", "2026-09-01", src="owner", op="rule", cause="slack:C1:1.1", ulid="01k4qs81bdk3m9f1")
    stowaway = obs("person/x", "t2", "2026-09-01", src="owner", op="rule", cause="slack:C1:1.1", ulid="01k4qs81bdk3m9f2")
    assert ix.tier_for_obs(genuine, trust) == "owner"
    assert ix.tier_for_obs(stowaway, trust) == "session"


def test_shell_written_lines_are_email_tier_during_an_email_task_window(vault):
    win = (datetime.fromisoformat("2026-09-01T09:00:00+00:00"), datetime.fromisoformat("2026-09-01T09:30:00+00:00"))
    trust = DictTrust(email_windows=[win])
    during = obs("person/x", "t", "2026-09-01", src="harness", cause="cli:123")
    during.when = datetime.fromisoformat("2026-09-01T09:10:00+00:00")
    after = obs("person/x", "t", "2026-09-01", src="harness", cause="cli:123")
    after.when = datetime.fromisoformat("2026-09-01T11:00:00+00:00")
    assert ix.tier_for_obs(during, trust) == "email", "env -u WANDA_TASK_ID buys nothing"
    assert ix.tier_for_obs(after, trust) == "session"
    imp = obs("person/x", "t", "2026-09-01", src="import", cause="import:abc")
    imp.when = during.when
    assert ix.tier_for_obs(imp, trust) == "email"


def test_inbound_supersedes_marks_the_loser_even_when_only_the_winner_was_written(vault, tmp_path):
    write_note(vault, "person", "d", "D", [
        Claim("c1", "Old address."),
        Claim("c2", "New address.", [Edge("supersedes", "people/d", "c1")]),
        Claim("c3", "Maybe moved."), Claim("c4", "Did not move.", [Edge("contradicts", "people/d", "c3")]),
    ])
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(), TODAY)
    status = {r["block"]: r["status"] for r in conn.execute("SELECT block, status FROM claims")}
    assert status["c1"] == "superseded" and status["c2"] != "superseded"
    assert status["c3"] == "disputed" and status["c4"] == "disputed", "both stay, both ranked last"
    assert {r["block"] for r in ix.live_claims(conn, "people/d.md")} == {"c2", "c3", "c4"}, "disputed claims stay visible"


def test_support_is_counted_from_the_ledger_group_not_the_kept_edges(vault, tmp_path):
    ulids = [f"01k4qm2f7a9x3n{i:02d}" for i in range(6)]
    days = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"]
    for u, d in zip(ulids, days):
        append(vault, obs("org/news.example", "Weekly newsletter.", d, cause=f"m:{u}", ulid=u))
    write_note(vault, "org", "news.example", "news.example",
               [Claim("c1", "Weekly newsletter.", [Edge("derived-from", "belt/ledger/2026-08-06", ulids[-1])])])
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(), TODAY)
    c = conn.execute("SELECT * FROM claims").fetchone()
    assert (c["n_support"], c["n_causes"], c["n_days"], c["first_seen"], c["last_seen"]) == (6, 6, 6, "2026-08-01", "2026-08-06")


def test_redirect_stub_is_not_a_live_note_and_aliases_win(vault, tmp_path):
    write_note(vault, "person", "robin-vale", "Robin Vale", [Claim("c1", "Ballots.")], extra_meta={"aliases": ["Robin", "R. Vale"]})
    (vault.root / "people" / "d@x.example.md").write_text("---\nkind: redirect\nsuperseded_by: people/robin-vale.md\n---\n- superseded-by:: [[people/robin-vale]]\n")
    append(vault, obs("person/d@x.example", "Old key still on the belt.", "2026-09-01", cause="m:1"))
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(), TODAY)
    assert conn.execute("SELECT COUNT(*) FROM docs WHERE path='people/d@x.example.md'").fetchone()[0] == 0
    assert "person/d@x.example" not in ix.all_subjects(conn)
    assert ix.canonical_subject(conn, "person/d@x.example") == "person/robin-vale"
    from wanda.memory.subjects import resolve
    r = resolve("person/d@x.example", ix.all_subjects(conn) | {"person/d@x.example"}, ix.subject_aliases(conn))
    assert r.how == "alias" and r.key == "person/robin-vale"
    assert ix.subject_aliases(conn)["person/robin"] == "person/robin-vale", "frontmatter aliases resolve as subject keys"
    assert [r["title"] for r in ix.roster(conn, TODAY)] == ["Robin Vale"]


def test_roster_recency_counts_topics_without_ids_and_never_double_counts(vault, tmp_path):
    for i in range(3):
        append(vault, obs("topic/election", "Ballots due.", f"2026-09-0{i + 1}", src="agent", cause="task:1", ulid=f"01k4qm2f7a9x3p{i:02d}"))
    write_note(vault, "topic", "election", "Election", [Claim("c1", "Ballots due.")])
    write_note(vault, "person", "robin", "Robin", [Claim("c1", "Secretary.")], ids=["mailto:a@x.example", "slack:U1"])
    append(vault, obs("person/robin", "Seen once.", "2026-09-01", src="agent", cause="task:1", ulid="01k4qm2f7a9x3p99"))
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(task_kinds={1: "dm"}), TODAY)
    assert [r["title"] for r in ix.roster(conn, TODAY)] == ["Election", "Robin"]


def test_owner_rules_tie_break_on_when_they_were_said(vault, tmp_path):
    for i, day in enumerate(["2026-08-20", "2026-08-10", "2026-08-30"]):
        u = f"01k4qs81bdk3m9g{i}"
        append(vault, obs("pref/mail-dispositions", f"trash mail from s{i}@x.example", day, src="owner", op="rule",
                          facet="mail-disposition", cause=f"slack:C1:{i}.1", ulid=u))
    write_note(vault, "pref", "mail-dispositions", "Mail dispositions", [
        Claim(f"c{i}", f"trash mail from s{i}@x.example", [Edge("owner-said", f"belt/ledger/{d}", f"01k4qs81bdk3m9g{i}")])
        for i, d in enumerate(["2026-08-20", "2026-08-10", "2026-08-30"])])
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(verified_causes={f"slack:C1:{i}.1" for i in range(3)}), TODAY)
    assert [r["block"] for r in ix.standing_rules(conn)] == ["c1", "c0", "c2"]


def test_a_duplicate_block_id_costs_one_line_not_the_whole_rebuild(vault, tmp_path):
    """A copy-pasted claim line reuses its ^block id, which UNIQUE(doc, block)
    refuses. One line and one flag, never the whole index."""
    import wanda.memory.passes as P
    write_note(vault, "person", "a", "A", [Claim("c1", "First."), Claim("c1", "Second."), Claim("c2", "Third.")])
    write_note(vault, "person", "b", "B", [Claim("c1", "Fine.")])
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(), TODAY)
    assert (rep.docs, rep.claims, rep.broken_notes) == (2, 3, [])
    assert {(r["doc"], r["block"], r["text"]) for r in conn.execute("SELECT doc, block, text FROM claims")} == {
        ("people/a.md", "c1", "First."), ("people/a.md", "c2", "Third."), ("people/b.md", "c1", "Fine.")}
    dup = [f for f in rep.flags if f[2] == "duplicate-block"]
    assert len(dup) == 1 and dup[0][0] == "people/a.md" and dup[0][1] == "c1"
    assert any("duplicate block on people/a.md" in i for i in P.fsck(vault, conn))


def test_one_unindexable_note_does_not_take_the_index_with_it(vault, tmp_path, monkeypatch):
    write_note(vault, "person", "a", "A", [Claim("c1", "Fine.")])
    write_note(vault, "person", "b", "B", [Claim("c1", "Boom.")])
    write_note(vault, "person", "c", "C", [Claim("c1", "Fine too.")])
    real = ix._index_claim

    def maybe_boom(conn, trust, rep, c, doc, *a, **kw):
        if doc == "people/b.md":
            raise RuntimeError("synthetic indexing failure")
        return real(conn, trust, rep, c, doc, *a, **kw)

    monkeypatch.setattr(ix, "_index_claim", maybe_boom)
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(), TODAY)
    assert (rep.docs, rep.claims) == (2, 2)
    assert [p for p, _ in rep.broken_notes] == ["people/b.md"]
    assert "indexing failed: " in rep.broken_notes[0][1]
    assert conn.execute("SELECT COUNT(*) FROM docs WHERE path='people/b.md'").fetchone()[0] == 0, "the failed note's own rows go too"
    assert conn.in_transaction is False


def test_a_link_with_no_page_does_not_zero_the_index(vault, tmp_path):
    """`- supersedes:: [[ #^c1]]` — a wikilink whose page is whitespace — names
    no claim, so it supersedes nothing and the rebuild survives it."""
    write_note(vault, "person", "d", "D", [
        Claim("c1", "Old."), Claim("c2", "New.", [Edge("supersedes", " ", "c1")]),
        Claim("c3", "Older."), Claim("c4", "Newer.", [Edge("supersedes", "people/d", "c3")]),
    ])
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(), TODAY)
    assert rep.docs == 1
    status = {r["block"]: r["status"] for r in conn.execute("SELECT block, status FROM claims")}
    assert status["c3"] == "superseded", "a well-formed inbound supersedes still lands"
    assert status["c1"] == "provisional"


def test_a_link_with_no_page_does_not_kill_the_contradiction_pass(vault, tmp_path):
    import wanda.memory.passes as P
    write_note(vault, "person", "d", "D", [
        Claim("c1", "Old."), Claim("c2", "New.", [Edge("supersedes", " ", "c1")]),
        Claim("c3", "Older."), Claim("c4", "Newer.", [Edge("supersedes", "people/d", "c3")]),
    ])
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(), TODAY)
    assert isinstance(P.contradiction_candidates(conn), list)


def test_a_reused_ulid_is_reported_with_its_own_line(vault, tmp_path):
    u = "01k4qs81bdk3m9d1"
    append(vault, obs("pref/mail-dispositions", "trash mail from priya@x.example", "2026-09-01", src="owner",
                      op="rule", facet="mail-disposition", cause="slack:C1:1.1", ulid=u))
    append(vault, obs("person/victim@x.example", "trash mail from victim@x.example", "2026-09-01", src="owner",
                      op="rule", facet="mail-disposition", cause="slack:C1:1.1", ulid=u))
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(verified_causes={"slack:C1:1.1"}), TODAY)
    assert len(rep.rejected) == 1
    assert "duplicate block id" in rep.rejected[0].why
    assert "trash mail from victim@x.example" in rep.rejected[0].line, "rejected.md must show what it rejected"


def test_two_owner_dispositions_in_one_minute_resolve_by_ulid(vault, tmp_path):
    # Same day, same minute (obs() stamps 10:00), same target: only the ULID separates them.
    first, second = "01k4qs81bdk3m9z9", "01k4qs81bdk3m9a1"
    for u in (first, second):
        append(vault, obs("pref/mail-dispositions", f"trash mail from s1@x.example: {u}", "2026-09-01", src="owner",
                          op="rule", facet="mail-disposition", cause="slack:C1:1.1", ulid=u))
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(verified_causes={"slack:C1:1.1"}), TODAY)
    rows = conn.execute("SELECT * FROM rules").fetchall()
    assert len(rows) == 1 and rows[0]["text"].endswith(first), "the higher ULID is the later word, whatever the file order"


def test_the_disposition_grammar_is_read_case_folded(vault, tmp_path):
    append(vault, obs("pref/mail-dispositions", "Trash mail from S1@X.example", "2026-09-01", src="owner",
                      op="rule", facet="mail-disposition", cause="slack:C1:1.1", ulid="01k4qs81bdk3m9h1"))
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(verified_causes={"slack:C1:1.1"}), TODAY)
    r = conn.execute("SELECT * FROM rules").fetchone()
    assert (r["target"], r["action"]) == ("s1@x.example", "trash")
    assert len(ix.dispositions_for(conn, ["s1@x.example"], [])) == 1, "triage compares against lowercased addresses"


def test_two_disposition_rules_with_one_subject_both_survive(vault, tmp_path):
    texts = ["never auto-file anything from the school", "always keep the HOA thread"]
    for i, (day, text) in enumerate(zip(["2026-09-01", "2026-09-02"], texts)):
        append(vault, obs("pref/mail-dispositions", text, day, src="owner", op="rule",
                          facet="mail-disposition", cause=f"slack:C1:{i}.1", ulid=f"01k4qs81bdk3m9j{i}"))
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(verified_causes={"slack:C1:0.1", "slack:C1:1.1"}), TODAY)
    assert {r["text"] for r in ix.standing_rules(conn)} == set(texts), "two dispositions the grammar cannot read are not one rule"


def test_a_freshly_built_index_has_no_write_only_columns(tmp_path):
    conn = ix.open_index(tmp_path / "memory.idx")
    cols = {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in ("claims", "subjects")}
    assert "facet" not in cols["claims"]
    assert "has_file" not in cols["subjects"]


def test_two_files_that_slugify_to_one_subject_are_flagged(vault, tmp_path):
    import wanda.memory.passes as P
    for name, title in (("Robin Vale", "Robin Vale"), ("robin-vale", "Robin S")):
        n = new_note(vault.root / "people" / f"{name}.md", "person", title, created=TODAY)
        n.claims.append(Claim("c1", f"From {name}."))
        write_atomic(n.path, n.render())
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(), TODAY)
    dup = [f for f in rep.flags if f[2] == "duplicate-subject"]
    assert len(dup) == 1
    assert dup[0][3].startswith("person/robin-vale also on ")
    assert {dup[0][0], dup[0][3].split(" also on ")[1]} == {"people/robin-vale.md", "people/Robin Vale.md"}
    assert any("duplicate subject on people/robin-vale.md" in i for i in P.fsck(vault, conn))


def test_export_false_is_honoured_however_it_is_typed(vault, tmp_path):
    spellings = ["false", '"false"', "off", "0", "no", None, "true"]
    for i, val in enumerate(spellings):
        meta = f"export: {val}\n" if val is not None else ""
        (vault.root / "people" / f"n{i}.md").write_text(f"---\ntype: person\ntitle: N{i}\n{meta}---\n\n# N{i}\n")
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(), TODAY)
    got = [r["export"] for r in conn.execute("SELECT path, export FROM docs ORDER BY path")]
    assert got == [0, 0, 0, 0, 0, 1, 1], "only an absent key or a real yes exports"


def test_a_later_veto_line_can_neither_lift_nor_outlive_a_suppression(vault, tmp_path):
    k, ell = "key:org/k.example|mail-pattern", "key:org/l.example|mail-pattern"
    lines = [("2026-09-01", k, "", "01k4qs81bdk3m9v0"), ("2026-09-02", k, "2026-09-10", "01k4qs81bdk3m9v1"),
             ("2026-09-02", ell, "2099-12-31", "01k4qs81bdk3m9v2")]
    for i, (day, ref, until, u) in enumerate(lines):
        append(vault, obs("org/k.example", "veto", day, src="owner", op="veto", cause=f"slack:C1:{i}.1",
                          ref=ref, until=until, ulid=u))
    conn = ix.open_index(tmp_path / "memory.idx")
    ix.rebuild(vault, conn, DictTrust(verified_causes={f"slack:C1:{i}.1" for i in range(3)}), TODAY)
    rows = {r["key"]: r["until"] for r in conn.execute("SELECT key, until FROM vetoes")}
    assert rows[k] == "2027-09-01", "a later line cannot shorten a standing suppression"
    assert ix.is_vetoed(conn, [k], "2026-12-01")
    assert rows[ell] == "2027-09-02", "and no line reaches past a year from its own day"


def test_a_tombstone_that_vanishes_mid_rebuild_costs_one_note(vault, tmp_path, monkeypatch):
    """The tombstone is unlinked between the glob and the row that describes
    it: every stat of it after it is read must sit inside the loop's try."""
    import pathlib
    write_note(vault, "person", "live", "Live", [Claim("c1", "Fine.")])
    (vault.retired_dir / "people").mkdir(parents=True, exist_ok=True)
    (vault.retired_dir / "people" / "gone.md").write_text(
        "---\nkind: tombstone\nsubject: person/gone\nsuperseded_by: person/live\n---\n\n# Gone\n")
    real_parse, real_stat = ix.parse_note, pathlib.Path.stat
    gone = []

    def parse(path, *a, **kw):
        doc = real_parse(path, *a, **kw)
        if path.name == "gone.md":
            gone.append(path)     # read succeeded; from here the file is not there any more
        return doc

    def stat(self, *a, **kw):
        if gone and self.name == "gone.md":
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(ix, "parse_note", parse)
    monkeypatch.setattr(pathlib.Path, "stat", stat)
    conn = ix.open_index(tmp_path / "memory.idx")
    rep = ix.rebuild(vault, conn, DictTrust(), TODAY)
    assert rep.docs == 1
    assert [p for p, _ in rep.broken_notes] == ["retired/people/gone.md"]
