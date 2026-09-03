"""Shared fixtures: a scrubbed environment, a fake `claude` binary, and a
seeded memory vault."""
import os
import stat
from datetime import datetime

import pytest

from wanda.memory.ledger import Observation
from wanda.memory.vault import Vault


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("WANDA_"):
            monkeypatch.delenv(key, raising=False)


def make_fake_claude(tmp_path, script: str) -> str:
    """A shell script standing in for the claude CLI. `script` runs after the
    prompt is available on stdin; it should print an envelope."""
    path = tmp_path / "fake-claude"
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def fake_claude(tmp_path):
    return lambda script: make_fake_claude(tmp_path, script)


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "memory")
    for d in ("belt/ledger", "belt/subjects", "people", "orgs", "topics", "prefs", "open", "retired"):
        (v.root / d).mkdir(parents=True)
    return v


def mk_obs(subject, text, day, src="triage", cause="", op="", facet="mail-pattern", ref="", ulid=None, until="", due=""):
    o = Observation(subject=subject, facet=facet, text=text, src=src, op=op, cause=cause, ref=ref, until=until, due=due,
                    when=datetime.fromisoformat(f"{day}T10:00:00+00:00"))
    if ulid:
        o.ulid = ulid
    return o
