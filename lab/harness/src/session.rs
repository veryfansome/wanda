//! What a session is set up with, how it is run, and what is read back out.

use crate::arrival::{arrival_text, prompt_for, Input};
use memory::text::py_repr;
use memory::vault::Vault;
use memory::{index, transcript};
use serde_json::{json, Value};
use std::io::Write;
use std::path::{Path, PathBuf};

/// A session that did the work and then filled the schema with scaffolding
/// instead of reporting it, which reads as a recall failure and is not one.
/// Matched whole-field and normalised, never as a substring and never on
/// length: a real answer that happens to be short is still an answer.
const PLACEHOLDER: [&str; 12] = [
    "test", "test entry", "testing", "todo", "tbd", "placeholder",
    "example", "sample", "n a", "answer here", "your answer", "dummy",
];

fn normalised(s: &str) -> String {
    let cleaned: String = s.to_lowercase().chars()
        .map(|c| if c.is_ascii_lowercase() || c.is_ascii_digit() || c == ' ' { c } else { ' ' })
        .collect();
    memory::text::one_line(&cleaned)
}

/// Which schema fields came back as scaffolding rather than as a report.
pub fn placeholder_fields(out: &Value) -> Vec<String> {
    let mut bad = Vec::new();
    for k in ["answer", "recalled", "recorded"] {
        let values: Vec<String> = match out.get(k) {
            Some(Value::String(s)) => vec![s.clone()],
            Some(Value::Array(a)) => a.iter().map(|x| match x {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            }).collect(),
            _ => vec![],
        };
        if values.iter().any(|v| PLACEHOLDER.contains(&normalised(v).as_str())) {
            bad.push(k.to_string());
        }
    }
    bad
}

/// The shape a session reports in.
pub fn schema() -> Value {
    json!({
        "type": "object",
        "additionalProperties": false,
        "required": ["recalled", "answer", "recorded"],
        "properties": {
            "recalled": {
                "type": "array",
                "description": "what you brought to bear on this, most relevant first, as node ids or names",
                "items": {"type": "string"},
            },
            "answer": {
                "type": "string",
                "description": "what you would say back, empty if you would say nothing",
            },
            "recorded": {
                "type": "array",
                "description": "one line per thing you wrote to memory",
                "items": {"type": "string"},
            },
        },
    })
}

/// The skills a session finds in its vault. Extracted so every entry point
/// sets up the same session rather than keeping its own copy: a session that
/// links differently is not comparable with the rest.
///
/// A skill here is a real Claude Code skill, discovered from the vault's own
/// `.claude/skills/` and invoked with the Skill tool. Without Skill in the tool
/// list a session sees only the user's plugin skills and reports that enrich
/// does not exist. wanda is a Claude Code session, and CLAUDE.md and skills are
/// how she is set up.
pub fn write_session_config(vault_path: &Path) {
    let _ = std::fs::create_dir_all(vault_path.join(".claude"));
    for name in ["enrich", "retract"] {
        let skill = vault_path.join(".claude").join("skills").join(name);
        let _ = std::fs::create_dir_all(&skill);
        let _ = std::fs::write(skill.join("SKILL.md"), index::template(name));
    }
}

/// The snapshot as a working tree, somewhere that can be written to freely.
/// Never the original vault: this writes, and the store it starts from has to
/// be identical every time.
///
/// The snapshot carries the generated CLAUDE.md files as the sessions of that
/// run saw them, and by default they are restored untouched — reading a past
/// run back has to show the session the store it actually met. `regenerate`
/// rebuilds them from the node files instead, which is what you want when the
/// index format itself is what changed: same store, new landing surface.
pub fn materialise(snaps: &Path, sha: &str, dest: &Path, regenerate: bool, for_session: bool)
    -> std::io::Result<()>
{
    let _ = std::fs::remove_dir_all(dest);
    std::fs::create_dir_all(dest)?;
    let tar = std::process::Command::new("git")
        .args(["archive", sha])
        .env("GIT_DIR", snaps)
        .output()?;
    let mut child = std::process::Command::new("tar")
        .args(["-x", "-C"]).arg(dest)
        .stdin(std::process::Stdio::piped())
        .spawn()?;
    {
        use std::io::Write;
        child.stdin.as_mut().expect("piped").write_all(&tar.stdout)?;
    }
    child.wait()?;
    // its own repo, or the session inherits this project's CLAUDE.md and memory
    std::process::Command::new("git").args(["init", "-q"]).current_dir(dest).status()?;
    if regenerate {
        let _ = index::regenerate_indexes(&Vault::new(dest));
    }
    if for_session {
        write_session_config(dest);
    }
    Ok(())
}

