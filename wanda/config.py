from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WANDA_",
        # Anchored to the repo, not the cwd: agent sessions run `wanda slack`
        # from ~/.wanda/workspace, where a relative ".env" resolves to nothing
        # and every command would fail with "token is not set". A .env in the
        # cwd still wins, for local overrides.
        env_file=(Path(__file__).resolve().parent.parent / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # iCloud IMAP
    icloud_email: str = ""
    icloud_app_password: str = ""
    imap_host: str = "imap.mail.me.com"
    imap_port: int = 993
    trash_folder: str = ""  # empty = discover via SPECIAL-USE

    # Slack
    slack_bot_token: str = ""
    slack_app_token: str = ""
    email_triage_slack_channel_id: str = ""
    # Who may TALK to wanda. Empty = anyone in the workspace. Distinct from
    # memory_owner_user_ids, whose word mints owner-tier memory.
    slack_allowed_user_ids: CsvList = Field(default_factory=list)
    # User token (xoxp-), only needed for `wanda slack search`.
    slack_user_token: str = ""
    slack_context_limit: int = 50

    # Enforcement & trash guards
    enforcement: Literal["shadow", "live"] = "shadow"
    never_trash: CsvList = Field(default_factory=list)  # addresses or domains
    trash_confidence_min: float = 0.8
    trash_cap_hourly: int = 5
    trash_cap_daily: int = 20

    # claude CLI
    claude_bin: str = ""
    email_triage_model: str = "claude-haiku-4-5-20251001"
    agent_model: str = "sonnet"
    triage_batch_size: int = 10
    # Wait for a batch to form before triaging: under IMAP IDLE every arrival
    # wakes the processor, and email is not latency-critical.
    triage_debounce_s: int = 150
    triage_timeout_s: int = 120
    agent_timeout_s: int = 900
    # Per-run ceilings. A size limit on one session that loops (there is no
    # --max-turns), not a bill: wanda runs on a subscription plan.
    triage_max_budget_usd: float = 0.25
    agent_max_budget_usd: float = 2.0
    dryrun_max_limit: int = 200
    # Bash is included so sessions can drive `wanda slack` and `wanda memory`.
    # A headless session cannot scope Bash to one command (--allowedTools is
    # not enforced under --permission-mode dontAsk), so this grants a session
    # real shell access — acceptable only in a trusted workspace. See README.
    agent_allowed_tools: str = "Bash,Read,WebSearch,Skill"
    # The only breaker: stops a runaway loop. Set high first, lower with data.
    daily_run_cap: int = 1000

    # memory
    memory_enabled: bool = True
    memory_dir: Path = Path("~/.wanda/memory")
    # Whose Slack messages mint owner-tier memory (rules that decide what
    # happens to mail). Empty = owner-tier minting disabled.
    memory_owner_user_ids: CsvList = Field(default_factory=list)
    memory_model: str = "claude-haiku-4-5-20251001"
    memory_max_budget_usd: float = 0.50
    memory_timeout_s: int = 180
    # Hours between paid distillation passes. The free hourly pass always
    # runs hourly; 24 = once a night at memory_nightly_local_time.
    memory_distill_hours: int = 24
    memory_nightly_local_time: str = "03:30"
    # Reinstate the v3 guard: only owner-said preferences may rewrite a
    # write-spec. Off by default — wanda files autonomously and reports diffs.
    memory_writespec_owner_only: bool = False

    # daemon
    data_dir: Path = Path("~/.wanda")
    idle_timeout_s: int = 720  # re-issue IDLE well under RFC 2177's 29-minute cap
    poll_fallback_s: int = 180
    snippet_bytes: int = 4096
    log_level: str = "INFO"

    @field_validator("slack_allowed_user_ids", "never_trash", "memory_owner_user_ids", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("memory_nightly_local_time")
    @classmethod
    def _check_hhmm(cls, v: str) -> str:
        """Parsed here exactly as main._nightly_due parses it. Left alone, a
        bad value either raises inside memory_tick — caught by memory_loop, so
        the nightly never runs again — or parses into an hour no clock reaches
        (`25:00`, `0330` → hour 330), which disables it forever with not even
        a log line."""
        hh, mm = ((str(v).split(":") + ["0"])[:2])
        hh, mm = hh.strip(), mm.strip()
        if not (hh.isdigit() and mm.isdigit() and int(hh) <= 23 and int(mm) <= 59):
            raise ValueError("must be HH:MM on a 24-hour clock, e.g. 03:30")
        return v

    @property
    def expanded_data_dir(self) -> Path:
        return self.data_dir.expanduser()

    @property
    def db_path(self) -> Path:
        return self.expanded_data_dir / "wanda.db"

    @property
    def dryrun_db_path(self) -> Path:
        """`wanda triage` writes here, never into the daemon's live state."""
        return self.expanded_data_dir / "dryrun.db"

    @property
    def lock_path(self) -> Path:
        return self.expanded_data_dir / "wanda.lock"

    @property
    def workspace_dir(self) -> Path:
        return self.expanded_data_dir / "workspace"

    @property
    def logs_dir(self) -> Path:
        return self.expanded_data_dir / "logs"

    # --- memory paths ---

    @property
    def memory_vault(self) -> Path:
        return self.memory_dir.expanduser()

    @property
    def memory_index_path(self) -> Path:
        return self.expanded_data_dir / "memory.idx"

    @property
    def memory_export_dir(self) -> Path:
        return self.expanded_data_dir / "memory.export"

    @property
    def memory_staging_dir(self) -> Path:
        return self.expanded_data_dir / "memory.staging"

    @property
    def memory_lock_path(self) -> Path:
        return self.expanded_data_dir / "memory.lock"

    @property
    def retire_journal_path(self) -> Path:
        return self.expanded_data_dir / "memory.retire.journal"

    @property
    def triage_cwd(self) -> Path:
        """An empty, harness-owned working directory for the classifier —
        never the daemon's cwd, which under launchd is the repo root with
        .env in it, and --restricted confines file tools to cwd + add-dirs."""
        return self.expanded_data_dir / "triage-cwd"

    @property
    def triage_settings_path(self) -> Path:
        return self.expanded_data_dir / "triage.settings.json"

    def resolve_claude_bin(self) -> str | None:
        return self.claude_bin or shutil.which("claude")


def load_config() -> Config:
    return Config()
