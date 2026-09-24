//! A projection of a session's native transcript: who said what, what wanda
//! did, what she answered.
//!
//! Claude Code keeps a transcript of every session it runs, as JSONL under
//! `~/.claude/projects/<cwd, slashes to dashes>/<session id>.jsonl`, and prunes
//! it after `cleanupPeriodDays` — thirty by default. That is the belt: what was
//! said, by day, unfiled, gone after a month. Nothing here writes a second copy
//! of it. A node in the vault carries `made: <session id>`, and this is what
//! turns that id back into the exchange.

use crate::text::{one_line, py_strip, take_chars};
use regex::Regex;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

/// Where Claude Code put this vault's transcripts. `MEM_TRANSCRIPTS` names the
/// directory outright, for when they were written under a different vault path.
pub fn project_dir(vault: &Path) -> PathBuf {
    if let Ok(e) = std::env::var("MEM_TRANSCRIPTS") {
        if !e.is_empty() {
            return PathBuf::from(e);
        }
    }
    let home = std::env::var("HOME").unwrap_or_default();
    let key = vault.canonicalize().unwrap_or_else(|_| vault.to_path_buf())
        .to_string_lossy().replace('/', "-");
    PathBuf::from(home).join(".claude").join("projects").join(key)
}

// the shape of an arriving prompt, as the harness writes it. The harness checks
// at startup that what it writes is what this reads, so the two cannot drift apart.
static PROMPT_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?s)^(?:You are wanda\.\s*\n\n)?Today is (\d{4}-\d{2}-\d{2})\.\s*\n\n(.*?)\n\nDo (?:two|three) things").unwrap());
static DM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?s)^(.+?) says to you, in a direct message:\n\n(.*)$").unwrap());
static EMAIL_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?s)^An email has arrived[^\n]*\n\n\s*From: (.+?)\n(.*)$").unwrap());
// the thread so far is rendered above the new message; only that new message is
// this exchange's input
static THREAD_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(concat!(
    r"(?s)^In a Slack thread that .+? read, wanda included\.[^\n]*\n\n",
    r"(?:The thread so far:\n\n.*?\n\n)?([^\n]+?) (?:now )?says:\n\n(.*)$")).unwrap());

/// (stated date, channel, speaker, text). A prompt not in this shape comes back
/// whole, undated, from nobody in particular.
pub fn parse_prompt(prompt: &str) -> (String, String, String, String) {
    let Some(m) = PROMPT_RE.captures(prompt) else {
        return (String::new(), String::new(), String::new(), py_strip(prompt).to_string());
    };
    let when = m[1].to_string();
    let arrival = m[2].to_string();
    for (chan, rx) in [("dm", &*DM_RE), ("email", &*EMAIL_RE), ("thread", &*THREAD_RE)] {
        if let Some(a) = rx.captures(&arrival) {
            let text: Vec<&str> = crate::text::split_lines(a.get(2).unwrap().as_str())
                .into_iter()
                .map(|l| if let Some(rest) = l.strip_prefix("    ") { rest } else { l })
                .collect();
            return (when, chan.to_string(), py_strip(&a[1]).to_string(),
                    py_strip(&text.join("\n")).to_string());
        }
    }
    (when, String::new(), String::new(), py_strip(&arrival).to_string())
}

#[derive(Clone, Debug)]
pub struct Turn {
    /// HH:MM:SS; the date it belongs to is the exchange's
    pub at: String,
    /// said · did · aside · answered
    pub kind: String,
    pub text: String,
    pub result: String,
}

#[derive(Clone, Debug, Default)]
pub struct Exchange {
    pub session: String,
    pub path: PathBuf,
    /// the date the session was given
    pub date: String,
    pub channel: String,
    pub speaker: String,
    pub text: String,
    pub answer: String,
    pub recalled: Vec<String>,
    pub recorded: Vec<String>,
    pub turns: Vec<Turn>,
    /// ISO timestamps, for ordering exchanges
    pub started: String,
    pub ended: String,
}

impl Exchange {
    pub fn actions(&self) -> Vec<&Turn> {
        self.turns.iter().filter(|t| t.kind == "did").collect()
    }

    /// Whether the session has given its answer, which can be an empty one.
    pub fn answered(&self) -> bool {
        !self.answer.is_empty() || self.turns.iter().any(|t| t.kind == "answered")
    }
}

static CLOCK_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"^\d{4}-\d{2}-\d{2}(?:[T ](\d{2}):(\d{2}):(\d{2}))?").unwrap());