/// Every tool call the session made, from its own transcript.
///
/// A PostToolUse hook only fires for a call that succeeded, so a log collected
/// that way cannot see an attempt that was refused — which is the one thing a
/// log of what a session tried to do exists to show. The transcript holds the
/// attempt and what came back, and holds the whole argument rather than its
/// first four hundred characters.
pub fn write_trace(toollog: &Path, key: &str, sid: &str, vault: &Path) {
    let p = transcript::project_dir(vault).join(format!("{sid}.jsonl"));
    if !p.exists() {
        return;
    }
    let Ok(mut fh) = std::fs::OpenOptions::new().create(true).append(true).open(toollog) else {
        return;
    };
    use std::io::Write;
    for c in transcript::tool_calls(&p) {
        let rec = json!({
            "ts": c.get("at"), "input_key": key, "tool": c.get("tool"),
            "arg": c.get("arg"), "failed": c.get("failed"),
            "session": memory::text::take_chars(sid, 8), "result": c.get("result"),
        });
        let _ = writeln!(fh, "{}", memory::text::py_json_utf8(&rec));
    }
}

pub fn read_trace(toollog: &Path, key: &str) -> Vec<Value> {
    let Ok(text) = std::fs::read_to_string(toollog) else { return Vec::new() };
    memory::text::split_lines(&text).into_iter()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .filter(|d| d.get("input_key").and_then(|x| x.as_str()) == Some(key))
        .collect()
}

const READS: [&str; 4] = ["recall", "search", "show", "help"];

/// How much work the session did, and how much of it was the store.
///
/// The mem counts come from mem's own log rather than from matching a verb in
/// the tool log: sessions chain several calls into one Bash command, so
/// everything after the first verb would be invisible.
///
/// Nothing here counts what the session was shown. The tool log clips a result
/// at 300 characters and a command at 400, so it cannot say whether a node or
/// an index arrived whole, or at all — `lab/reach.py` reads the transcripts and
/// answers that per session, after the run.
pub fn summarise_trace(trace: &[Value], mem: &[Value]) -> String {
    let s = |v: &Value, k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
    // insertion-ordered, and printed as Python prints a dict
    let mut tools: indexmap::IndexMap<String, i64> = indexmap::IndexMap::new();
    for t in trace {
        *tools.entry(s(t, "tool")).or_insert(0) += 1;
    }
    let shown = format!("{{{}}}", tools.iter()
        .map(|(k, v)| format!("{}: {v}", py_repr(k)))
        .collect::<Vec<_>>().join(", "));
    let is_read = |m: &Value| READS.contains(&s(m, "cmd").as_str());
    let reads = mem.iter().filter(|m| is_read(m)).count();
    let writes = mem.len() - reads;
    let empty = mem.iter()
        .filter(|m| is_read(m) && m.get("rc").and_then(|x| x.as_i64()).unwrap_or(0) != 0)
        .count();
    format!("{} calls {shown} \u{b7} mem reads {reads} ({empty} empty) \
             \u{b7} mem writes {writes}",
            trace.len())
}

/// A path as a single shell word. There is no escape for `'` inside `'…'`, so
/// a quote closes, escapes itself, and reopens — the only POSIX spelling.
fn sh_quote(p: &Path) -> String {
    format!("'{}'", p.display().to_string().replace('\'', r"'\''"))
}

/// `mem`, on the session's PATH, and the name the prompt gives it.
///
/// An absolute path in front of every call invites a session to go and read
/// the tool instead of using it. The shim goes where the image already points
/// PATH, not beside the store, so it is not a file in the session's own
/// directory.
pub fn install_mem(mem: &Path) -> String {
    for d in std::env::var("PATH").unwrap_or_default().split(':') {
        let bin_dir = Path::new(d);
        if !bin_dir.is_dir() {
            continue;
        }
        let shim = bin_dir.join("mem");
        // one word, whatever the path holds: a lab directory with a space in
        // it would otherwise make `exec` try the first half of it, and every
        // session in that run would be unable to call `mem` at all
        if std::fs::write(&shim, format!("#!/bin/sh\nexec {} \"$@\"\n", sh_quote(mem))).is_err() {
            continue;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&shim, std::fs::Permissions::from_mode(0o755));
        }
        return "mem".into();
    }
    // nowhere on PATH is writable: name the file. Quoted, because this is the
    // name the prompt gives the tool and a session types what it is told.
    sh_quote(mem)
}

pub const MODEL: &str = "claude-sonnet-5";

/// A dict as Python prints one, which is what the report already carries.
/// Absolute, with symlinks followed. Both paths can sit under one — on macOS
/// `/var` is a link to `/private/var` — and Claude Code files a session's
/// transcripts under the followed path, so a vault path resolved only halfway
/// sends the auto-memory check looking somewhere nothing was ever written.
/// canonicalize alone cannot do it: neither path exists yet.
pub fn resolve(p: &Path) -> Result<PathBuf, String> {
    let abs = std::path::absolute(p).map_err(|e| e.to_string())?;
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    let mut head = abs.as_path();
    loop {
        if let Ok(real) = head.canonicalize() {
            let mut out = real;
            out.extend(tail.iter().rev());
            return Ok(out);
        }
        match (head.parent(), head.file_name()) {
            (Some(par), Some(name)) => {
                tail.push(name.to_os_string());
                head = par;
            }
            _ => return Ok(abs),
        }
    }
}

