"""`wanda memory` verbs, including provenance from the harness environment."""
import sqlite3
from argparse import Namespace

import pytest

from tests.conftest import mk_obs
from wanda import memory_cli
from wanda.config import Config
from wanda.memory import index as ix
from wanda.memory import passes as P
from wanda.memory.ledger import Observation, append, iter_observations
from wanda.memory.notes import Claim, Edge, new_note
from wanda.memory.vault import Vault, write_atomic
from wanda.store import Store

TODAY = "2026-09-03"


@pytest.fixture
def env(tmp_path):
    cfg = Config(_env_file=None, data_dir=tmp_path / "data", memory_dir=tmp_path / "data" / "memory",
                 email_triage_slack_channel_id="C1")
    store = Store(cfg.db_path)
    svc = P.Services(cfg, store, Vault(cfg.memory_vault), today=lambda: TODAY)
    P.ensure_vault(svc)
    return cfg, store, svc


def run(cfg, **kw):
    return memory_cli.run(cfg, Namespace(**kw))


def test_note_resolves_subjects_and_stamps_provenance(env, monkeypatch, capsys):
    cfg, store, svc = env
    n = new_note(svc.vault.root / "topics" / "hoa-board-election.md", "topic", "HOA board election")
    write_atomic(n.path, n.render())
    conn = P.open_conn(svc)
    ix.rebuild(svc.vault, conn, P.StoreTrust(store), TODAY)
    conn.close()
    monkeypatch.setenv("WANDA_TASK_ID", "7")
    assert run(cfg, verb="note", text="Statements are due Sept 15.", about="topic/hoa-election", facet="", until="") == 0
    assert "filed under existing subject topic/hoa-board-election" in capsys.readouterr().out
    assert run(cfg, verb="note", text="Kitchen remodel starts in October.", about="topics/kitchen-remodel", facet="Project State", until="") == 0
    assert "new subject topic/kitchen-remodel" in capsys.readouterr().out
    assert run(cfg, verb="note", text="x", about="junk", facet="", until="") == 2
    obs = [o for o in iter_observations(svc.vault) if isinstance(o, Observation)]
    assert [(o.subject, o.src, o.cause) for o in obs] == [
        ("topic/hoa-board-election", "agent", "task:7"), ("topic/kitchen-remodel", "agent", "task:7")]
    assert obs[1].facet == "project-state"


def test_read_verbs(env, capsys):
    cfg, store, svc = env
    u = "01k4qm2f7a9x3k01"
    append(svc.vault, mk_obs("person/robin@x.example", "Runs ballots.", "2026-09-01", src="agent", cause="task:1", ulid=u))
    store.create_task(None, "D1", "1.1", kind="dm")
    store.record_run(kind="agent", task_id=1, session_id="s", started_at="2026-09-01T09:00:00+00:00", exit_code=0, cost_usd=0, status="ok")
    n = new_note(svc.vault.root / "people" / "robin@x.example.md", "person", "Robin Vale", ids=["mailto:robin@x.example", "slack:U_DEV"])
    n.claims.append(Claim("c1", "Runs ballots.", [Edge("derived-from", "belt/ledger/2026-09-01", u)]))
    write_atomic(n.path, n.render())
    assert run(cfg, verb="reindex", full=False) == 0
    assert run(cfg, verb="who", ident="robin@x.example") == 0
    out = capsys.readouterr().out
    assert "Runs ballots." in out and "[people/CLAUDE.md]" in out
    assert run(cfg, verb="who", ident="U_DEV") == 0 and "Runs ballots." in capsys.readouterr().out
    assert run(cfg, verb="search", text="ballots", limit=5) == 0 and "people/robin@x.example.md#^c1" in capsys.readouterr().out
    assert run(cfg, verb="recall", text="who runs the ballots", budget=3000) == 0 and "<memory>" in capsys.readouterr().out
    assert run(cfg, verb="show", path="people/robin@x.example.md") == 0 and "^c1" in capsys.readouterr().out
    assert run(cfg, verb="status") == 0 and "docs" in capsys.readouterr().out
    assert run(cfg, verb="fsck") == 0


def test_open_from_an_email_task_is_email_tier(env, monkeypatch, capsys):
    """Tier comes from the run window this process belongs to (by process
    group), never from the environment or a default."""
    import os
    cfg, store, svc = env
    store.ingest_message(dedupe_key="k", message_id="<k>", folder="INBOX", uidvalidity=1, uid=1, from_addr="a@b.c", subject="s", date_hdr="d", snippet="b")
    tid = store.create_task(1, "C1", "9.9", kind="email")
    store.open_run_window("s-email", tid, "email")  # an email task is running now
    monkeypatch.delenv("WANDA_TASK_ID", raising=False)  # unsetting it buys nothing
    assert run(cfg, verb="open", title="Wire the dues by Friday", check_by="2026-09-10", about="topic/dues") == 0
    assert "stays off the always-loaded list" in capsys.readouterr().out
    store.close_run_window("s-email")  # the session ends; the window is recorded and still covers the line
    run(cfg, verb="reindex")
    conn = ix.open_readonly(cfg.memory_index_path)
    assert conn.execute("SELECT tier FROM docs WHERE type='open'").fetchone()["tier"] == "email"
    assert ix.due_soon(conn, "2026-09-03") == []


