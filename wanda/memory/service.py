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
from wanda.triage import sanitize

log = logging.getLogger(__name__)

MEMO_CAUSE_CAP = 5  # per (subject, facet): past this, without an owner-tier line, a memo is dropped
# Bytes, where prompts/email_triage.md:24, the batch schema (triage.py:44) and
# triage.Memo (triage.py:59) all ask the model for <= 240 *characters*: a
# non-ASCII memo is stored shorter than it was told. Not a safety bound —
# ledger.format_line trims the free text to vault.CLAIM_TEXT_CAP_B (600 B) and
# again to fit LEDGER_LINE_CAP_B, so an oversize memo is appended trimmed,
# never dropped — just the tighter product choice for a short ledger line.
MEMO_TEXT_CAP_B = 240


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
        self.authority = passes.Authority(windows=store.all_windows())
        self.owns_shared_state = False

    def adopt_shared_state(self) -> int:
        """The daemon alone owns the derived state, and says so once, here: a
        run window still open at startup belongs to a run a previous daemon
        never finished, and only a process holding owner authority may build
        the shared index. Every other process — a dry run, the CLI — reads
        both and repairs neither. Runs once at daemon start, before any
        worker thread exists, so the rebind below needs no lock. A new
        construction site that forgets to call this gets a service that
        never builds an index."""
        closed = self.store.close_orphan_windows()
        if closed:
            log.warning("closed %d agent-run window(s) left open by a previous daemon", closed)
        # Re-read after closing, or a crashed run's window still looks open
        # to window_tier for the rest of this process's life.
        self.authority.windows = self.store.all_windows()
        self.owns_shared_state = True
        return closed

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
            lambda: ix.open_readonly(self.cfg.memory_index_path), self.store,
        )

    # --- run windows (provenance) ---

    def open_window(self, session_id: str, task_id: int | None, kind: str) -> None:
        # Event loop only (main.py:685). Rebound, never mutated in place: a
        # pass in a worker thread may be iterating the current list.
        w = self.store.open_run_window(session_id, task_id, kind)
        self.authority.windows = [x for x in self.authority.windows if x["session_id"] != session_id] + [w]

    def close_window(self, session_id: str) -> None:
        # Event loop only (main.py:720). The stamp is one dict-item assignment
        # under the GIL, so a pass iterating the list sees this window either
        # open or closed, never half-written.
        ended = self.store.close_run_window(session_id)
        for w in self.authority.windows:
            if w["session_id"] == session_id and not w.get("ended_at"):
                w["ended_at"] = ended

    def conn_ro(self):
        """A read-only index connection. A missing index is built inline by
        the daemon only (~25 ms at 100 notes, ~370 ms at 1,000, at 5 claims
        and 3 observations per note); a corrupt one is renamed aside and
        rebuilt by the daemon only. In any other process both are None, so
        callers degrade to header-only / no block."""
        path = self.cfg.memory_index_path
        if not path.exists():
            if not self.owns_shared_state:
                log.debug("memory index missing; this process does not build it")
                return None
            try:
                self._rebuild_inline()
            except Exception:
                log.exception("memory index could not be built")
                return None
        try:
            conn = ix.open_readonly(path)
            if conn is None:
                return None
            conn.execute("SELECT 1 FROM docs LIMIT 1")
            if conn.execute("SELECT v FROM meta WHERE k='rebuilt_at'").fetchone() is None:
                # `open_index` commits the schema (empty tables) outside the
                # rebuild transaction, so a build that failed — or has not
                # run — leaves a readable but never-populated index. Without
                # `rebuilt_at` it has never been successfully built: treat it
                # as unavailable so the projection shows the marker rather
                # than rendering as though wanda knows nothing.
                conn.close()
                if not self.owns_shared_state:
                    log.debug("memory index never built (no rebuilt_at); this process does not build it")
                    return None
                try:
                    self._rebuild_inline()
                except Exception:
                    log.exception("memory index could not be built")
                    return None
                conn = ix.open_readonly(path)
                if conn is None:
                    return None
                if conn.execute("SELECT v FROM meta WHERE k='rebuilt_at'").fetchone() is None:
                    conn.close()
                    return None
            return conn
        except sqlite3.DatabaseError as e:
            if not self.owns_shared_state:
                log.warning("memory index unreadable (%s); only the daemon rebuilds it", e)
                return None
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
        return datetime.now(timezone.utc).date().isoformat()  # UTC, matching every persisted date; see Services.today

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
                text = clean_text(str(get("text") or ""), MEMO_TEXT_CAP_B)
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

    def handle_command(self, p: dict) -> str:
        """Mint the owner's lines from a Slack event this daemon received,
        hold their authority in memory, put them in the index now, and apply
        them now — a rule is live for the next triage batch."""
        ctx = commands.Context(channel=p["channel"], ts=p["ts"], user=p["user"], text=p.get("text", ""))
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
                seen: set[str] = set()
                for rec in passes.L.iter_observations(self.vault):
                    # First-wins, matching _load_ledger (index.py:248-254) and
                    # _pending_ops (passes.py:765-771): a second line reusing a
                    # ULID must not REPLACE the first one's obs row, or install
                    # its own veto keys, in the window before the rebuild below.
                    if isinstance(rec, passes.L.Rejected) or rec.ulid in seen:
                        continue
                    seen.add(rec.ulid)
                    if rec.ulid in ulids:
                        ix.insert_observation(conn, rec, ix.tier_for_obs(rec, trust))
                passes._apply_ops(svc, conn, passes.HourlyReport(), self.today(), only=ulids)
                ix.rebuild(self.vault, conn, trust, self.today())
            finally:
                conn.close()
            if svc.touched:
                # wanda's own write, committed as wanda's and under the same
                # lock the hourly pass holds — outside it, that pass's
                # `git add -A` (passes.py:283) could stage these paths into its
                # own commit. Left uncommitted the note is absorbed next pass
                # as "owner edits (auto)" (passes.py:440). Named paths only:
                # _git_commit_paths(vault, msg, []) degrades to a bare
                # `git add -A` (passes.py:314), which would sweep an owner edit
                # in flight into a curated: commit. The ledger line stays for
                # the next pass's `belt:` commit.
                try:
                    passes._git_commit_paths(self.vault, passes._curated_message("owner command", svc), sorted(svc.touched))
                except Exception:
                    log.exception("owner command applied; committing the note failed")

    # --- passes ---

    def run_hourly(self, workspace: Path | None) -> passes.HourlyReport:
        svc = self.services()
        if workspace is not None:
            write_settings(workspace / ".claude" / "settings.json", self.wanda_bin)
        with passes.memory_lock(self.cfg.memory_lock_path):
            conn = passes.open_conn(svc)
            try:
                rep = passes.hourly(svc, conn, workspace)
            finally:
                conn.close()
        # The daemon's scheduler state belongs to the daemon's wrapper: a
        # hand-run `wanda memory hourly` must not postpone the pass that
        # holds owner authority. memory_tick's failure path stamps this too
        # (main.py:893), so a raising pass still does not spin every tick.
        self.store.memory_set("hourly_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return rep

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