/// The time of day the timestamp states, in the offset it states it in — the
/// Python formats the parsed value without converting it, so neither does this.
fn clock(ts: &str) -> String {
    match CLOCK_RE.captures(ts) {
        // a bare date parses, and formats as midnight
        Some(c) if c.get(1).is_none() => "00:00:00".into(),
        Some(c) => format!("{}:{}:{}", &c[1], &c[2], &c[3]),
        None => "--:--:--".into(),
    }
}

/// One tool call, by the argument that says what it did. Without the skill's
/// own name a log says "skill" twenty times and cannot tell one from another.
fn salient_keys(tool: &str) -> Option<&'static [&'static str]> {
    Some(match tool {
        "Read" | "Write" | "Edit" => &["file_path"],
        "Bash" => &["command"],
        "Glob" | "Grep" => &["pattern", "path"],
        "Skill" => &["skill", "args"],
        _ => return None,
    })
}

pub fn salient(tool: &str, inp: &Value) -> String {
    let Some(map) = inp.as_object() else { return py_str(inp) };
    match salient_keys(tool) {
        Some(keys) => keys.iter()
            .filter_map(|k| map.get(*k).map(str_of).filter(|s| !s.is_empty()))
            .collect::<Vec<_>>().join(" "),
        None => {
            let mut ks: Vec<&String> = map.keys().collect();
            ks.sort();
            ks.iter().map(|k| k.as_str()).collect::<Vec<_>>().join(",")
        }
    }
}

/// `str(v)` for a JSON value, as the Python would print it.
fn str_of(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_str(other),
    }
}

fn py_str(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True".into() } else { "False".into() },
        Value::String(s) => s.clone(),
        other => crate::text::py_json(other),
    }
}

/// A session reaches `mem` by whatever name the prompt gave it: the shim on
/// PATH, or the file named outright. The older spellings are for a transcript
/// written when it was a Python module.
static MEM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"\S*python3?\s+\S*mem\.pyc?\b|\S*/mem\.pyc?\b|\S*/mem\b").unwrap());

/// One line for a tool call. A mem invocation is the action itself, so it is
/// kept whole with the interpreter path collapsed to `mem`; a read is named by
/// what it read.
fn command(tool: &str, inp: &Value) -> String {
    let get = |k: &str| inp.get(k).map(str_of).unwrap_or_default();
    match tool {
        "Bash" => {
            let cmd = crate::text::split_lines(&get("command")).into_iter()
                .map(|l| py_strip(l)).filter(|l| !l.is_empty())
                .collect::<Vec<_>>().join(" ; ");
            MEM_RE.replace_all(&cmd, "mem").to_string()
        }
        "Read" => format!("read {}", get("file_path")),
        "Grep" | "Glob" => {
            let s = format!("{} {} {}", tool.to_lowercase(), get("pattern"), get("path"));
            s.trim_end().to_string()
        }
        "Skill" => format!("skill {}", get("skill")),
        _ => format!("{tool} {}", take_chars(&crate::text::py_json(inp), 120)),
    }
}

