"""Glue between the daemon and the memory package: paths, the trust
oracle, seeding, the triage block, owner commands, and the two passes."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from wanda.memory import audit, commands, index as ix, passes, recall, render
from wanda.memory.ledger import Observation, append as ledger_append
from wanda.memory.subjects import subject_from_address
from wanda.memory.vault import Vault, clean_text

log = logging.getLogger(__name__)


class MemoryService:
    def __init__(self, cfg, store, slack=None, wanda_bin: str | None = None):
        self.cfg = cfg
        self.store = store
        self.slack = slack
        self.vault = Vault(cfg.memory_vault)
        self.wanda_bin = wanda_bin or _default_wanda_bin()

    # --- setup ---

    def ensure(self) -> list[str]:
        """Seed the vault (copy-if-absent), create the harness-owned dirs and
        the triage settings file. Safe to call on every start."""
        created = passes.ensure_vault(self.services())
        self.cfg.triage_cwd.mkdir(parents=True, exist_ok=True)
        self.cfg.memory_export_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        self.write_triage_settings()
        return created

    def write_triage_settings(self) -> Path:
        """Outside the workspace, regenerated per batch: a session editing
        workspace settings must not change triage's hooks next batch."""
        from wanda.memory.vault import write_atomic
        write_atomic(self.cfg.triage_settings_path, audit.settings_json(self.wanda_bin))
        return self.cfg.triage_settings_path

    def services(self) -> passes.Services:
        return passes.Services(self.cfg, self.store, self.vault, verify_owner=self._verifier())

    def _verifier(self):
        if self.slack is None or not self.cfg.memory_owner_user_ids:
            return None
        return passes.make_owner_verifier(
            self.slack.fetch_message_sync, list(self.cfg.memory_owner_user_ids),
            lambda: ix.open_readonly(self.cfg.memory_index_path), self.store, self.sender_for_thread,
        )

    def conn_ro(self):
        try:
            return ix.open_readonly(self.cfg.memory_index_path)
        except Exception:
            return None

    def today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    # --- workspace ---

    def prepare_workspace(self, workspace: Path) -> None:
        """Composed projection + regenerated settings, before every agent run.
        Never raises: a missing or broken index yields a header-only file."""
        from wanda.memory.vault import write_atomic
        (workspace / ".claude").mkdir(parents=True, exist_ok=True)
        write_atomic(workspace / ".claude" / "settings.json", audit.settings_json(self.wanda_bin))
        conn = None
        try:
            conn = self.conn_ro()
            text = render.compose_projection(self.vault, conn, self.today())
        except Exception:
            log.exception("projection failed; writing header only")
            text = render.compose_projection(self.vault, None, self.today())
        finally:
            if conn is not None:
                conn.close()
        render.write_projection(workspace, text)

    # --- seeds ---

    def seed_for_conversation(self, p: dict) -> tuple[str, str]:
        """(memory block, prior answers block) for a mention/DM seed."""
        conn = self.conn_ro()
        memory = ""
        try:
            if conn is not None:
                memory = recall.for_agent(self.vault, conn, recall.AgentContext(asker_slack_id=p.get("user", ""), text=p.get("text", "")), self.today())
        except Exception:
            log.exception("memory recall failed; seeding without it")
        finally:
            if conn is not None:
                conn.close()
        prior = ""
        if p.get("kind") == "dm":
            answers = self.store.recent_answers(p["channel"], limit=3)
            if answers:
                from wanda.triage import sanitize
                prior = "Your earlier answers in this conversation, oldest first:\n<transcript>\n" + "\n".join(
                    f"[{a['started_at'][:16]}] wanda: {sanitize((a['result_text'] or '')[:1200])}" for a in answers) + "\n</transcript>\n\n"
        return memory, prior

    def seed_for_email(self, row) -> str:
        conn = self.conn_ro()
        try:
            if conn is None:
                return ""
            return recall.for_agent(self.vault, conn, recall.AgentContext(
                sender_addr=row["from_addr"] or "", subject_hdr=row["subject"] or "", text=""), self.today())
        except Exception:
            log.exception("memory recall failed for email task")
            return ""
        finally:
            if conn is not None:
                conn.close()

    # --- triage ---

    def triage_block(self, rows) -> str:
        conn = self.conn_ro()
        try:
            if conn is None:
                return ""
            return recall.for_triage(conn, rows, self.store.sender_stats, self.cfg.memory_export_dir)
        except Exception:
            log.exception("triage memory block failed")
            return ""
        finally:
            if conn is not None:
                conn.close()

    def record_memos(self, rows_by_key: dict, verdicts: dict) -> int:
        """Triage memos become email-tier ledger lines. The subject is bound
        by the harness from the real From header; the model never names it.
        At most one memo per verdict; dropped once a (subject, facet) has
        five causes and no owner edge, so a newsletter cannot write forever."""
        n = 0
        conn = self.conn_ro()
        try:
            for key, v in verdicts.items():
                memo = getattr(v, "memo", None)
                row = rows_by_key.get(key)
                if not memo or row is None:
                    continue
                subject = subject_from_address(row["from_addr"] or "")
                if not subject:
                    continue
                get = (lambda k: getattr(memo, k, None)) if not isinstance(memo, dict) else memo.get
                facet = clean_text(str(get("facet") or "mail-pattern"), 32).lower().replace(" ", "-")[:32] or "mail-pattern"
                text = clean_text(str(get("text") or ""), 240)
                if not text:
                    continue
                if conn is not None:
                    r = conn.execute("SELECT n_causes FROM subjects WHERE key=?", (subject,)).fetchone()
                    if r and (r["n_causes"] or 0) >= 5:
                        continue
                ledger_append(self.vault, Observation(subject=subject, facet=facet, text=text, src="triage",
                                                      cause=f"m:{key[:12]}"))
                n += 1
        except Exception:
            log.exception("recording triage memos failed")
        finally:
            if conn is not None:
                conn.close()
        return n

    # --- owner commands ---

    def sender_for_thread(self, channel: str, thread_ts: str) -> str:
        if not thread_ts:
            return ""
        task = self.store.get_task_by_thread(channel, thread_ts)
        if task is None or task["kind"] != "email" or not task["message_pk"]:
            return ""
        msg = self.store.get_message(task["message_pk"])
        if msg is None:
            return ""
        from wanda.triage import addresses_in
        a = addresses_in(msg["from_addr"] or "")
        return a[0] if a else ""

    def handle_command(self, p: dict) -> str:
        ctx = commands.Context(channel=p["channel"], ts=p["ts"], user=p["user"], text=p.get("text", ""),
                               task_sender=self.sender_for_thread(p["channel"], p.get("thread_ts") or ""))
        conn = self.conn_ro()
        try:
            minted = commands.handle(ctx, conn, self.store, list(self.cfg.memory_owner_user_ids))
        finally:
            if conn is not None:
                conn.close()
        for o in minted.observations:
            ledger_append(self.vault, o)
        if minted.observations:
            # The harness itself saw this message from this author: verified.
            self.store.set_owner_check(ctx.cause, True, "minted in-process")
            for o in minted.observations:
                self.store.memory_set(f"checked:{o.ulid}", "1")
        return minted.reply

    # --- passes ---

    def run_hourly(self, workspace: Path | None) -> passes.HourlyReport:
        svc = self.services()
        if workspace is not None:
            from wanda.memory.vault import write_atomic
            (workspace / ".claude").mkdir(parents=True, exist_ok=True)
            write_atomic(workspace / ".claude" / "settings.json", audit.settings_json(self.wanda_bin))
        with passes.memory_lock(self.cfg.memory_lock_path):
            conn = passes.open_conn(svc)
            try:
                return passes.hourly(svc, conn, workspace)
            finally:
                conn.close()

    async def run_nightly(self, run_model, workspace: Path | None) -> passes.NightlyReport:
        svc = self.services()
        with passes.memory_lock(self.cfg.memory_lock_path):
            conn = passes.open_conn(svc)
            try:
                return await passes.nightly(svc, conn, run_model, workspace)
            finally:
                conn.close()


def _default_wanda_bin() -> str:
    import sys
    cand = Path(sys.executable).parent / "wanda"
    return str(cand) if cand.exists() else "wanda"


def provenance_env(task_id: int | None, session_id: str, user: str) -> dict[str, str]:
    return {
        "WANDA_TASK_ID": str(task_id) if task_id else "",
        "WANDA_SESSION_ID": session_id or "",
        "WANDA_SLACK_USER": user or "",
    }
