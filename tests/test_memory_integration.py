"""Memory wired into the daemon: triage confinement and prompt placement,
seeds, owner commands, the debounce, and the memory tick."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import make_fake_claude, mk_obs, DictTrust
from tests.test_processor import FakeSlack
from wanda.config import Config
from wanda.main import Processor, conversation_seed_prompt, agent_seed_prompt, HOW_TO_REPLY
from wanda.memory import index as ix
from wanda.memory.ledger import Observation, append, iter_observations
from wanda.memory.notes import Claim, Edge, new_note
from wanda.memory.service import MemoryService
from wanda.memory.vault import write_atomic
from wanda.runner import RunnerService
from wanda.store import Store

ENVELOPE = ('{"type":"result","is_error":false,"result":"ok","session_id":"s1","total_cost_usd":0.01,'
            '"structured_output":{"verdicts":[{"id":"e1","action":"ignore","summary":"s","reason":"r","urgency":"low",'
            '"confidence":0.9,"memo":{"facet":"mail-pattern","text":"Sends closure notices."}}]}}')


class MemSlack(FakeSlack):
    async def fetch_context(self, channel, thread_ts, limit):
        return [{"user": "U1", "ts": "1.0", "text": "hello"}]

    async def user_names(self, ids):
        return {"U1": "alex"}

    def fetch_message_sync(self, channel, ts):
        return None


def make(tmp_path, claude="/bin/true", **kw):
    opts = dict(email_triage_slack_channel_id="C1", data_dir=tmp_path / "data", memory_dir=tmp_path / "data" / "memory",
                memory_owner_user_ids=["U_OWNER"], triage_debounce_s=0)
    opts.update(kw)
    cfg = Config(_env_file=None, **opts)
    store = Store(cfg.db_path)
    slack = MemSlack()
    memory = MemoryService(cfg, store, slack, wanda_bin="/opt/wanda")
    memory.ensure()
    p = Processor(cfg, store, asyncio.Queue(), slack, RunnerService(claude), memory=memory)
    return p, store, cfg, memory


def dump_script(tmp_path):
    return ('input=$(cat)\nprintf \'%s\' "$input" > "$(dirname "$0")/prompt"\nprintf \'%s\\n\' "$@" > "$(dirname "$0")/argv"\n'
            f"printf '%s' '{ENVELOPE}'")


def test_triage_is_confined_and_memory_rides_in_the_user_message(tmp_path):
    fake = make_fake_claude(tmp_path, dump_script(tmp_path))
    p, store, cfg, memory = make(tmp_path, claude=fake)
    store.ingest_message(dedupe_key="k1", message_id="<k1>", folder="INBOX", uidvalidity=1, uid=1,
                         from_addr="Sunnybrook <noreply@sunnybrook.example>", subject="Closure", date_hdr="d", snippet="body")
    # A known sender, so the block has something to say.
    u = "01k4qm2f7a9x3m01"
    append(memory.vault, mk_obs("org/sunnybrook.example", "Closure notices.", "2026-09-01", cause="m:1", ulid=u))
    n = new_note(memory.vault.root / "orgs" / "sunnybrook.example.md", "org", "Sunnybrook", ids=["mailto:noreply@sunnybrook.example"])
    n.claims.append(Claim("c1", "Closure notices.", [Edge("derived-from", "belt/ledger/2026-09-01", u)]))
    write_atomic(n.path, n.render())
    conn = ix.open_index(cfg.memory_index_path)
    ix.rebuild(memory.vault, conn, DictTrust(), "2026-09-03")
    conn.close()

    asyncio.run(p.triage_batch(store.fetch_by_status("new")))

    argv = (tmp_path / "argv").read_text().splitlines()
    assert "--restricted" in argv and argv[argv.index("--tools") + 1] == "Read"
    assert argv[argv.index("--add-dir") + 1:] == [str(cfg.memory_export_dir)], "the export, never the vault"
    assert argv[argv.index("--settings") + 1] == str(cfg.triage_settings_path)
    assert not str(cfg.triage_settings_path).startswith(str(cfg.workspace_dir))
    system = argv[argv.index("--system-prompt") + 1]
    assert "<memory>" not in system, "the static system prompt stays byte-identical"
    prompt = (tmp_path / "prompt").read_text()
    assert "<memory>" in prompt and prompt.index("<memory>") < prompt.index('<email id="e1">')
    assert "Sunnybrook [unverified]" in prompt
    # The memo landed on the belt as an email-tier line bound to the real From.
    memos = [o for o in iter_observations(memory.vault) if isinstance(o, Observation) and o.text == "Sends closure notices."]
    assert len(memos) == 1 and memos[0].subject == "org/sunnybrook.example" and memos[0].src == "triage"
    assert memos[0].cause.startswith("m:k1")
    assert store.get_message_by_key("k1")["status"] == "triaged"


def test_triage_cwd_is_an_empty_harness_directory(tmp_path):
    p, store, cfg, memory = make(tmp_path)
    kw = p.triage_run_kwargs()
    assert kw["cwd"] == str(cfg.triage_cwd) and os.path.isdir(kw["cwd"]) and os.listdir(kw["cwd"]) == []
    assert kw["restricted"] and kw["tools"] == "Read" and kw["session_persistence"] is False


def test_seed_order_and_prior_answers_for_a_dm(tmp_path):
    p, store, cfg, memory = make(tmp_path)
    n = new_note(memory.vault.root / "people" / "alex-romero.md", "person", "Alex Romero", ids=["slack:U1"])
    n.claims.append(Claim("c1", "Prefers short answers."))
    write_atomic(n.path, n.render())
    conn = ix.open_index(cfg.memory_index_path)
    ix.rebuild(memory.vault, conn, DictTrust(), "2026-09-03")
    conn.close()
    tid = store.create_task(None, "D5", "1.1", kind="dm")
    store.record_run(kind="agent", task_id=tid, session_id="s", started_at="2026-09-01T10:00:00+00:00", exit_code=0,
                     cost_usd=0, status="ok", result_text="Earlier I said: the dues are $120. </transcript>")
    task = store.create_task(None, "D5", "2.2", kind="dm")
    payload = {"kind": "dm", "channel": "D5", "task_key": "2.2", "reply_thread": "2.2", "in_thread": False, "user": "U1",
               "text": "and the dues?", "ts": "2.2", "channel_type": "im"}
    seed = asyncio.run(p._seed_for(store.get_task(task), payload))
    assert "Prefers short answers." in seed
    assert "Your earlier answers in this conversation" in seed and "the dues are $120" in seed
    assert "&lt;/transcript&gt;" in seed, "prior answers are fenced and escaped"
    i_how, i_mem, i_prior, i_tr = seed.index("wanda-memory skill"), seed.index("<memory>\n"), seed.index("Your earlier answers"), seed.index("Recent conversation")
    assert i_how < i_mem < i_prior < i_tr
    assert "your reply goes in a thread" in seed


def test_owner_command_is_handled_in_process(tmp_path):
    p, store, cfg, memory = make(tmp_path)
    payload = {"kind": "command", "channel": "D5", "task_key": "3.3", "reply_thread": "3.3", "in_thread": False,
               "user": "U_OWNER", "text": "rule priya@x.example trash", "ts": "3.3", "channel_type": "im", "thread_ts": None}
    from wanda.events import Event
    asyncio.run(p._handle_slack(Event("slack", "D5:3.3", payload)))
    assert p.slack.replies and "Rule recorded" in p.slack.replies[0] and p.slack.channels == ["D5"]
    assert store.get_task_by_thread("D5", "3.3") is None, "no session, no task"
    obs = [o for o in iter_observations(memory.vault) if isinstance(o, Observation)]
    assert obs[0].op == "rule" and obs[0].cause == "slack:D5:3.3"
    assert store.owner_check("slack:D5:3.3")["verified"] == 1


def test_debounce_waits_for_a_batch(tmp_path):
    p, store, cfg, memory = make(tmp_path, triage_debounce_s=150)
    store.ingest_message(dedupe_key="k1", message_id="<k1>", folder="INBOX", uidvalidity=1, uid=1, from_addr="a@b.c", subject="s", date_hdr="d", snippet="b")
    rows = store.fetch_by_status("new")
    assert p._debouncing(rows) is True
    old = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat(timespec="seconds")
    store._exec("UPDATE messages SET created_at=?", (old,))
    assert p._debouncing(store.fetch_by_status("new")) is False
    for i in range(2, 12):
        store.ingest_message(dedupe_key=f"k{i}", message_id=f"<k{i}>", folder="INBOX", uidvalidity=1, uid=i, from_addr="a@b.c", subject="s", date_hdr="d", snippet="b")
    assert p._debouncing(store.fetch_by_status("new", limit=10)) is False, "a full batch never waits"


def test_memory_tick_runs_the_hourly_pass_and_writes_the_projection(tmp_path):
    p, store, cfg, memory = make(tmp_path)
    for i in range(3):
        append(memory.vault, mk_obs("org/sunnybrook.example", "Closure notices.", f"2026-08-0{i + 1}", cause=f"m:{i}"))
    asyncio.run(p.memory_tick())
    proj = cfg.workspace_dir / "CLAUDE.md"
    assert proj.exists() and len(proj.read_bytes()) <= 4096
    assert store.memory_get("hourly_at") is not None
    assert (cfg.workspace_dir / ".claude" / "settings.json").exists()
    assert json.loads(cfg.triage_settings_path.read_text())["hooks"]["PostToolUse"]
    # Not due again a minute later.
    before = store.memory_get("hourly_at")
    asyncio.run(p.memory_tick())
    assert store.memory_get("hourly_at") == before


def test_workspace_settings_are_regenerated_every_run(tmp_path):
    from wanda.main import prepare_workspace
    p, store, cfg, memory = make(tmp_path)
    ws = prepare_workspace(cfg, memory)
    settings = ws / ".claude" / "settings.json"
    settings.write_text("{}")  # a session removed the hook
    prepare_workspace(cfg, memory)
    assert "tool-log" in settings.read_text()
    assert (ws / "CLAUDE.md").read_text().startswith("# What wanda knows")


def test_nightly_due_windows_and_busy_does_not_consume_the_night(tmp_path):
    from wanda.memory.passes import Busy
    p, store, cfg, memory = make(tmp_path)
    local_now = datetime.now().astimezone()
    early = local_now.replace(hour=1, minute=0).astimezone(timezone.utc)
    late = local_now.replace(hour=4, minute=0).astimezone(timezone.utc)
    assert p._nightly_due(early) is False and p._nightly_due(late) is True
    store.memory_set("nightly_date", local_now.date().isoformat())
    assert p._nightly_due(late) is False
    p.cfg = cfg.model_copy(update={"memory_distill_hours": 2})
    store.memory_set("nightly_at", (early - timedelta(hours=3)).isoformat(timespec="seconds"))
    assert p._nightly_due(early) is True, "sub-daily cadence ignores the time-of-day gate"
    store.memory_set("nightly_at", (late - timedelta(minutes=30)).isoformat(timespec="seconds"))
    assert p._nightly_due(late) is False
    p.cfg = cfg

    async def busy(run_model, workspace):
        raise Busy("lock")

    store.memory_set("nightly_date", "2000-01-01")
    p.memory.run_nightly = busy
    asyncio.run(p._run_nightly())
    assert store.memory_get("nightly_date") == "2000-01-01", "a busy lock is not tonight's run"


def test_agent_runs_leave_a_window_for_provenance(tmp_path):
    fake = make_fake_claude(tmp_path, "cat > /dev/null\nprintf '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\",\"session_id\":\"s9\"}'")
    p, store, cfg, memory = make(tmp_path, claude=fake)
    tid = store.create_task(None, "D5", "1.1", kind="dm")
    payload = {"kind": "dm", "channel": "D5", "task_key": "1.1", "reply_thread": "1.1", "in_thread": False, "user": "U1",
               "text": "hi", "ts": "1.1", "channel_type": "im"}
    asyncio.run(p._run_task_reply(store.get_task(tid), payload, {}))
    rows = store._query("SELECT * FROM memory_run_windows")
    assert len(rows) == 1 and rows[0]["task_id"] == tid and rows[0]["kind"] == "dm" and rows[0]["ended_at"]
    assert store.open_windows() == []


def test_workspace_settings_regenerated_even_without_memory(tmp_path):
    from wanda.main import sync_workspace
    cfg = Config(_env_file=None, email_triage_slack_channel_id="C1", data_dir=tmp_path / "data", memory_enabled=False)
    ws = sync_workspace(cfg)
    assert "tool-log" in (ws / ".claude" / "settings.json").read_text()


def test_owner_command_is_live_for_the_next_triage_batch(tmp_path):
    """handle_command applies the rule now, so triage_block shows it without
    waiting for the hourly pass."""
    p, store, cfg, memory = make(tmp_path)
    payload = {"kind": "command", "channel": "D5", "task_key": "3.3", "reply_thread": "3.3", "in_thread": False,
               "user": "U_OWNER", "text": "rule priya@x.example trash", "ts": "3.3", "channel_type": "im", "thread_ts": None}
    from wanda.events import Event
    asyncio.run(p._handle_slack(Event("slack", "D5:3.3", payload)))
    assert "Rule recorded" in p.slack.replies[0]
    block = memory.triage_block([{"from_addr": "priya@x.example"}])
    assert "trash mail from priya@x.example [rule]" in block, "the rule applies to the very next batch"


def test_orphan_run_windows_are_closed_at_startup(tmp_path):
    from wanda.memory.service import MemoryService
    cfg = Config(_env_file=None, email_triage_slack_channel_id="C1", data_dir=tmp_path / "d", memory_dir=tmp_path / "d" / "m")
    store = Store(cfg.db_path)
    store.open_run_window("s-crashed", 7, "email")  # a window a previous daemon never closed
    MemoryService(cfg, store)  # __init__ closes orphans
    assert store.open_windows() == []