pub fn load(path: &Path) -> Exchange {
    let mut ex = Exchange {
        session: path.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default(),
        path: path.to_path_buf(),
        ..Default::default()
    };
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let mut results: std::collections::HashMap<String, String> = Default::default();
    let mut pending: Vec<(String, usize)> = Vec::new();

    for line in crate::text::split_lines(&text) {
        let Ok(d) = serde_json::from_str::<Value>(line) else { continue };
        let t = d.get("type").and_then(|x| x.as_str()).unwrap_or("");
        let ts = d.get("timestamp").and_then(|x| x.as_str()).unwrap_or("");
        if (t == "user" || t == "assistant") && !ts.is_empty() {
            if ex.started.is_empty() {
                ex.started = ts.to_string();
            }
            ex.ended = ts.to_string();
        }
        if t == "attachment" {
            let a = d.get("attachment").cloned().unwrap_or(Value::Null);
            if a.get("type").and_then(|x| x.as_str()) == Some("structured_output") {
                if let Some(data) = a.get("data").and_then(|x| x.as_object()) {
                    ex.answer = data.get("answer").map(str_of).unwrap_or_default();
                    let list = |k: &str| data.get(k).and_then(|x| x.as_array())
                        .map(|a| a.iter().map(str_of).collect()).unwrap_or_default();
                    ex.recalled = list("recalled");
                    ex.recorded = list("recorded");
                    ex.turns.push(Turn { at: clock(ts), kind: "answered".into(),
                                         text: ex.answer.clone(), result: String::new() });
                }
            }
            continue;
        }
        if (t != "user" && t != "assistant") || d.get("isMeta").and_then(|x| x.as_bool()) == Some(true) {
            continue;
        }
        let content = d.get("message").and_then(|m| m.get("content"));
        if t == "user" {
            if let Some(s) = content.and_then(|c| c.as_str()) {
                if ex.text.is_empty() && ex.speaker.is_empty() {
                    let (date, channel, speaker, text) = parse_prompt(s);
                    ex.date = date; ex.channel = channel; ex.speaker = speaker;
                    ex.text = text.clone();
                    ex.turns.push(Turn { at: clock(ts), kind: "said".into(),
                                         text, result: String::new() });
                }
                continue;
            }
        }
        let Some(blocks) = content.and_then(|c| c.as_array()) else { continue };
        for b in blocks {
            let bt = b.get("type").and_then(|x| x.as_str()).unwrap_or("");
            let name = b.get("name").and_then(|x| x.as_str()).unwrap_or("");
            if t == "assistant" && bt == "text" {
                let s = b.get("text").and_then(|x| x.as_str()).unwrap_or("");
                if !py_strip(s).is_empty() {
                    ex.turns.push(Turn { at: clock(ts), kind: "aside".into(),
                                         text: py_strip(s).to_string(), result: String::new() });
                }
            } else if t == "assistant" && bt == "tool_use" {
                if name == "StructuredOutput" {
                    // the attachment carries the same data; this is the
                    // fallback when it is absent, from a run that died first
                    if ex.answer.is_empty() {
                        if let Some(i) = b.get("input").and_then(|x| x.as_object()) {
                            ex.answer = i.get("answer").map(str_of).unwrap_or_default();
                        }
                    }
                    continue;
                }
                let inp = b.get("input").cloned().unwrap_or(Value::Object(Default::default()));
                ex.turns.push(Turn { at: clock(ts), kind: "did".into(),
                                     text: command(name, &inp), result: String::new() });
                pending.push((b.get("id").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                              ex.turns.len() - 1));
            } else if t == "user" && bt == "tool_result" {
                results.insert(
                    b.get("tool_use_id").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                    result_text(b.get("content")));
            }
        }
    }
    for (tid, idx) in pending {
        let text = ex.turns[idx].text.clone();
        if !text.contains("mem ") && !text.starts_with("mem") {
            continue;
        }
        // what mem said back is what the call made — `ok event:...` — and a
        // chained command has one such line per verb
        let said: Vec<String> = crate::text::split_lines(results.get(&tid).map(|s| s.as_str()).unwrap_or(""))
            .into_iter()
            .filter(|l| l.starts_with("ok") || l.starts_with('(') || l.starts_with("warning"))
            .map(|l| py_strip(l).to_string())
            .collect();
        ex.turns[idx].result = take_chars(&said.join(" | "), 240);
    }
    ex
}

fn result_text(c: Option<&Value>) -> String {
    match c {
        Some(Value::Array(a)) => a.iter()
            .filter_map(|x| x.get("text").and_then(|t| t.as_str()))
            .collect::<Vec<_>>().join(" "),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => py_str(other),
    }
}

/// What a session sees: the exchange's own date, and times of day only — a
/// timestamp's date is never rendered, whatever day the reader is on.
/// `in_progress` marks the caller's own exchange while it has not answered.
pub fn render(ex: &Exchange, full: bool, in_progress: bool) -> String {
    let mut head = format!("session {}", ex.session);
    if !ex.date.is_empty() {
        head += &format!(" \u{b7} {}", ex.date);
    }
    if !ex.channel.is_empty() {
        head += &format!(" \u{b7} {}", ex.channel);
    }
    head += &format!(" \u{b7} {} actions", ex.actions().len());
    if in_progress {
        head += " \u{b7} this session, in progress";
    }
    let mut out = vec![head, String::new()];
    let who = if ex.speaker.is_empty() { "someone" } else { &ex.speaker };
    for t in &ex.turns {
        match t.kind.as_str() {
            "said" => out.push(format!("{}  {who} said: {}", t.at, t.text)),
            "did" => {
                out.push(format!("{}  wanda ran: {}", t.at,
                    if full { t.text.clone() } else { take_chars(&t.text, 300) }));
                if !t.result.is_empty() {
                    out.push(format!("          \u{2192} {}", t.result));
                }
            }
            "aside" => out.push(format!("{}  wanda (aside): {}", t.at,
                if full { t.text.clone() } else { take_chars(&t.text, 200) })),
            "answered" => out.push(format!("{}  wanda said: {}", t.at,
                if t.text.is_empty() { "(nothing)" } else { &t.text })),
            _ => {}
        }
    }
    if !ex.turns.iter().any(|t| t.kind == "answered") {
        out.push(format!("          wanda said: {}", if !ex.answer.is_empty() {
            &ex.answer
        } else if in_progress {
            "(nothing yet \u{2014} this session is in progress)"
        } else {
            "(nothing \u{2014} the session did not answer)"
        }));
    }
    out.join("\n")
}

