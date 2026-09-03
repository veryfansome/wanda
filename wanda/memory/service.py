"""Glue between the daemon and the memory package: paths, the authority
the daemon holds in memory, seeding, the triage block, owner commands, and
the two passes."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from wanda.memory import audit, commands, index as ix, passes, recall, render
from wanda.memory.ledger import Observation, append as ledger_append, line_fingerprint
from wanda.memory.subjects import subject_from_address
from wanda.memory.vault import Vault, clean_text, slugify, write_atomic
from wanda.triage import addresses_in, sanitize

log = logging.getLogger(__name__)

MEMO_CAUSE_CAP = 5  # per (subject, facet): past this, without an owner-tier line, a memo is dropped


def default_wanda_bin() -> str:
    cand = Path(sys.executable).parent / "wanda"
    return str(cand) if cand.exists() else "wanda"


def write_settings(path: Path, wanda_bin: str) -> None:
    """The hook registration, regenerated from the template every time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, audit.settings_json(wanda_bin))


class MemoryService:
    def __init__(self, cfg, store, slack=None, wanda_bin: str | None = None):
        self.cfg = cfg
        self.store = store
        self.slack = slack
        self.vault = Vault(cfg.memory_vault)
        self.wanda_bin = wanda_bin or default_wanda_bin()
        # Owner authority and run windows live here, in this process. A
        # session can write wanda.db; it cannot write the daemon's memory.
        closed = store.close_orphan_windows()
        if closed:
            log.warning("closed %d agent-run window(s) left open by a previous daemon", closed)
        self.authority = passes.Authority(windows=store.all_windows())

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
        write_settings(self.cfg.triage_settings_path, self.wanda_bin)
        return self.cfg.triage_settings_path

    def services(self) -> passes.Services:
        return passes.Services(self.cfg, self.store, self.vault, verify_owner=self._verifier(), authority=self.authority)

    def _verifier(self):
        if self.slack is None or not self.cfg.memory_owner_user_ids:
            return None
        return passes.make_owner_verifier(
            self.slack.fetch_message_sync, list(self.cfg.memory_owner_user_ids),
            lambda: ix.open_readonly(self.cfg.memory_index_path), self.store, self.sender_for_thread,
        )

    # --- run windows (provenance) ---

    def open_window(self, session_id: str, task_id: int | None, kind: str) -> None:
        w = self.store.open_run_window(session_id, task_id, kind)
        self.authority.windows = [x for x in self.authority.windows if x["session_id"] != session_id] + [w]

    def close_window(self, session_id: str) -> None:
        ended = self.store.close_run_window(session_id)
        for w in self.authority.windows:
            if w["session_id"] == session_id and not w.get("ended_at"):
                w["ended_at"] = ended

    def conn_ro(self):
        """A read-only index connection. A missing index is built inline
        (measured 5–53 ms); a corrupt one is renamed aside and rebuilt; if
        that fails too, None — callers degrade to header-only / no block."""
        path = self.cfg.memory_index_path
        try:
            if not path.exists():
                self._rebuild_inline()
            conn = ix.open_readonly(path)
            if conn is None:
                return None
            conn.execute("SELECT 1 FROM docs LIMIT 1")
            return conn
        except sqlite3.DatabaseError as e:
            log.warning("memory index unreadable (%s); rebuilding", e)
            try:
                for suffix in ("", "-wal", "-shm"):
                    p = Path(str(path) + suffix)
                    if p.exists():
                        p.rename(str(p) + ".corrupt")
                self._rebuild_inline()
                return ix.open_readonly(path)
            except Exception:
                log.exception("memory index could not be rebuilt")
                return None
        except Exception:
            log.exception("memory index unavailable")
            return None

    def _rebuild_inline(self) -> None:
        svc = self.services()
        try:
            with passes.memory_lock(self.cfg.memory_lock_path):
                conn = passes.open_conn(svc)
                try:
                    ix.rebuild(self.vault, conn, svc.trust(), self.today())
                finally:
                    conn.close()
        except passes.Busy:
            pass  # a pass is building it right now

    def today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    # --- workspace ---

    def prepare_workspace(self, workspace: Path) -> None:
        """The composed projection, before every agent run, resumed turns
        included, because CLAUDE.md is re-read on every turn. Never raises."""
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
        by the harness from the real From header (and follows the note it
        belongs to); the model never names it. At most one memo per verdict;
        dropped once a (subject, facet) has five causes and no owner-tier
        line, so a newsletter cannot write forever. Drops are logged."""
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
                    log.debug("memo dropped: no address in %r", row["from_addr"])
                    continue
                if conn is not None:
                    subject = ix.canonical_subject(conn, subject)
                get = (lambda k: getattr(memo, k, None)) if not isinstance(memo, dict) else memo.get
                facet = slugify(str(get("facet") or "mail-pattern"), 32) or "mail-pattern"
                text = clean_text(str(get("text") or ""), 240)
                if not text:
                    continue
                if conn is not None and self._memo_saturated(conn, subject, facet):
                    log.debug("memo dropped: %s/%s has %d causes and no owner line", subject, facet, MEMO_CAUSE_CAP)
                    continue
                o = Observation(subject=subject, facet=facet, text=text, src="triage", cause=f"m:{key[:12]}")
                try:
                    ledger_append(self.vault, o)
                except ValueError as e:
                    log.warning("memo dropped: %s", e)
                    continue
                n += 1
        except Exception:
            log.exception("recording triage memos failed")
        finally:
            if conn is not None:
                conn.close()
        return n

    @staticmethod
    def _memo_saturated(conn, subject: str, facet: str) -> bool:
        rows = conn.execute("SELECT src, cause, day, ulid, tier FROM obs WHERE subject=? AND facet=? AND op=''", (subject, facet)).fetchall()
        if any(r["tier"] == "owner" for r in rows):
            return False
        causes = {ix.cause_key(r["src"], r["cause"], r["day"], r["ulid"]) for r in rows}
        return len(causes) >= MEMO_CAUSE_CAP

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
        a = addresses_in(msg["from_addr"] or "")
        return a[0] if a else ""

    def handle_command(self, p: dict) -> str:
        """Mint the owner's lines from a Slack event this daemon received,
        hold their authority in memory, put them in the index now, and apply
        them now — a rule is live for the next triage batch."""
        ctx = commands.Context(channel=p["channel"], ts=p["ts"], user=p["user"], text=p.get("text", ""),
                               task_sender=self.sender_for_thread(p["channel"], p.get("thread_ts") or ""))
        conn = self.conn_ro()
        try:
            minted = commands.handle(ctx, conn, self.store, list(self.cfg.memory_owner_user_ids))
        finally:
            if conn is not None:
                conn.close()
        if not minted.observations:
            return minted.reply
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.set_owner_check(ctx.cause, True, passes.MINTED_IN_PROCESS)
        for o in minted.observations:
            ledger_append(self.vault, o)
            self.authority.minted[o.ulid] = line_fingerprint(o)
            self.store.memory_set(f"checked:{o.ulid}", stamp)
        try:
            self.apply_now({o.ulid for o in minted.observations})
        except passes.Busy:
            log.info("owner command recorded; a pass holds the lock, it applies on the next one")
        except Exception:
            log.exception("owner command recorded but not applied yet; the next pass will")
        return minted.reply

    def apply_now(self, ulids: set[str]) -> None:
        """Zero-lag for owner ops: index the lines and apply them under the
        lock, then rebuild so the projection and triage see the result."""
        svc = self.services()
        with passes.memory_lock(self.cfg.memory_lock_path):
            conn = passes.open_conn(svc)
            try:
                trust = svc.trust()
                for rec in passes.L.iter_observations(self.vault):
                    if not isinstance(rec, passes.L.Rejected) and rec.ulid in ulids:
                        ix.insert_observation(conn, rec, ix.tier_for_obs(rec, trust))
                passes._apply_ops(svc, conn, passes.HourlyReport(), self.today(), only=ulids)
                ix.rebuild(self.vault, conn, trust, self.today())
            finally:
                conn.close()

    # --- passes ---

    def run_hourly(self, workspace: Path | None) -> passes.HourlyReport:
        svc = self.services()
        if workspace is not None:
            write_settings(workspace / ".claude" / "settings.json", self.wanda_bin)
        with passes.memory_lock(self.cfg.memory_lock_path):
            conn = passes.open_conn(svc)
            try:
                return passes.hourly(svc, conn, workspace)
            finally:
                conn.close()

    async def run_nightly(self, run_model, workspace: Path | None) -> passes.NightlyReport:
        """Prepare and apply in worker threads under the lock; the model
        calls in between hold nothing, so hourly ticks are never starved.
        Everything the model said is staged as one file before the vault is
        touched, so a busy lock never loses paid output."""
        svc = self.services()

        def prepare():
            with passes.memory_lock(self.cfg.memory_lock_path):
                conn = passes.open_conn(svc)
                try:
                    return passes.nightly_prepare(svc, conn)
                finally:
                    conn.close()

        prep = await asyncio.to_thread(prepare)
        calls = 0
        budget = False
        distill_out = writespec_out = None
        try:
            if prep.ask or prep.contras:
                distill_out = await run_model((passes.PROMPTS_DIR / "memory_distill.md").read_text(),
                                              passes.distill_prompt(prep.ask, prep.contras), passes.RESOLUTION_SCHEMA)
                calls += 1
            if prep.writespec_prompt:
                writespec_out = await run_model((passes.PROMPTS_DIR / "memory_writespec.md").read_text(),
                                                prep.writespec_prompt, passes.WRITESPECS_SCHEMA)
                calls += 1
        except passes.BudgetReached:
            # The run cap stopped the paid call. Apply the free graduations and
            # offers, leave the model candidates to retry, and report a skip.
            budget = True
        payload = passes.merge_model_output(prep, distill_out, writespec_out)
        staged = passes.stage(svc, payload) if (payload.get("resolutions") or payload.get("writespecs")) else None

        def apply():
            with passes.memory_lock(self.cfg.memory_lock_path):
                conn = passes.open_conn(svc)
                try:
                    rep = passes.nightly_apply(svc, conn, prep, payload, workspace)
                finally:
                    conn.close()
            if staged is not None and not rep.deferred and not rep.writespecs_deferred:
                staged.unlink(missing_ok=True)
            return rep

        rep = await asyncio.to_thread(apply)
        rep.model_calls = calls
        if budget:
            rep.skipped_reason = "budget"
        return rep