def test_forget_refuses_owner_stated_claims(env):
    cfg, store, svc = env
    u = "01k4qs81bdk3m9d1"
    append(svc.vault, mk_obs("pref/mail-dispositions", "trash mail from a@x.example", "2026-09-01", src="owner", op="rule",
                             facet="mail-disposition", cause="slack:D1:1.1", ulid=u))
    store.set_owner_check("slack:D1:1.1", True, "")
    store.memory_set(f"checked:{u}", "2026-09-03T00:00:00+00:00")
    n = new_note(svc.vault.root / "prefs" / "mail-dispositions.md", "pref", "Mail dispositions")
    n.claims.append(Claim("c1", "trash mail from a@x.example", [Edge("owner-said", "belt/ledger/2026-09-01", u)]))
    write_atomic(n.path, n.render())
    run(cfg, verb="reindex", full=False)
    with pytest.raises(SystemExit):
        run(cfg, verb="forget", ref="prefs/mail-dispositions#c1")


def test_note_is_retrievable_immediately(env, capsys):
    """Zero-lag: a fact noted in turn 1 is visible to `who`/`search` in the
    same session, before any hourly pass."""
    cfg, store, svc = env
    run(cfg, verb="reindex")
    assert run(cfg, verb="note", text="Prefers texts over email.", about="person/robin@x.example", facet="preference", until="") == 0
    capsys.readouterr()
    assert run(cfg, verb="who", ident="robin@x.example") == 0
    assert "Prefers texts over email." in capsys.readouterr().out


def test_dates_are_validated_before_anything_is_written(env):
    cfg, store, svc = env
    with pytest.raises(SystemExit):
        run(cfg, verb="open", title="Pay dues", check_by="tomorrow", about="topic/dues")
    with pytest.raises(SystemExit):
        run(cfg, verb="note", text="x", about="topic/dues", facet="", until="next week")
    assert not list((svc.vault.root / "open").glob("tomorrow*"))
    assert not list(svc.vault.ledger_dir.glob("*.md"))


def test_paths_are_confined_to_the_vault(env, tmp_path):
    cfg, store, svc = env
    secret = tmp_path / "secret.env"
    secret.write_text("TOKEN=abc")
    for verb, kw in (("show", {"path": "../../secret.env"}), ("walk", {"path": ["../../secret.env"]}),
                     ("retire", {"path": "../../secret.env", "to": None})):
        with pytest.raises(SystemExit):
            run(cfg, verb=verb, **kw)
    assert secret.read_text() == "TOKEN=abc"


def test_merge_is_not_a_session_verb(env, monkeypatch):
    cfg, store, svc = env
    n = new_note(svc.vault.root / "people" / "a@x.example.md", "person", "a@x.example")
    write_atomic(n.path, n.render())
    monkeypatch.setenv("WANDA_TASK_ID", "3")
    with pytest.raises(SystemExit):
        run(cfg, verb="retire", path="people/a@x.example.md", to="people/alice.md")
    monkeypatch.delenv("WANDA_TASK_ID")
    assert run(cfg, verb="retire", path="people/a@x.example.md", to="people/alice.md") == 0


def test_pin_verb(env, monkeypatch, capsys):
    cfg, store, svc = env
    n = new_note(svc.vault.root / "people" / "d.md", "person", "D")
    n.claims.append(Claim("c1", "Secretary."))
    write_atomic(n.path, n.render())
    run(cfg, verb="reindex")
    assert run(cfg, verb="pin", ref="people/d#c1") == 0
    obs = [o for o in iter_observations(svc.vault) if isinstance(o, Observation)]
    assert obs[-1].op == "pin" and obs[-1].ref == "people/d.md#^c1"


# --- wave 6: what a session may not do, and what the operator is told ------------------------------------

def test_a_stale_run_window_is_visible_in_status(env, capsys):
    """The one surface that shows what _in_session refuses four verbs on."""
    cfg, store, svc = env
    store.open_run_window("s-crashed", 7, "email")
    assert run(cfg, verb="status") == 0
    assert "open windows: 1" in capsys.readouterr().out


def test_an_unreadable_store_counts_as_a_session(env, monkeypatch, capsys):
    """A guard whose job is to refuse must not fail open."""
    cfg, store, svc = env
    monkeypatch.setattr(Store, "open_windows", lambda self: (_ for _ in ()).throw(sqlite3.DatabaseError("malformed")))
    with pytest.raises(SystemExit):
        run(cfg, verb="reindex")
    assert "cannot read the run windows" in capsys.readouterr().err


def test_fsck_does_not_build_the_shared_index(env, capsys):
    """A read verb that rebuilds does it with no owner authority, which
    empties the derived rules table. 1 already means "found N issues"."""
    cfg, store, svc = env
    assert run(cfg, verb="fsck") == 2
    assert not cfg.memory_index_path.exists()
    assert "reindex" in capsys.readouterr().err


