"""The tool-call audit log: a PostToolUse hook that appends one JSON line per
tool call. Tamper-evident, not tamper-proof — a session can edit its own log,
which is why each line is also mirrored to the unified log."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEEP_DAYS = 90
SALIENT = {
    "Read": ("file_path",), "Write": ("file_path",), "Edit": ("file_path",), "Bash": ("command",),
    "Glob": ("pattern", "path"), "Grep": ("pattern", "path"), "WebFetch": ("url",), "WebSearch": ("query",),
    "Skill": ("skill",),
}


def settings_json(wanda_bin: str) -> str:
    """The settings file that registers the hook. Regenerated from this
    template on every run, so a hook a session removed comes back."""
    return json.dumps({
        "hooks": {
            "PostToolUse": [
                {"matcher": "", "hooks": [{"type": "command", "command": f"{wanda_bin} hook tool-log", "timeout": 10}]}
            ]
        }
    }, indent=2) + "\n"


def salient_input(tool: str, tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return str(tool_input)[:500]
    keys = SALIENT.get(tool)
    if keys:
        parts = [str(tool_input.get(k, "")) for k in keys if tool_input.get(k)]
        return " ".join(parts)[:500]
    return ",".join(sorted(str(k) for k in tool_input))[:200]


def log_line(event: dict, env: dict[str, str], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tool = str(event.get("tool_name") or "")
    return {
        "ts": now.isoformat(timespec="seconds"),
        "session": str(event.get("session_id") or "")[:64],
        "task": env.get("WANDA_TASK_ID", ""),
        "lane": env.get("WANDA_LANE", ""),
        "tool": tool[:64],
        "input": salient_input(tool, event.get("tool_input")),
        "cwd": str(event.get("cwd") or "")[:300],
    }


def append_line(logs_dir: Path, line: dict) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    day = line["ts"][:10]
    path = logs_dir / f"tools-{day}.jsonl"
    data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, data)
    finally:
        os.close(fd)
    return path


def prune(logs_dir: Path, keep_days: int = KEEP_DAYS, now: datetime | None = None) -> int:
    if not logs_dir.is_dir():
        return 0
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=keep_days)).date().isoformat()
    n = 0
    for p in logs_dir.glob("tools-????-??-??.jsonl"):
        if p.name[6:16] < cutoff:
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n


def mirror(line: dict) -> None:
    """Best-effort copy to the unified log, which a same-user process cannot
    rewrite. Never raises, never blocks a session."""
    try:
        subprocess.run(["logger", "-t", "wanda-tools", json.dumps(line, ensure_ascii=False)],
                       timeout=2, capture_output=True, check=False)
    except Exception:
        pass


def run_hook(logs_dir: Path, stdin=None, env: dict[str, str] | None = None) -> int:
    """Entry point for `wanda hook tool-log`. Reads the hook JSON from stdin,
    re-serialises the salient part (never shell-interpolates it), appends
    under flock, exits 0 no matter what — a full disk must not break a session."""
    try:
        raw = (stdin or sys.stdin).read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            event = {}
        line = log_line(event, env if env is not None else dict(os.environ))
        append_line(logs_dir, line)
        mirror(line)
        if int(time.time()) % 50 == 0:  # occasional, cheap
            prune(logs_dir)
    except Exception:
        pass
    return 0


def summarize(logs_dir: Path, days: int = 1, allowed_roots: list[str] | None = None, now: datetime | None = None) -> dict:
    """For doctor and the digest: counts by tool, plus anything a session did
    that nothing asked for — reads outside the granted roots, shell commands
    that are not `wanda ...`."""
    now = now or datetime.now(timezone.utc)
    out = {"calls": 0, "by_tool": {}, "reads_outside": [], "shell_other": [], "sessions": set()}
    roots = [os.path.expanduser(r) for r in (allowed_roots or [])]
    for i in range(days):
        day = (now - timedelta(days=i)).date().isoformat()
        p = logs_dir / f"tools-{day}.jsonl"
        if not p.exists():
            continue
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            out["calls"] += 1
            tool = line.get("tool", "?")
            out["by_tool"][tool] = out["by_tool"].get(tool, 0) + 1
            out["sessions"].add(line.get("session", ""))
            inp = line.get("input", "")
            if tool in ("Read", "Write", "Edit", "Glob", "Grep") and roots and inp:
                if not any(os.path.expanduser(inp).startswith(r) for r in roots):
                    out["reads_outside"].append(inp[:200])
            if tool == "Bash" and inp and not inp.lstrip().startswith(("wanda ", "uv run wanda")):
                out["shell_other"].append(inp[:200])
    out["sessions"] = len(out["sessions"])
    return out
