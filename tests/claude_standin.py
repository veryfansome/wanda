"""A stand-in for `claude -p --input-format stream-json --output-format
stream-json --verbose`, for tests that cannot start a session. It writes the
events and transcript lines the pinned CLI (2.1.268) wrote for sessions run
against it with their input open, in their shapes, trimmed:

- it reads its input from the start, but takes the first message only once
  it has started (`startup_s`, 0.3 s unless set; the CLI's first turn began
  0.74 to 2.08 s after the first write in those sessions), and each turn
  takes every message waiting when it starts as one, recorded as one `user`
  entry: strings joined with a newline, text blocks one after another, as
  2.1.268's code reads (none of those sessions wrote two messages in that
  window);
- each turn opens with a `system`/`init` event, after it has taken its
  messages, and ends with one `result` event carrying `structured_output`, a
  cumulative `total_cost_usd` and `result_index`;
- a user message read from stdin while a turn runs is handed to that turn
  after the tool call in progress returns, recorded in the transcript as an
  `attachment` of type `queued_command` (with the CLI's wrapper in
  `rendered`) and `queue-operation` lines, and nothing on stdout;
- one read after the turn's last tool call starts a further turn when the
  first has given its result, recorded as an ordinary `user` entry;
- on the end of stdin, what is still queued is run before it exits, as
  2.1.268's code reads; none of those sessions ran it, and STANDIN's
  `on_eof` "drop" exits without running it instead.

It also stands in for `claude -p --output-format json`, as a session the
clock starts runs: the prompt is all of stdin, recorded as a string, the
turns run as above, and only the last turn's result is printed, once it
exits, as 2.1.268's code reads.

What it does is set by the JSON in the STANDIN environment variable, or by
each of a list of them, one for each session in the order they start under
the same home, the last for every one after:
`startup_s`, `steps` (seconds each tool call of the first turn takes),
`reply_s` (time from the last tool call to the result), `steps_later` (the
tool calls of a later turn), `fail` ("first" or "later": that turn's result
is an error, of the subtype `fail_subtype`, error_during_execution unless
set), `no_report` ("first" or "later": that turn ends as a success
carrying the model's text and no structured output, as the first turns of
two of those sessions did), `crash` ("later": the process exits 1
in a later turn, before its result, as a crash or a kill from outside would
end it), `silent_later` (a later turn's answer is empty), `notify` ("mid":
a background command's notice of its end handed at the first step, in mode
"task-notification", as 2.1.268's code reads; "after": one that starts a
turn of its own after the first result, which answers nothing), `on_eof`,
`hang` (never answer; "later": only in a later turn), `big` (a tool result
this long, in characters),
`stderr` (characters written to stderr first), `leave` (start a process that
outlives it, its pid written to ~/left.pid), `answer_new` (a later turn
answers only what it was handed since the turn before), `event` (an event
written after the first tool call), `result_without` (a field every result
leaves out), `refuse` (`turn`, "first" or "later": that turn is refused
before any step, Claude Code saying `said` in an assistant message whose
`error` is `error`, one of the list its output schema gives that field, or
none when `error` is null, and in a result that is an error; no session has
shown this result's shape, which follows the error results above).

Its answer says how many messages it was handed, and which, so a test can
tell what the one answer covered."""

import atexit
import contextlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

NOTICE = ("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
          "<summary>Background command \"sleep\" completed (exit code 0)</summary>\n</task-notification>")
