"""`wanda memory` verbs, including provenance from the harness environment."""
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
    cfg, store, svc = env
    store.ingest_message(dedupe_key="k", message_id="<k>", folder="INBOX", uidvalidity=1, uid=1, from_addr="a@b.c", subject="s", date_hdr="d", snippet="b")
    tid = store.create_task(1, "C1", "9.9", kind="email")
    monkeypatch.setenv("WANDA_TASK_ID", str(tid))
    assert run(cfg, verb="open", title="Wire the dues by Friday", check_by="2026-09-10", about="topic/dues") == 0
    files = list((svc.vault.root / "open").glob("2026-*.md"))
    assert len(files) == 1 and "tier: email" in files[0].read_text()
    assert "stays off the always-loaded list" in capsys.readouterr().out


def test_forget_refuses_owner_stated_claims(env):
    cfg, store, svc = env
    u = "01k4qs81bdk3m9d1"
    append(svc.vault, mk_obs("pref/mail-dispositions", "trash mail from a@x.example", "2026-09-01", src="owner", op="rule",
                             facet="mail-disposition", cause="slack:D1:1.1", ulid=u))
    store.set_owner_check("slack:D1:1.1", True, "")
    n = new_note(svc.vault.root / "prefs" / "mail-dispositions.md", "pref", "Mail dispositions")
    n.claims.append(Claim("c1", "trash mail from a@x.example", [Edge("owner-said", "belt/ledger/2026-09-01", u)]))
    write_atomic(n.path, n.render())
    run(cfg, verb="reindex", full=False)
    with pytest.raises(SystemExit):
        run(cfg, verb="forget", ref="prefs/mail-dispositions#c1")