/// One line for a listing.
pub fn line(ex: &Exchange) -> String {
    let said = take_chars(&one_line(&ex.text), 70);
    let ans = take_chars(&one_line(&ex.answer), 70);
    let ans = if ans.is_empty() { "(silent)".to_string() } else { ans };
    format!("{}  {}  {}: {said}\n          wanda: {ans}",
        take_chars(&ex.session, 8),
        if ex.date.is_empty() { "----------" } else { &ex.date },
        if ex.speaker.is_empty() { "?" } else { &ex.speaker })
}

/// A session id, or an unambiguous prefix of one.
pub fn find(vault: &Path, r: &str) -> Option<PathBuf> {
    let d = project_dir(vault);
    if !d.exists() {
        return None;
    }
    let exact = d.join(format!("{r}.jsonl"));
    if exact.exists() {
        return Some(exact);
    }
    let mut hits: Vec<PathBuf> = std::fs::read_dir(&d).ok()?.flatten()
        .map(|e| e.path())
        .filter(|p| {
            let n = p.file_name().map(|x| x.to_string_lossy().to_string()).unwrap_or_default();
            n.starts_with(r) && n.ends_with(".jsonl")
        })
        .collect();
    hits.sort();
    if hits.len() == 1 { hits.pop() } else { None }
}

/// Every exchange this vault has had that the transcripts still hold, oldest
/// first by when they ran.
pub fn load_all(vault: &Path) -> Vec<Exchange> {
    let d = project_dir(vault);
    let Ok(rd) = std::fs::read_dir(&d) else { return Vec::new() };
    let mut out: Vec<Exchange> = rd.flatten().map(|e| e.path())
        .filter(|p| p.extension().map(|x| x == "jsonl").unwrap_or(false))
        .map(|p| load(&p)).collect();
    out.sort_by(|a, b| a.started.cmp(&b.started));
    out
}

/// Every tool call in one transcript, with what came back and whether it
/// failed. Separate from `load`, which renders the exchange for a session to
/// read and keeps only what `mem` said; a log of what a session did wants the
/// calls it got refused as much as the ones that worked, and the whole argument
/// rather than a line of it.
pub fn tool_calls(path: &Path) -> Vec<serde_json::Map<String, Value>> {
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let mut calls: std::collections::HashMap<String, (String, String, String)> = Default::default();
    let mut results: std::collections::HashMap<String, (bool, String)> = Default::default();
    let mut order: Vec<String> = Vec::new();
    for line in crate::text::split_lines(&text) {
        let Ok(d) = serde_json::from_str::<Value>(line) else { continue };
        let Some(blocks) = d.get("message").and_then(|m| m.get("content"))
            .and_then(|c| c.as_array()) else { continue };
        let ts = d.get("timestamp").and_then(|x| x.as_str()).unwrap_or("");
        for b in blocks {
            let bt = b.get("type").and_then(|x| x.as_str()).unwrap_or("");
            let name = b.get("name").and_then(|x| x.as_str()).unwrap_or("");
            if bt == "tool_use" && name != "StructuredOutput" {
                let id = b.get("id").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let inp = b.get("input").cloned().unwrap_or(Value::Object(Default::default()));
                calls.insert(id.clone(), (name.to_string(), salient(name, &inp), clock(ts)));
                order.push(id);
            } else if bt == "tool_result" {
                let id = b.get("tool_use_id").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let failed = b.get("is_error").and_then(|x| x.as_bool()).unwrap_or(false);
                results.insert(id, (failed, result_text(b.get("content"))));
            }
        }
    }
    order.iter().filter_map(|cid| {
        let (tool, arg, at) = calls.get(cid)?;
        let (failed, text) = results.get(cid).cloned().unwrap_or((false, String::new()));
        let mut m = serde_json::Map::new();
        m.insert("at".into(), at.clone().into());
        m.insert("tool".into(), tool.clone().into());
        m.insert("arg".into(), arg.clone().into());
        m.insert("failed".into(), failed.into());
        m.insert("result".into(), take_chars(&one_line(&text), 300).into());
        Some(m)
    }).collect()
}