WRAPPER = (
    "<system-reminder>\nThe user sent a new message while you were working:\n{text}\n\n"
    "This is how Claude Code surfaces messages the user sends mid-turn — within the "
    "running turn, often alongside the next tool result, rather than as a separate "
    "conversation turn. Address the message above as you continue this turn.\n</system-reminder>"
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def arg(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def said(text):
    """The words of the message a text carries: its last line indented four
    spaces, as the harness's frames indent a message, or its last line."""
    lines = [l for l in text.splitlines() if l.strip()]
    framed = [l[4:] for l in lines if l.startswith("    ") and not l.startswith("     ")]
    return (framed or lines or [""])[-1].strip()


def main():
    argv = sys.argv[1:]
    cfg = json.loads(os.environ.get("STANDIN") or "{}")
    if isinstance(cfg, list):
        counted = os.path.join(os.path.expanduser("~"), "standin-sessions")
        n = int(open(counted).read()) if os.path.exists(counted) else 0
        with open(counted, "w") as f:
            f.write(str(n + 1))
        cfg = cfg[min(n, len(cfg) - 1)]
    streamed = arg(argv, "--output-format") == "stream-json"
    if (arg(argv, "--input-format") == "stream-json") != streamed or arg(argv, "--output-format") not in (
            "json", "stream-json"):
        print("stand-in: only the streamed form and json output are stood in for", file=sys.stderr)
        return 2
    if streamed and "--verbose" not in argv:
        print("Error: When using --print, --output-format=stream-json requires --verbose", file=sys.stderr)
        return 1
    sid = arg(argv, "--session-id") or str(uuid.uuid4())
    key = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(os.getcwd()))
    tdir = os.path.join(os.path.expanduser("~"), ".claude", "projects", key)
    os.makedirs(tdir, exist_ok=True)
    transcript = open(os.path.join(tdir, f"{sid}.jsonl"), "a")
    if cfg.get("leave"):
        # a tool's command writes into Claude Code's own pipes, not its output
        child = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        with open(os.path.join(os.path.expanduser("~"), "left.pid"), "w") as f:
            f.write(str(child.pid))
    if cfg.get("stderr"):
        sys.stderr.write("x" * int(cfg["stderr"]))
        sys.stderr.flush()

    inbox: queue.Queue = queue.Queue()
    eof = threading.Event()
    last: list[dict] = []

    def read_stdin():
        if not streamed:
            inbox.put({"type": "user", "message": {"role": "user", "content": sys.stdin.read()}})
            eof.set()
            return
        for line in sys.stdin:
            if line.strip():
                inbox.put(json.loads(line))
                record({"type": "queue-operation", "operation": "enqueue", "timestamp": now(), "sessionId": sid})
        eof.set()

    def emit(ev):
        if ev.get("type") == "result":
            for field in [cfg["result_without"]] if cfg.get("result_without") else []:
                ev.pop(field, None)
            last[:] = [ev]
        if streamed:
            sys.stdout.write(json.dumps(ev) + "\n")
            sys.stdout.flush()

    def record(entry):
        transcript.write(json.dumps(entry) + "\n")
        transcript.flush()

    def texts(msg):
        content = msg["message"]["content"]
        return [content] if isinstance(content, str) else [b["text"] for b in content if b.get("type") == "text"]

    def waiting_now():
        """Every message waiting when a turn starts, as the one it takes."""
        msgs = []
        with contextlib.suppress(queue.Empty):
            while True:
                msgs.append(inbox.get_nowait())
        if len(msgs) < 2:
            return msgs[0] if msgs else None
        values = [m["message"]["content"] for m in msgs]
        merged = ("\n".join(values) if all(isinstance(v, str) for v in values) else
                  [b for v in values for b in ([{"type": "text", "text": v}] if isinstance(v, str) else v)])
        return {"type": "user", "message": {"role": "user", "content": merged}}

    if not streamed:
        # the last turn's result alone, once it exits
        atexit.register(lambda: last and print(json.dumps(last[-1]), flush=True))
    threading.Thread(target=read_stdin, daemon=True).start()
    time.sleep(cfg.get("startup_s", 0.3))
    while (first := waiting_now()) is None:
        if eof.is_set():
            return 0
        time.sleep(0.05)
    handed = []  # every message's text, in the order the session was handed them
    answered = 0  # how many of them a turn before answered
    cost = 0.0
    index = 0

    def take_queued():
        """What was written while the turn ran, handed at this step."""
        if cfg.get("notify") == "mid" and index == 0:
            record({"type": "attachment", "timestamp": now(), "sessionId": sid,
                    "attachment": {"type": "queued_command", "prompt": NOTICE,
                                   "commandMode": "task-notification", "timestamp": now()}})
        while True:
            try:
                msg = inbox.get_nowait()
            except queue.Empty:
                return
            for t in texts(msg):
                handed.append(t)
                record({"type": "attachment", "timestamp": now(), "sessionId": sid,
                        "attachment": {"type": "queued_command", "prompt": msg["message"]["content"],
                                       "commandMode": "prompt", "timestamp": now()},
                        "rendered": [{"content": [{"type": "text", "text": WRAPPER.format(text=t)}]}]})
            record({"type": "queue-operation", "operation": "remove", "timestamp": now(),
                    "sessionId": sid, "reason": "absorbed_mid_turn"})

    def turn(msg, steps, fail, notice=False):
        nonlocal cost, index, answered
        record({"type": "queue-operation", "operation": "dequeue", "timestamp": now(), "sessionId": sid})
        record({"type": "user", "message": {"role": "user", "content": msg["message"]["content"]},
                "promptSource": "sdk", "timestamp": now(), "sessionId": sid, "uuid": str(uuid.uuid4()),
                **({"origin": {"kind": "task-notification"}} if notice else {})})
        emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": os.getcwd(),
              "claude_code_version": "2.1.268", "uuid": str(uuid.uuid4())})
        if not notice:
            handed.extend(texts(msg))
        if (refuse := cfg.get("refuse")) and refuse["turn"] == ("later" if index else "first"):
            words = {"type": "text", "text": refuse["said"]}
            emit({"type": "assistant", "message": {"role": "assistant", "content": [words]},
                  "parent_tool_use_id": None, "session_id": sid, "uuid": str(uuid.uuid4()),
                  **({"error": refuse["error"]} if refuse.get("error") else {})})
            record({"type": "assistant", "message": {"role": "assistant", "content": [words]}, "timestamp": now(),
                    "sessionId": sid})
            emit({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "result": refuse["said"],
                  "session_id": sid, "total_cost_usd": round(cost, 4), "result_index": index,
                  "queued_turn_count": 0, "uuid": str(uuid.uuid4())})
            index += 1
            return
        if cfg.get("hang") in (True, "later" if index else "first"):
            time.sleep(3600)
        for n, secs in enumerate(steps):
            tid = f"toolu_{uuid.uuid4().hex[:24]}"
            use = {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": f"mem recall step{n}"}}
            emit({"type": "assistant", "message": {"role": "assistant", "content": [use]},
                  "parent_tool_use_id": None, "session_id": sid, "uuid": str(uuid.uuid4())})
            record({"type": "assistant", "message": {"role": "assistant", "content": [use]},
                    "timestamp": now(), "sessionId": sid})
            time.sleep(secs)
            out = "x" * int(cfg["big"]) if cfg.get("big") and n == 0 else f"(nothing for step{n})"
            res = {"tool_use_id": tid, "type": "tool_result", "content": out, "is_error": False}
            emit({"type": "user", "message": {"role": "user", "content": [res]}, "parent_tool_use_id": None,
                  "session_id": sid, "uuid": str(uuid.uuid4()), "timestamp": now(),
                  "tool_use_result": {"stdout": out, "stderr": "", "interrupted": False}})
            record({"type": "user", "message": {"role": "user", "content": [res]}, "timestamp": now(),
                    "sessionId": sid})
            take_queued()
            if n == 0 and cfg.get("event") and index == 0:
                emit(cfg["event"])
        time.sleep(cfg.get("reply_s", 0))
        if cfg.get("crash") == "later" and index:
            sys.stdout.flush()
            os._exit(1)
        index_now = index
        index += 1
        cost += 0.01
        covered = handed[answered:] if index_now > 0 and cfg.get("answer_new") else handed
        answered = len(handed)
        if fail:
            emit({"type": "result", "subtype": cfg.get("fail_subtype", "error_during_execution"), "is_error": True,
                  "num_turns": 1,
                  "session_id": sid, "total_cost_usd": round(cost, 4), "result_index": index_now,
                  "queued_turn_count": 0, "errors": ["stand-in failure"], "uuid": str(uuid.uuid4())})
            return
        if cfg.get("no_report") == ("later" if index_now else "first"):
            emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": len(steps) + 1,
                  "result": "I need the values for this tool call to proceed.", "session_id": sid,
                  "total_cost_usd": round(cost, 4), "result_index": index_now, "queued_turn_count": 0,
                  "stop_reason": "end_turn", "uuid": str(uuid.uuid4())})
            return
        quiet = notice or (index_now > 0 and cfg.get("silent_later"))
        report = {"recalled": [], "answer": "" if quiet else f"one answer to {len(covered)}: " + " | ".join(
            said(t) for t in covered), "recorded": []}
        record({"type": "attachment", "timestamp": now(), "sessionId": sid,
                "attachment": {"type": "structured_output", "data": report}})
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": len(steps) + 1,
              "result": json.dumps(report), "structured_output": report, "session_id": sid,
              "total_cost_usd": round(cost, 4), "result_index": index_now, "queued_turn_count": 0,
              "stop_reason": "tool_use", "uuid": str(uuid.uuid4())})

    turn(first, cfg.get("steps", [0.2]), cfg.get("fail") == "first")
    if cfg.get("notify") == "after":
        turn({"type": "user", "message": {"role": "user", "content": NOTICE}}, [], cfg.get("fail") == "later",
             notice=True)
    while True:
        # a message read after the turn's last step starts a further turn; at
        # the end of input whatever is still queued is run first
        if cfg.get("on_eof") == "drop" and eof.wait(0.5):
            return 0
        if (msg := waiting_now()) is None:
            if eof.is_set():
                return 0
            time.sleep(0.05)
            continue
        turn(msg, cfg.get("steps_later", []), cfg.get("fail") == "later")


if __name__ == "__main__":
    sys.exit(main())
