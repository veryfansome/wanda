"""The settings the household's container runs with, and `mem` as the
image's PATH finds it."""

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wanda.config import Config
from wanda.main import settings_problem

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("WANDA_"):
            monkeypatch.delenv(key, raising=False)


def test_settings_problem():
    assert "anyone" in settings_problem(Config(_env_file=None))
    c = Config(_env_file=None, slack_owner_user_ids="U1,U2", slack_names="U1:fan")
    assert "U2" in settings_problem(c)
    c = Config(_env_file=None, slack_owner_user_ids="U1,U2", slack_names="U1:fan, U2:mei")
    assert settings_problem(c) is None and c.slack_names == {"U1": "fan", "U2": "mei"}
    with pytest.raises(ValueError, match="id:name pairs"):
        Config(_env_file=None, slack_names="U1:fan,mei")


def test_triage_can_be_off_and_alerts_go_where_set():
    assert Config(_env_file=None, email_triage="off").email_triage is False
    assert Config(_env_file=None).email_triage is True
    assert Config(_env_file=None, email_triage_slack_channel_id="C1").alerts_to == "C1"
    assert Config(_env_file=None, email_triage_slack_channel_id="C1", alert_channel="G2").alerts_to == "G2"


def test_the_run_store_can_live_apart_from_the_data_directory(tmp_path):
    c = Config(_env_file=None, data_dir=tmp_path / "home.d")
    assert c.db_path == tmp_path / "home.d" / "wanda.db"
    c = Config(_env_file=None, data_dir=tmp_path / "home.d", run_store="/srv/wanda/store")
    assert [c.db_path, c.lock_path, c.dryrun_db_path] == [
        Path("/srv/wanda/store") / n for n in ("wanda.db", "wanda.lock", "dryrun.db")]


# --- `mem` as the image's PATH finds it ---

@pytest.fixture
def wrapped(tmp_path):
    """docker/mem as the image lays it out, in front of a stand-in for the
    real `mem` that prints its dates."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "libexec").mkdir()
    shutil.copy(ROOT / "docker" / "mem", tmp_path / "bin" / "mem")
    (tmp_path / "libexec" / "mem").write_text('#!/bin/sh\necho "$MEM_DATE $MEM_REAL_DATE $MEM_UTC_OFFSET $*"\n')
    for f in (tmp_path / "bin" / "mem", tmp_path / "libexec" / "mem"):
        f.chmod(0o755)
    return tmp_path / "bin" / "mem"


def by_hand(mem: Path, **env) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("MEM_", "WANDA_"))}
    return subprocess.run([str(mem), "recall", "me"], env=base | env, capture_output=True, text=True)


def test_a_call_by_hand_is_dated_in_the_households_zone(wrapped):
    """In a zone whose date is not UTC's now, as the household's is of an
    evening, the date is the zone's."""
    zone = "Etc/GMT+12" if datetime.now(timezone.utc).hour < 12 else "Etc/GMT-14"
    done = by_hand(wrapped, WANDA_TZ=zone)
    here = datetime.now(ZoneInfo(zone))
    assert here.date() != datetime.now(timezone.utc).date()
    offset = int(here.utcoffset().total_seconds())
    assert done.stdout == f"{here.date()} {here.date()} {offset} recall me\n", done.stderr
    # half hours, and hours that read as octal with their leading zero
    for zone, offset in (("Asia/Kolkata", 19800), ("Asia/Shanghai", 28800), ("Pacific/Chatham", None)):
        offset = offset or int(datetime.now(ZoneInfo(zone)).utcoffset().total_seconds())
        assert by_hand(wrapped, WANDA_TZ=zone).stdout.split()[2] == str(offset)


def test_a_sessions_dates_pass_through(wrapped):
    done = by_hand(wrapped, MEM_DATE="2030-01-02", MEM_REAL_DATE="2030-01-02", MEM_UTC_OFFSET="3600")
    assert done.stdout == "2030-01-02 2030-01-02 3600 recall me\n"


def test_no_date_without_a_zone(wrapped):
    zones = Path("/usr/share/zoneinfo")
    assert (zones / "America").is_dir() and (zones / "zone.tab").is_file()
    for env in ({}, {"WANDA_TZ": "Mars/Olympus"}, {"WANDA_TZ": "America"}, {"WANDA_TZ": "zone.tab"}):
        done = by_hand(wrapped, **env)
        assert done.returncode == 1 and done.stdout == "" and "WANDA_TZ" in done.stderr
