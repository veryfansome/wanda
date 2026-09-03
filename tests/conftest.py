"""Shared fixtures: a scrubbed environment, a fake `claude` binary, and a
seeded memory vault."""
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from wanda.memory.ledger import Observation
from wanda.memory.vault import Vault

CONVERSATION_KINDS = ("mention", "mention_guest", "dm")


@dataclass
class DictTrust:
    """A TrustOracle for tests: verified causes, checked lines, and windows
    given as (start, end, kind, pgid)."""
    verified_causes: set[str] = field(default_factory=set)
    task_kinds: dict[int, str] = field(default_factory=dict)  # legacy in old tests; the index no longer reads task ids
    checked_lines: set[str] | None = None      # None = every line under a verified cause counts
    email_windows: list[tuple[datetime, datetime]] = field(default_factory=list)
    windows: list[tuple] = field(default_factory=list)   # (start, end, kind, pgid)

    def owner_verified(self, cause: str) -> bool:
        return cause in self.verified_causes

    def line_checked(self, ulid: str) -> bool:
        return True if self.checked_lines is None else ulid in self.checked_lines

    def line_authored(self, ulid: str) -> bool:
        return False

    def _covering(self, when):
        out = [(s, e, k) for s, e, k, *_ in self.windows if s <= when <= e]
        out += [(s, e, "email") for s, e in self.email_windows if s <= when <= e]
        return out

    def window_tier(self, when: datetime) -> str:
        return "email" if any(w[2] not in CONVERSATION_KINDS for w in self._covering(when)) else "session"


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
