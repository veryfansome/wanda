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
    # Empty = anyone in the workspace may talk to wanda. Set it to restrict
    # who can trigger agent sessions.
    slack_owner_user_ids: CsvList = Field(default_factory=list)
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
    triage_timeout_s: int = 120
    agent_timeout_s: int = 900
    triage_max_budget_usd: float = 0.25
    agent_max_budget_usd: float = 2.0
    # What an in-flight run is expected to cost. Reserving the *ceiling*
    # instead would let two queued replies exhaust a $5 day at $0 real spend.
    triage_expected_usd: float = 0.05
    agent_expected_usd: float = 0.40
    dryrun_max_limit: int = 200
    # Bash is included so sessions can drive `wanda slack`. Note that a headless
    # session cannot scope Bash to one command (--allowedTools is not enforced
    # under --permission-mode dontAsk), so this grants a session real shell
    # access — acceptable only in a trusted workspace. See README.
    agent_allowed_tools: str = "Bash,Read,WebSearch,Skill"
    daily_run_cap: int = 200
    daily_cost_cap_usd: float = 5.0

    # daemon
    data_dir: Path = Path("~/.wanda")
    idle_timeout_s: int = 720  # re-issue IDLE well under RFC 2177's 29-minute cap
    poll_fallback_s: int = 180
    snippet_bytes: int = 4096
    log_level: str = "INFO"

    @field_validator("slack_owner_user_ids", "never_trash", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
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

    def resolve_claude_bin(self) -> str | None:
        return self.claude_bin or shutil.which("claude")


def load_config() -> Config:
    return Config()
