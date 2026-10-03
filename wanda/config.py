from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]
CsvMap = Annotated[dict[str, str], NoDecode]


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

    # Off leaves mail alone: no iCloud settings are needed, nothing is read,
    # and mail rows already in the store are left as they are.
    email_triage: bool = True

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
    # Where failures no one would otherwise see are posted: a private channel's
    # id, or a person's user id for their DM with wanda. Empty uses the triage
    # channel.
    alert_channel: str = ""
    # Who may start a session. The daemon refuses to start with it empty: a
    # session reads the household's whole memory.
    slack_owner_user_ids: CsvList = Field(default_factory=list)
    # Slack user id to the name the vault knows that person by, "U0123:fan".
    # A display name can change, and a second spelling of a person in a
    # session's prompt becomes a second person in the vault.
    slack_names: CsvMap = Field(default_factory=dict)
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

    # the clock. Whose morning gets a look, and when, as <member id>@HH:MM in
    # the household's own time (WANDA_TZ), before noon, each id one of
    # slack_owner_user_ids; empty for none.
    mornings: CsvList = Field(default_factory=list)
    # No morning look starts in these hours; a reminder asked for at a time in
    # them still comes then, and a message still gets its answer.
    quiet_hours: str = "21:30-07:00"

    # daemon
    data_dir: Path = Path("~/.wanda")
    # The vault, when it is not the data directory's vault/. In Docker it is a
    # named volume apart from the data directory, which is on the Mac's mount:
    # a `mem` call takes hundredths of a second on the volume, seconds there.
    vault: str = ""
    # Where the run store (wanda.db), its dry-run twin and the daemon's lock
    # live, when not in the data directory. In Docker a named volume of its
    # own, which the Mac cannot reach: SQLite's locks do not cross the Mac's
    # mount, and a read there while the daemon writes can corrupt the store.
    run_store: str = ""
    # How many memory sessions run at once, 1 or 2; email task sessions take
    # the same places. One unless set: two sessions that overlap can each meet
    # what the other is writing before it ends, and with one a conversation
    # waits for another's session instead.
    memory_sessions: int = 1
    # The household's time zone, e.g. America/Los_Angeles: the date and time
    # every memory session is told, and when its day turns over.
    tz: str = ""
    idle_timeout_s: int = 720  # re-issue IDLE well under RFC 2177's 29-minute cap
    poll_fallback_s: int = 180
    snippet_bytes: int = 4096
    log_level: str = "INFO"

    @field_validator("slack_owner_user_ids", "never_trash", "mornings", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("slack_names", mode="before")
    @classmethod
    def _split_pairs(cls, v: object) -> object:
        if isinstance(v, str):
            pairs = [s.split(":", 1) for s in v.split(",") if s.strip()]
            if any(len(p) != 2 or not p[0].strip() or not p[1].strip() for p in pairs):
                raise ValueError("expected id:name pairs, comma-separated")
            return {i.strip(): n.strip() for i, n in pairs}
        return v

    @field_validator("memory_sessions", mode="before")
    @classmethod
    def _default_when_empty(cls, v: object) -> object:
        # compose passes a setting .env leaves empty as "", which means unset
        if isinstance(v, str) and not v.strip():
            return cls.model_fields["memory_sessions"].default
        return v

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def alerts_to(self) -> str:
        return self.alert_channel or self.email_triage_slack_channel_id

    @property
    def expanded_data_dir(self) -> Path:
        return self.data_dir.expanduser()

    @property
    def run_store_dir(self) -> Path:
        return Path(self.run_store).expanduser() if self.run_store else self.expanded_data_dir

    @property
    def db_path(self) -> Path:
        return self.run_store_dir / "wanda.db"

    @property
    def dryrun_db_path(self) -> Path:
        """`wanda triage` writes here, never into the daemon's live state."""
        return self.run_store_dir / "dryrun.db"

    @property
    def lock_path(self) -> Path:
        return self.run_store_dir / "wanda.lock"

    @property
    def vault_dir(self) -> Path:
        return Path(self.vault).expanduser() if self.vault else self.expanded_data_dir / "vault"

    @property
    def snapshots_dir(self) -> Path:
        """A bare repository in the data directory, never inside the vault:
        the vault's own .git stays empty, and its history is not one `git log`
        away."""
        return self.expanded_data_dir / "snapshots.git"

    def resolve_claude_bin(self) -> str | None:
        return self.claude_bin or shutil.which("claude")


def load_config() -> Config:
    return Config()