def test_unretire_is_not_a_session_verb_and_is_reported(env, monkeypatch, capsys):
    cfg, store, svc = env
    n = new_note(svc.vault.root / "people" / "a@x.example.md", "person", "a@x.example")
    write_atomic(n.path, n.render())
    assert run(cfg, verb="retire", path="people/a@x.example.md", to=None) == 0
    monkeypatch.setenv("WANDA_TASK_ID", "3")
    with pytest.raises(SystemExit):
        run(cfg, verb="unretire", path="people/a@x.example.md")
    monkeypatch.delenv("WANDA_TASK_ID")
    assert run(cfg, verb="unretire", path="people/a@x.example.md") == 0
    assert [r["text"] for r in store.digest_pending() if "unretire`" in r["text"]] == [
        "restored retired/people/a@x.example.md with `wanda memory unretire`"]


def test_hourly_is_not_a_session_verb_and_does_not_claim_the_daemons_slot(env, capsys):
    cfg, store, svc = env
    store.open_run_window("s-crashed", 7, "email")
    with pytest.raises(SystemExit):
        run(cfg, verb="hourly")
    err = capsys.readouterr().err
    assert "s-crashed" in err and "restart the daemon" in err
    store.close_run_window("s-crashed")
    assert run(cfg, verb="hourly") == 0
    assert store.memory_get("hourly_at") is None, "a hand-run pass must not postpone the daemon's own"
    assert "holds no owner authority" in capsys.readouterr().err


def test_retire_into_a_freshly_edited_successor_says_the_merge_is_journaled(env):
    """Both notes are written now and neither has a filesha: row, so the
    successor is genuinely "being edited" and _write_note defers."""
    cfg, store, svc = env
    for slug, title in (("a@x.example", "a@x.example"), ("alice", "Alice")):
        n = new_note(svc.vault.root / "people" / f"{slug}.md", "person", title)
        write_atomic(n.path, n.render())
    with pytest.raises(SystemExit) as e:
        run(cfg, verb="retire", path="people/a@x.example.md", to="people/alice.md")
    assert "journaled" in str(e.value) and "ten minutes" in str(e.value)
    assert "people/a@x.example.md" in cfg.retire_journal_path.read_text()


def test_a_busy_ledger_leaves_no_half_written_commitment(env, monkeypatch):
    cfg, store, svc = env

    def busy(*a, **kw):
        raise TimeoutError("memory ledger is busy")

    monkeypatch.setattr(memory_cli, "ledger_append", busy)
    with pytest.raises(SystemExit) as e:
        run(cfg, verb="open", title="Wire the dues", check_by="2026-09-10", about="topic/dues")
    assert "not recorded" in str(e.value)
    assert list((svc.vault.root / "open").glob("2026-*.md")) == [], "an untracked commitment is worse than none"
    with pytest.raises(SystemExit) as e2:
        run(cfg, verb="note", text="Dues are due.", about="topic/dues", facet="", until="")
    assert "not recorded" in str(e2.value)


def test_pin_without_an_index_advises_reindex(env, capsys):
    cfg, store, svc = env
    assert run(cfg, verb="pin", ref="people/d#c1") == 1
    assert "reindex" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        run(cfg, verb="pin", ref="not a ref")
    assert "expected a claim reference" in str(e.value)


def test_reindex_and_hourly_close_the_index_connection(env, monkeypatch):
    """passes.open_conn is a read-write WAL connection; closing it is what
    checkpoints the WAL."""
    cfg, store, svc = env
    conns = []
    real = P.open_conn
    monkeypatch.setattr(P, "open_conn", lambda s: conns.append(real(s)) or conns[-1])
    assert run(cfg, verb="reindex") == 0
    assert run(cfg, verb="hourly") == 0
    assert len(conns) == 2
    for c in conns:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")


def test_open_reports_the_subject_it_filed_under(env, capsys):
    cfg, store, svc = env
    n = new_note(svc.vault.root / "topics" / "hoa-board-election.md", "topic", "HOA board election")
    write_atomic(n.path, n.render())
    assert run(cfg, verb="reindex") == 0
    capsys.readouterr()
    assert run(cfg, verb="open", title="Ballots go out", check_by="2026-09-10", about="topic/hoa-election") == 0
    out = capsys.readouterr().out
    assert "filed under existing subject topic/hoa-board-election" in out
    assert "stays off the always-loaded list" in out
    assert run(cfg, verb="open", title="Pay the dues", check_by="2026-09-11", about="topic/dues") == 0
    assert "new subject topic/dues" in capsys.readouterr().out
    assert [r["text"] for r in store.digest_pending() if r["kind"] == "mint"] == [
        "new subject topic/dues (from `wanda memory open`)"]


def test_note_says_when_there_was_no_index_to_match_against(env, capsys):
    cfg, store, svc = env
    assert run(cfg, verb="note", text="Dues are due.", about="topic/dues", facet="", until="") == 0
    assert "no index yet" in capsys.readouterr().err