/// One exchange. Returns what the session reported, a one-line summary, and
/// the session id — which this process chooses, so the id is known before the
/// session runs and every node it writes is stamped with it.
#[allow(clippy::too_many_arguments)]
pub fn run_session(vault: &Path, inp: &Input, mem_cmd: &str, timeout_s: u64, key: &str,
               memlog: &Path, members: &[String], history: &[(String, String)])
    -> Result<(Value, String, String), String>
{
    let arrival = arrival_text(inp, members, history);
    let prompt = prompt_for(&inp.date, &arrival, mem_cmd);
    let sid = uuid::Uuid::new_v4().to_string();
    let date = &inp.date;
    // the date this process was given only takes hold from the system prompt.
    // Set in the user prompt it loses to the environment date Claude Code
    // injects, and elapsed-time judgements come out months off.
    let system = format!(
        "Today is {date}. Any other date you are shown is the machine's, \
         not yours — the date in your system prompt, the date the shell reports, \
         the timestamps on files. Every judgement about the date, about how long \
         ago something happened, and about what is overdue uses {date} as now. \
         Everything you know about these people comes from this history and \
         from the vault you are working in. Your surroundings are not part of \
         it: the machine, files outside the vault, the shell environment, the \
         git repository and whatever account this session is signed in as tell \
         you nothing about anyone here, and none of it belongs in memory. \
         Working out what was meant, and what it implies, from what was \
         actually said is exactly your job.");

    let mut cmd = std::process::Command::new("claude");
    cmd.args(["-p", "--output-format", "json", "--model", MODEL,
              "--json-schema", &memory::text::py_json(&schema()),
              // the transcript Claude Code writes under this id is the belt:
              // what was said both ways and every mem call, kept for a month.
              // A node the session writes carries the id as `made:`.
              "--session-id", &sid,
              "--append-system-prompt", &system,
              // a runaway guard, not a cost control: several times what a long
              // session spends, so only a genuine loop trips it.
              "--max-budget-usd", "2.50",
              "--tools", "Read,Glob,Grep,Bash,Skill",
              // both flags: --tools exposes them, --allowedTools grants them.
              // Without the second, Bash is refused outright under dontAsk.
              "--allowedTools", "Read,Glob,Grep,Bash,Skill",
              "--permission-mode", "dontAsk",
              "--setting-sources", "project"])
        .current_dir(vault)
        // Claude Code instructs every session, headless included, to keep a
        // memory of its own in a directory that is not the vault
        .env("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
        .env("MEM_VAULT", vault)
        .env("MEM_DATE", date)
        .env("MEM_SESSION", &sid)
        .env("LAB_INPUT", key)
        .env("LAB_MEMLOG", memlog)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let mut child = cmd.spawn().map_err(|e| format!("claude did not start: {e}"))?;
    child.stdin.as_mut().expect("piped").write_all(prompt.as_bytes())
        .map_err(|e| e.to_string())?;
    drop(child.stdin.take());
    let p = wait_with_timeout(child, timeout_s)?;
    if !p.status.success() {
        // claude -p can exit non-zero with nothing on stderr after the session
        // has done all its work, with whatever it did say on stdout. Keep
        // both, and never raise an empty message.
        let err = memory::text::py_strip(
            &memory::text::take_chars(&String::from_utf8_lossy(&p.stderr), 300)).to_string();
        let err = if err.is_empty() {
            format!("exit {}, stderr empty", p.status.code().unwrap_or(-1))
        } else { err };
        let tail = String::from_utf8_lossy(&p.stdout);
        let tail: String = tail.chars().rev().take(300).collect::<Vec<_>>()
            .into_iter().rev().collect();
        let tail = memory::text::py_strip(&tail).to_string();
        return Err(if tail.is_empty() { err } else { format!("{err} | stdout tail: {tail}") });
    }
    // the wording of the parse failure is this parser's, not CPython's. It is
    // recorded and shown, and nothing reads it back.
    let env: Value = serde_json::from_slice(&p.stdout)
        .map_err(|e| format!("claude returned something that is not json: {e}"))?;
    let body = match env.get("result") {
        Some(Value::String(s)) => serde_json::from_str(s).unwrap_or_else(|_| json!({})),
        Some(other) => other.clone(),
        None => json!({}),
    };
    let meta = format!("turns={} cost={}",
        env.get("num_turns").map(|v| v.to_string()).unwrap_or("None".into()),
        env.get("total_cost_usd").map(|v| v.to_string()).unwrap_or("None".into()));
    Ok((body, meta, sid))
}

fn wait_with_timeout(mut child: std::process::Child, secs: u64)
    -> Result<std::process::Output, String>
{
    let start = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return child.wait_with_output().map_err(|e| e.to_string()),
            Ok(None) => {
                if start.elapsed().as_secs() >= secs {
                    let _ = child.kill();
                    return Err(format!("timed out after {secs}s"));
                }
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
            Err(e) => return Err(e.to_string()),
        }
    }
}
