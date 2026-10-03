//! A projection of a session's native transcript: who said what, what wanda
//! did, what she answered — rendered for her, so in her voice.
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

// the shape of an arriving prompt, as the harness writes it now and as it wrote
// it for transcripts still on disk. The harness checks at startup that this
// reads every one of those, so the two cannot drift apart.
static PROMPT_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?s)^(?:(?:You are|I am) wanda\.\s*\n\n)?Today is (\d{4}-\d{2}-\d{2})\.\s*\n\n(.*?)\n\n(?:Do (?:two|three)|I do three) things").unwrap());
static DM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?s)^(.+?) says to (?:you|me), in a direct message:\n\n(.*)$").unwrap());
static EMAIL_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?s)^An email has arrived[^\n]*\n\n\s*From: (.+?)\n(.*)$").unwrap());
// the thread so far is rendered above the new message; only that new message is
// this exchange's input. The product gives a direct message with messages
// before it, a group direct message and a channel the same shape, naming the
// place, since who could read an exchange is part of what it was; a public
// channel, and a thread in one, say that anyone in the Slack can read them.
// A product turn that took messages from several people names the others
// after the speaker.
static THREAD_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(concat!(
    r"(?s)^In (?P<place>a Slack thread in a public channel|a public Slack channel|a Slack thread",
    r"|a Slack channel|a direct message|a group direct message) ",
    r"that (?:anyone in this Slack can read; .+? and I are in it|.+? (?:read, wanda included|and I read))",
    r"\.[^\n]*\n\n",
    r"(?:The (?:thread|conversation) so far:\n\n.*?\n\n)?",
    r"(?P<speaker>[^\n]+?) (?:now )?says(?:, after (?P<also>[^\n]+?))?:\n\n(?P<text>.*)$")).unwrap());
// a session no message started: nobody is speaking, so the name is who she
// speaks to, and the indented lines are what woke her
static UNPROMPTED_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?s)^No message started this session\. What I say now reaches (.+?) alone, in a direct message\.\n\n(.*)$").unwrap());
// a session no message started whose answer reaches no one: the product's own
// news for her memory. Nobody speaks and nobody hears, so the speaker group is
// empty; without it, the first group, the text, would be taken for the speaker
static NOBODY_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"(?s)^No message started this session\. What I say now reaches no one\.(?P<speaker>)\n\n(?P<text>.*)$").unwrap());

// a message added to the conversation while its session worked, as the product
// hands it to that session (wanda/vault.py, ADDED). Every line of the message
// is indented, so the closing sentence, which is not, ends it.
static ADDED_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(concat!(
    r"(?s)^(?P<speaker>[^\n]+?) adds this in the same [^\n]+? at [^\n]+?, ",
    r"before anything I say back has been sent:\n\n(?P<text>.*?)\n\n",
    r"Nothing I have said back in this session has been sent yet")).unwrap());

/// Who added a message, and what it said; None for a text not in the
/// product's shape, which Claude Code's own prompts and continuations are.
fn parse_added(s: &str) -> Option<(String, String)> {
    let a = ADDED_RE.captures(s)?;
    let text: Vec<&str> = crate::text::split_lines(&a["text"]).into_iter()
        .map(|l| l.strip_prefix("    ").unwrap_or(l)).collect();
    Some((py_strip(&a["speaker"]).to_string(), py_strip(&text.join("\n")).to_string()))
}

/// (stated date, channel, speaker, text). A prompt not in this shape comes back
/// whole, undated, from nobody in particular.
pub fn parse_prompt(prompt: &str) -> (String, String, String, String) {
    let Some(m) = PROMPT_RE.captures(prompt) else {
        return (String::new(), String::new(), String::new(), py_strip(prompt).to_string());
    };
    let when = m[1].to_string();
    let arrival = m[2].to_string();
    for (chan, rx) in [("nobody", &*NOBODY_RE), ("dm", &*DM_RE), ("email", &*EMAIL_RE),
                       ("thread", &*THREAD_RE), ("clock", &*UNPROMPTED_RE)] {
        if let Some(a) = rx.captures(&arrival) {
            let said = a.name("text").unwrap_or_else(|| a.get(2).unwrap());
            let text: Vec<&str> = crate::text::split_lines(said.as_str())
                .into_iter()
                .map(|l| if let Some(rest) = l.strip_prefix("    ") { rest } else { l })
                .collect();
            let chan = match a.name("place").map(|p| p.as_str()) {
                Some("a direct message") => "dm",
                Some("a group direct message") => "group dm",
                Some("a Slack channel") => "channel",
                Some("a public Slack channel") => "public channel",
                Some("a Slack thread in a public channel") => "public thread",
                _ => chan,
            };
            let speaker = py_strip(a.name("speaker").unwrap_or_else(|| a.get(1).unwrap()).as_str());
            // whoever else's message the turn took is named with the speaker,
            // so the exchange is one person's only when its turn was
            let speaker = match a.name("also") {
                Some(also) => format!("{speaker} (after {})", py_strip(also.as_str())),
                None => speaker.to_string(),
            };
            return (when, chan.to_string(), speaker, py_strip(&text.join("\n")).to_string());
        }
    }
    (when, String::new(), String::new(), py_strip(&arrival).to_string())
}

#[derive(Clone, Debug)]
pub struct Turn {
    /// HH:MM:SS; the date it belongs to is the exchange's
    pub at: String,
    /// said · added · did · aside · answered
    pub kind: String,
    pub text: String,
    pub result: String,
    /// who added it, for a message added while the session worked
    pub who: String,
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

    /// Whether the session has given its answer, which can be an empty one,
    /// since the last message it was handed.
    pub fn answered(&self) -> bool {
        match self.turns.iter().rposition(|t| t.kind == "added") {
            Some(a) => self.turns[a..].iter().any(|t| t.kind == "answered"),
            None => !self.answer.is_empty() || self.turns.iter().any(|t| t.kind == "answered"),
        }
    }
}

static CLOCK_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(
    r"^\d{4}-\d{2}-\d{2}(?:[T ](\d{2}):(\d{2}):(\d{2}))?").unwrap());

/// The time of day the timestamp states. Claude Code stamps in UTC, and with
/// `MEM_UTC_OFFSET` set a UTC stamp is shown in the household's own time, the
/// time its sessions are told it is; any other stamp keeps the offset it states.
fn clock(ts: &str) -> String {
    match CLOCK_RE.captures(ts) {
        // a bare date parses, and formats as midnight
        Some(c) if c.get(1).is_none() => "00:00:00".into(),
        Some(c) => {
            let part = |i: usize| c[i].parse::<i64>().unwrap_or(0);
            let mut t = part(1) * 3600 + part(2) * 60 + part(3);
            if ts.ends_with('Z') || ts.ends_with("+00:00") {
                t += std::env::var("MEM_UTC_OFFSET").ok()
                    .and_then(|s| s.parse::<i64>().ok()).unwrap_or(0);
            }
            let t = t.rem_euclid(86_400);
            format!("{:02}:{:02}:{:02}", t / 3600, t % 3600 / 60, t % 60)
        }
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
    let mut opened = false;

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
            // a message added while a turn ran, handed to it at its next step
            // in the product's frame; Claude Code hands a background command's
            // notice of its end the same way, in a mode of its own, its own
            // notes as meta, and prompts of its own (a plugin's, `/goal`'s, an
            // agent team's) in this mode too
            if a.get("type").and_then(|x| x.as_str()) == Some("queued_command")
                && a.get("commandMode").and_then(|x| x.as_str()) == Some("prompt")
                && a.get("isMeta").and_then(|x| x.as_bool()) != Some(true) {
                for said in message_texts(a.get("prompt")) {
                    added(&mut ex, &said, ts);
                }
            }
            if a.get("type").and_then(|x| x.as_str()) == Some("structured_output") {
                if let Some(data) = a.get("data").and_then(|x| x.as_object()) {
                    answer(&mut ex, data, ts);
                }
            }
            continue;
        }
        if (t != "user" && t != "assistant") || d.get("isMeta").and_then(|x| x.as_bool()) == Some(true) {
            continue;
        }
        let content = d.get("message").and_then(|m| m.get("content"));
        if t == "user" {
            let said = message_texts(content);
            if !said.is_empty() {
                if notice(&d, &said) {
                    continue;
                }
                let mut later = said.iter();
                if !opened {
                    // the prompt, a string as the lab writes it, or text
                    // blocks as a session with open input is handed it; a
                    // block after it is a message Claude Code took into the
                    // same turn
                    opened = true;
                    let (date, channel, speaker, text) = parse_prompt(later.next().unwrap());
                    ex.date = date; ex.channel = channel; ex.speaker = speaker;
                    ex.text = text.clone();
                    ex.turns.push(Turn { at: clock(ts), kind: "said".into(),
                                         text, result: String::new(), who: String::new() });
                }
                // a message that began a later turn of the session, or one
                // taken into a turn with another; Claude Code begins turns
                // with words of its own too, which are no one's
                for s in later {
                    added(&mut ex, s, ts);
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
                                         text: py_strip(s).to_string(), result: String::new(), who: String::new() });
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
                                     text: command(name, &inp), result: String::new(), who: String::new() });
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
    // whoever added a message is named with the speaker, so the exchange is
    // one person's only when one person spoke in it
    let named = speakers(&ex.speaker);
    let mut others: Vec<String> = ex.turns.iter()
        .filter(|t| t.kind == "added" && !t.who.is_empty() && !named.contains(&t.who))
        .map(|t| t.who.clone()).collect();
    others.sort();
    others.dedup();
    if !ex.speaker.is_empty() && !others.is_empty() {
        ex.speaker = format!("{} (then {})", ex.speaker, others.join(" and "));
    }
    ex
}

/// The words of a message a session was handed: a string, or text blocks
/// alone; a tool's result is not one.
fn message_texts(c: Option<&Value>) -> Vec<String> {
    match c {
        Some(Value::String(s)) => vec![s.clone()],
        Some(Value::Array(a)) if !a.is_empty() && a.iter()
            .all(|b| b.get("type").and_then(|x| x.as_str()) == Some("text")) =>
            a.iter().map(|b| b.get("text").map(str_of).unwrap_or_default()).collect(),
        _ => Vec::new(),
    }
}

/// A turn Claude Code began with a background command's notice of its end,
/// not with a message.
fn notice(d: &Value, said: &[String]) -> bool {
    d.get("origin").and_then(|o| o.get("kind")).and_then(|k| k.as_str()) == Some("task-notification")
        || said[0].trim_start().starts_with("<task-notification>")
}

/// A message added after the opening one, if it is in the product's frame, at
/// the time Claude Code records for it: for one taken in mid-turn, when it
/// was queued; for one in a turn's opening message, when that turn began.
fn added(ex: &mut Exchange, said: &str, ts: &str) {
    if let Some((who, text)) = parse_added(said) {
        ex.turns.push(Turn { at: clock(ts), kind: "added".into(), text, result: String::new(), who });
    }
}

/// An answer the session gave. The exchange's answer is the last that says
/// something, which is the one the product posts, a clock session's too, and
/// the one a lab session gives: an answer before it was never sent, and is
/// kept as an aside, which nobody was shown either; an empty one after it
/// changes nothing, and an empty one before it is not shown. The one exception
/// is a later answer that is a placeholder the product drops: the one before
/// it was sent.
fn answer(ex: &mut Exchange, data: &serde_json::Map<String, Value>, ts: &str) {
    let said = data.get("answer").map(str_of).unwrap_or_default();
    let says = |t: &Turn| t.kind == "answered" && !py_strip(&t.text).is_empty();
    if py_strip(&said).is_empty() && ex.turns.iter().any(says) {
        return;
    }
    ex.turns.retain(|t| t.kind != "answered" || says(t));
    for t in ex.turns.iter_mut().filter(|t| t.kind == "answered") {
        t.kind = "aside".into();
    }
    let list = |k: &str| data.get(k).and_then(|x| x.as_array())
        .map(|a| a.iter().map(str_of).collect()).unwrap_or_default();
    ex.recalled = list("recalled");
    ex.recorded = list("recorded");
    ex.answer = said;
    ex.turns.push(Turn { at: clock(ts), kind: "answered".into(),
                         text: ex.answer.clone(), result: String::new(), who: String::new() });
}

/// The people a speaker names: one, or one after others ("{speaker} (after
/// {a} and {b})", a turn that took several people's messages).
fn speakers(speaker: &str) -> Vec<String> {
    match speaker.split_once(" (after ") {
        Some((first, rest)) => std::iter::once(first)
            .chain(rest.trim_end_matches(')').split(" and "))
            .map(|s| s.to_string()).collect(),
        None => vec![speaker.to_string()],
    }
}

/// Whether one person alone spoke in an exchange. A turn that took several
/// people's messages names them all in its speaker, and so does a session
/// handed another person's message while it worked.
pub fn one_speaker(ex: &Exchange) -> bool {
    !ex.speaker.is_empty() && !ex.speaker.contains(" (after ") && !ex.speaker.contains(" (then ")
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

/// The label on a prompt that did not parse, which is shown whole. A prompt
/// opens in her own voice, so under "someone said" it would read as another
/// person claiming to be her.
const UNPARSED: &str = "the opening message";

/// Who an exchange was with, as its opening line names them. In a clock
/// exchange nobody spoke, and under "said" the person she spoke to would read
/// as having started it; in one that reached no one, nobody spoke or heard.
/// Whoever added a message later is shown by it, not as having opened the
/// exchange.
fn opened_by(ex: &Exchange) -> String {
    match (ex.speaker.is_empty(), ex.date.is_empty()) {
        _ if ex.channel == "nobody" => "unprompted, to no one".to_string(),
        (false, _) if ex.channel == "clock" => format!("unprompted, to {}", ex.speaker),
        (false, _) => format!("{} said", ex.speaker.split(" (then ").next().unwrap_or(&ex.speaker)),
        (true, true) => UNPARSED.to_string(),
        (true, false) => "someone said".to_string(),
    }
}

/// What follows "I said" and "me". What she said in a session that reached no
/// one was posted nowhere, and a later session should not read it as said to
/// anyone.
fn to_whom(ex: &Exchange) -> &'static str {
    if ex.channel == "nobody" { ", to no one" } else { "" }
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
    // an answer given before a message the session is still working on has
    // not been sent, and may never be
    let last_added = ex.turns.iter().rposition(|t| t.kind == "added");
    let pending = |i: usize| in_progress && last_added.is_some_and(|a| a > i);
    let who = opened_by(ex);
    let to = to_whom(ex);
    for (i, t) in ex.turns.iter().enumerate() {
        match t.kind.as_str() {
            "said" => out.push(format!("{}  {who}: {}", t.at, t.text)),
            "added" => out.push(format!("{}  {} added: {}", t.at, t.who, t.text)),
            "did" => {
                out.push(format!("{}  I ran: {}", t.at,
                    if full { t.text.clone() } else { take_chars(&t.text, 300) }));
                if !t.result.is_empty() {
                    out.push(format!("          \u{2192} {}", t.result));
                }
            }
            "aside" => out.push(format!("{}  I (aside): {}", t.at,
                if full { t.text.clone() } else { take_chars(&t.text, 200) })),
            "answered" if pending(i) => if !t.text.is_empty() {
                out.push(format!("{}  I (aside): {}", t.at,
                    if full { t.text.clone() } else { take_chars(&t.text, 200) }));
            },
            "answered" => out.push(format!("{}  I said{to}: {}", t.at,
                if t.text.is_empty() { "(nothing)" } else { &t.text })),
            _ => {}
        }
    }
    if !ex.turns.iter().enumerate().any(|(i, t)| t.kind == "answered" && !pending(i)) {
        let waiting = ex.turns.iter().enumerate().any(|(i, t)| t.kind == "answered" && pending(i));
        out.push(format!("          I said{to}: {}", if !ex.answer.is_empty() && !waiting {
            &ex.answer
        } else if in_progress {
            "(nothing yet \u{2014} this session is in progress)"
        } else {
            "(nothing \u{2014} I did not answer)"
        }));
    }
    out.join("\n")
}

/// One line for a listing.
pub fn line(ex: &Exchange) -> String {
    let said = take_chars(&one_line(&ex.text), 70);
    let ans = take_chars(&one_line(&ex.answer), 70);
    let ans = if ans.is_empty() { "(silent)".to_string() } else { ans };
    let who = match (ex.speaker.is_empty(), ex.date.is_empty()) {
        _ if ex.channel == "nobody" => opened_by(ex),
        (false, _) if ex.channel == "clock" => opened_by(ex),
        (false, _) => ex.speaker.clone(),
        (true, true) => UNPARSED.to_string(),
        (true, false) => "?".to_string(),
    };
    format!("{}  {}  {who}: {said}\n          me{}: {ans}",
        take_chars(&ex.session, 8),
        if ex.date.is_empty() { "----------" } else { &ex.date },
        to_whom(ex))
}

/// Whether an exchange is one with this person, for a listing of them: `name`
/// anywhere in its speaker, or one of `others`, the other names the person
/// goes by, as the whole name of someone it names, so that one of those names
/// inside a longer one lists nobody else. A clock exchange in which she gave
/// no answer that says something passed nothing between them, and a look
/// every morning would otherwise push what the person said out of the most
/// recent few; `--day` and the id still show it. One that reached no one was
/// with no one, whoever it was about.
pub fn was_with(ex: &Exchange, name: &str, others: &[String]) -> bool {
    if ex.channel == "nobody" || (ex.channel == "clock" && py_strip(&ex.answer).is_empty()) {
        return false;
    }
    ex.speaker.to_lowercase().contains(&name.to_lowercase())
        || names_in(ex).iter().any(|s| others.iter().any(|o| o.to_lowercase() == s.to_lowercase()))
}

/// Everyone an exchange names: its speaker, or whom the clock had her speak
/// to; anyone whose message the opening turn took with the speaker's; and
/// whoever added one while it worked.
fn names_in(ex: &Exchange) -> Vec<String> {
    let opened = ex.speaker.split(" (then ").next().unwrap_or("");
    let mut out = speakers(opened);
    out.extend(ex.turns.iter().filter(|t| t.kind == "added" && !t.who.is_empty())
        .map(|t| t.who.clone()));
    out
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

#[cfg(test)]
mod tests {
    use super::*;

    fn prompt(arrival: &str) -> String {
        format!("I am wanda.\n\nToday is 2026-01-01.\n\n{arrival}\n\nDo three things, in this order.\n\nRun mem as: mem\n")
    }

    // the product's frames, written out whole as a session is handed them, so
    // a change to either the product or this parser shows here
    #[test]
    fn the_product_frames_read_back() {
        for (arrival, chan) in [
            ("In a direct message that probe and I read.\n\nThe conversation so far:\n\n    \
              Mon 2025-12-29 23:58 probe: earlier\n    23:59 me: reply\n\nprobe now says:\n\n    \
              one line\n    and a second", "dm"),
            ("In a group direct message that other, probe and I read. Everyone in it sees what \
              I say there.\n\nprobe says:\n\n    one line\n    and a second", "group dm"),
            ("In a Slack channel that other, probe and I read. Everyone in it sees what I say \
              there.\n\nThe conversation so far:\n\n    09:10 other: earlier\n        over two lines\n\n\
              probe now says:\n\n    one line\n    and a second", "channel"),
            ("In a public Slack channel that anyone in this Slack can read; other, probe and I are \
              in it.\n\nThe conversation so far:\n\n    09:10 other: earlier\n\n\
              probe now says:\n\n    one line\n    and a second", "public channel"),
            ("In a Slack thread that other (a guest in this Slack), probe and I read. Everyone \
              in it sees what I say there.\n\nThe thread so far:\n\n    09:10 other: earlier\n\n\
              probe now says:\n\n    one line\n    and a second", "thread"),
            ("In a Slack thread in a public channel that anyone in this Slack can read; probe and \
              I are in it.\n\nprobe says:\n\n    one line\n    and a second", "public thread"),
        ] {
            assert_eq!(parse_prompt(&prompt(arrival)),
                       ("2026-01-01".into(), chan.into(), "probe".into(),
                        "one line\nand a second".into()), "{arrival}");
        }
    }

    fn entry(v: serde_json::Value) -> String {
        format!("{v}\n")
    }

    fn addition(speaker: &str, text: &str) -> String {
        format!("{speaker} adds this in the same group direct message at 16:42, before anything I say \
                 back has been sent:\n\n    {text}\n\nNothing I have said back in this session has been \
                 sent yet. The last answer I give in this session that says something is the one sent, so \
                 that is where anything said here gets its answer.")
    }

    fn exchange_of(name: &str, lines: &[String]) -> Exchange {
        let dir = std::env::temp_dir().join(format!("transcript-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(format!("{name}.jsonl"));
        std::fs::write(&path, lines.concat()).unwrap();
        let ex = load(&path);
        std::fs::remove_file(&path).unwrap();
        ex
    }

    fn opening() -> String {
        prompt("In a group direct message that fan, mei and I read. Everyone in it sees what I say \
                there.\n\nfan says:\n\n    remind me at 5")
    }

    // as Claude Code 2.1.268 records a session with open input:
    // the prompt as text blocks, a message taken in at a turn's next step as
    // a queued_command attachment
    #[test]
    fn a_message_added_while_the_session_worked_is_read_back_as_its_speakers() {
        use serde_json::json;
        let ts = |s: &str| format!("2026-10-01T23:42:{s}.000Z");
        let ex = exchange_of("mid-turn", &[
            entry(json!({"type": "user", "timestamp": ts("00"), "promptSource": "sdk",
                        "message": {"role": "user", "content": [{"type": "text", "text": opening()}]}})),
            entry(json!({"type": "assistant", "timestamp": ts("05"), "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                     "input": {"command": "mem recall plumber"}}]}})),
            entry(json!({"type": "user", "timestamp": ts("09"), "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "(nothing)"}]}})),
            entry(json!({"type": "attachment", "timestamp": ts("07"), "attachment": {
                        "type": "queued_command", "commandMode": "prompt",
                        "prompt": [{"type": "text", "text": addition("mei", "and tell me too")}]}})),
            entry(json!({"type": "user", "isMeta": true, "timestamp": ts("10"),
                        "message": {"role": "user", "content": "[structured-output-enforce] x"}})),
            entry(json!({"type": "attachment", "timestamp": ts("12"), "attachment": {
                        "type": "structured_output", "data": {"answer": "At 5, both of you.",
                                                              "recalled": [], "recorded": []}}})),
        ]);
        assert_eq!((ex.date.as_str(), ex.channel.as_str(), ex.text.as_str()),
                   ("2026-01-01", "group dm", "remind me at 5"));
        assert_eq!(ex.speaker, "fan (then mei)");
        assert!(!one_speaker(&ex));
        assert_eq!(ex.answer, "At 5, both of you.");
        let shown = render(&ex, false, false);
        assert!(shown.contains("23:42:00  fan said: remind me at 5\n"), "{shown}");
        assert!(shown.contains("23:42:07  mei added: and tell me too\n"), "{shown}");
        assert!(shown.ends_with("23:42:12  I said: At 5, both of you."), "{shown}");
        assert!(line(&ex).contains("fan (then mei): remind me at 5"));
    }

    // a message that began a later turn: the answer given before it is sent
    // unless a later one says something, as the product posts them
    #[test]
    fn an_answer_stands_until_a_later_one_says_something() {
        use serde_json::json;
        let ts = |s: &str| format!("2026-10-01T23:42:{s}Z");
        let lines = [
            entry(json!({"type": "user", "timestamp": ts("00"),
                        "message": {"role": "user", "content": [{"type": "text", "text": opening()}]}})),
            entry(json!({"type": "attachment", "timestamp": ts("10"), "attachment": {
                        "type": "structured_output", "data": {"answer": "Will do."}}})),
            entry(json!({"type": "user", "timestamp": ts("11"), "message": {"role": "user",
                        "content": [{"type": "text", "text": addition("fan", "thanks!")}]}})),
        ];
        // while the later turn works, the earlier answer is not yet sent
        let ex = exchange_of("later-turn", &lines);
        assert_eq!(ex.speaker, "fan");
        assert!(one_speaker(&ex) && ex.answer == "Will do." && !ex.answered());
        let shown = render(&ex, false, true);
        assert!(shown.contains("23:42:10  I (aside): Will do.\n23:42:11  fan added: thanks!"), "{shown}");
        assert!(shown.ends_with("I said: (nothing yet \u{2014} this session is in progress)"), "{shown}");
        // a later turn that says nothing leaves it the answer
        let silent = entry(json!({"type": "attachment", "timestamp": ts("15"), "attachment": {
                                 "type": "structured_output", "data": {"answer": ""}}}));
        let ex = exchange_of("later-silent", &[lines.concat(), silent]);
        let shown = render(&ex, false, false);
        assert!(ex.answer == "Will do." && shown.contains("23:42:10  I said: Will do.\n23:42:11  fan added: thanks!"));
        assert!(!shown.contains("(nothing)") && line(&ex).ends_with("me: Will do."), "{shown}");
        // one that says something replaces it
        let later = entry(json!({"type": "attachment", "timestamp": ts("15"), "attachment": {
                                "type": "structured_output", "data": {"answer": "At 5, then."}}}));
        let shown = render(&exchange_of("later-said", &[lines.concat(), later]), false, false);
        assert!(shown.contains("23:42:10  I (aside): Will do.\n23:42:11  fan added: thanks!\n\
                                23:42:15  I said: At 5, then."), "{shown}");
    }

    // an empty answer before one that says something is not shown at all
    #[test]
    fn an_empty_answer_a_later_one_replaced_is_not_shown() {
        use serde_json::json;
        let ex = exchange_of("empty-first", &[
            entry(json!({"type": "user", "timestamp": "2026-10-01T23:42:00Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": opening()}]}})),
            entry(json!({"type": "attachment", "timestamp": "2026-10-01T23:42:10Z", "attachment": {
                        "type": "structured_output", "data": {"answer": ""}}})),
            entry(json!({"type": "user", "timestamp": "2026-10-01T23:42:11Z", "message": {"role": "user",
                        "content": [{"type": "text", "text": addition("fan", "and the plumber")}]}})),
            entry(json!({"type": "attachment", "timestamp": "2026-10-01T23:42:20Z", "attachment": {
                        "type": "structured_output", "data": {"answer": "At 5."}}})),
        ]);
        let shown = render(&ex, false, false);
        assert!(!shown.contains("(aside)") && !shown.contains("(nothing)") && ex.answer == "At 5.", "{shown}");
    }

    // a message Claude Code took into the opening turn with the prompt is a
    // later block of the opening message
    #[test]
    fn a_message_taken_with_the_prompt_is_added() {
        use serde_json::json;
        let ex = exchange_of("merged", &[
            entry(json!({"type": "user", "timestamp": "2026-10-01T23:42:00Z", "message": {"role": "user",
                        "content": [{"type": "text", "text": opening()},
                                    {"type": "text", "text": addition("mei", "and tell me too")}]}})),
        ]);
        assert_eq!((ex.text.as_str(), ex.speaker.as_str()), ("remind me at 5", "fan (then mei)"));
        assert!(!one_speaker(&ex));
        assert!(render(&ex, false, false).contains("23:42:00  mei added: and tell me too"));
    }

    // a background command's notice of its end, mid-turn or as a turn of its
    // own, and Claude Code's own queued notes, are no one's message
    #[test]
    fn a_notice_is_not_a_message() {
        use serde_json::json;
        let notice = "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n</task-notification>";
        let ex = exchange_of("notices", &[
            entry(json!({"type": "user", "timestamp": "2026-10-01T23:42:00Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": opening()}]}})),
            entry(json!({"type": "attachment", "timestamp": "2026-10-01T23:42:05Z", "attachment": {
                        "type": "queued_command", "commandMode": "task-notification", "prompt": notice}})),
            entry(json!({"type": "attachment", "timestamp": "2026-10-01T23:42:06Z", "attachment": {
                        "type": "queued_command", "commandMode": "prompt", "isMeta": true, "prompt": "a note"}})),
            entry(json!({"type": "attachment", "timestamp": "2026-10-01T23:42:10Z", "attachment": {
                        "type": "structured_output", "data": {"answer": "Will do."}}})),
            entry(json!({"type": "user", "timestamp": "2026-10-01T23:42:12Z", "origin": {"kind": "task-notification"},
                        "message": {"role": "user", "content": notice}})),
            entry(json!({"type": "attachment", "timestamp": "2026-10-01T23:42:15Z", "attachment": {
                        "type": "structured_output", "data": {"answer": ""}}})),
        ]);
        let shown = render(&ex, false, false);
        assert!(!shown.contains("added") && one_speaker(&ex) && ex.answer == "Will do.", "{shown}");
        assert!(shown.ends_with("23:42:10  I said: Will do."), "{shown}");
    }

    // someone already named in a turn of several speakers is not named twice;
    // words not in the product's shape, as Claude Code queues and begins turns
    // with its own, are no one's, in a turn's message or handed at a step
    #[test]
    fn an_addition_names_only_someone_new() {
        use serde_json::json;
        let arrival = "In a group direct message that fan, mei and I read. Everyone in it sees what I \
                       say there.\n\nThe conversation so far:\n\n    16:41 fan: remind me at 5\n\n\
                       mei now says, after fan:\n\n    me too";
        let ex = exchange_of("named-once", &[
            entry(json!({"type": "user", "message": {"role": "user", "content": prompt(arrival)}})),
            entry(json!({"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                        "prompt": [{"type": "text", "text": addition("fan", "at the house")}]}})),
            entry(json!({"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt",
                        "prompt": "a prompt of Claude Code's own"}})),
            entry(json!({"type": "user", "message": {"role": "user",
                        "content": [{"type": "text", "text": "a shape no frame has"}]}})),
        ]);
        assert_eq!(ex.speaker, "mei (after fan)");
        let shown = render(&ex, false, false);
        assert!(shown.contains("fan added: at the house") && !shown.contains("a shape no frame has")
                && !shown.contains("Claude Code's own") && !shown.contains("someone"), "{shown}");
    }

    // a turn that took messages from more than one person is no one person's
    #[test]
    fn a_turn_of_several_speakers_is_no_one_persons() {
        let arrival = "In a group direct message that other, probe and I read. Everyone in it sees \
                       what I say there.\n\nThe conversation so far:\n\n    16:58 other: remind me at 5\n\n\
                       probe now says, after other:\n\n    one line";
        assert_eq!(parse_prompt(&prompt(arrival)),
                   ("2026-01-01".into(), "group dm".into(), "probe (after other)".into(),
                    "one line".into()));
    }

    fn exchange(prompt: &str, answer: &str) -> Exchange {
        let (date, channel, speaker, text) = parse_prompt(prompt);
        Exchange { date, channel, speaker, text, answer: answer.into(), ..Default::default() }
    }

    const LOOK: &str = "I am wanda.\n\nToday is 2031-01-13.\n\nNo message started this session. \
What I say now reaches mei alone, in a direct message.\n\n    It is Monday, 08:00, and this is \
my look at the day ahead for mei.\n\n    Come due for mei after 2031-01-12:\n    \
`trajectory:aaaaaa`  2031-01-13, today  I undertook to remind mei at 5\n        involves: me; mei\n\n\
Do three things, in this order.\n";

    #[test]
    fn a_clock_exchange_reads_back_as_hers_to_the_person_named() {
        let ex = exchange(LOOK, "");
        assert_eq!((ex.date.as_str(), ex.channel.as_str(), ex.speaker.as_str()),
                   ("2031-01-13", "clock", "mei"));
        assert_eq!(ex.text, "It is Monday, 08:00, and this is my look at the day ahead for mei.\n\n\
Come due for mei after 2031-01-12:\n`trajectory:aaaaaa`  2031-01-13, today  I undertook to \
remind mei at 5\n    involves: me; mei");
        assert!(line(&ex).contains("unprompted, to mei: It is Monday, 08:00"));
    }

    #[test]
    fn a_silent_clock_exchange_is_left_out_of_a_listing_with_that_person() {
        assert!(!was_with(&exchange(LOOK, ""), "mei", &[]));
        assert!(!was_with(&exchange(LOOK, "  "), "Mei", &[]));
        assert!(was_with(&exchange(LOOK, "Morning, it is at 5 today."), "mei", &[]));
        let dm = "I am wanda.\n\nToday is 2031-01-13.\n\nmei says to me, in a direct message:\
\n\n    morning\n\nDo three things, in this order.\n";
        assert!(was_with(&exchange(dm, ""), "mei", &[]), "a silent reply to a message still lists");
        assert!(!was_with(&exchange(dm, "hi"), "fan", &[]));
    }

    /// A transcript as Claude Code writes one: the prompt, then each turn's
    /// structured output as an attachment.
    fn transcript(tag: &str, prompt: &str, answers: &[&str]) -> Exchange {
        let dir = std::env::temp_dir().join(format!("mem-transcript-{tag}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut lines = vec![serde_json::json!({"type": "user", "timestamp": "2031-01-13T16:00:00.000Z",
            "message": {"role": "user", "content": prompt}}).to_string()];
        for (i, a) in answers.iter().enumerate() {
            lines.push(serde_json::json!({"type": "attachment",
                "timestamp": format!("2031-01-13T16:0{}:00.000Z", i + 1),
                "attachment": {"type": "structured_output",
                               "data": {"recalled": [], "answer": a, "recorded": []}}}).to_string());
        }
        let path = dir.join("s1.jsonl");
        std::fs::write(&path, lines.join("\n") + "\n").unwrap();
        let ex = load(&path);
        std::fs::remove_dir_all(&dir).ok();
        ex
    }

    // a clock exchange that answered in one turn and said nothing in a later
    // one, begun when a command it left running ended
    #[test]
    fn a_clock_exchange_reads_as_its_last_answer_that_says_something() {
        let ex = transcript("clock", LOOK, &["Morning, it is at 5 today.", ""]);
        assert_eq!(ex.answer, "Morning, it is at 5 today.");
        assert!(was_with(&ex, "mei", &[]), "listed with the person it reached");
        assert!(line(&ex).ends_with("me: Morning, it is at 5 today."), "{}", line(&ex));
        // shown whole, the empty answer after it changes nothing
        let full = render(&ex, true, false);
        assert!(full.ends_with("16:01:00  I said: Morning, it is at 5 today.") && !full.contains("(nothing)"),
                "{full}");
        // nothing said in any turn is silent
        let ex = transcript("clock-silent", LOOK, &["", " "]);
        assert!(!was_with(&ex, "mei", &[]) && line(&ex).ends_with("me: (silent)"));
        // a message's exchange reads the same way
        let dm = "I am wanda.\n\nToday is 2031-01-13.\n\nmei says to me, in a direct message:\
\n\n    morning\n\nDo three things, in this order.\n";
        let ex = transcript("dm", dm, &["Morning.", ""]);
        assert_eq!(ex.answer, "Morning.");
        assert!(line(&ex).ends_with("me: Morning."));
    }

    // the product's frame for a session it starts itself and whose answer it
    // posts nowhere, written out whole with the news it carries
    const NOBODY: &str = "No message started this session. What I say now reaches no one.\n\n    \
The person I have known in this Slack as fan is named Fan Zhu there now. It is the same person; only \
the name I take for them from this Slack has changed. When my memory is read after this session ends, \
their messages, and others' mentions of them, start to reach me under Fan Zhu if it finds exactly one \
person by the name Fan Zhu, named Fan Zhu, in any capitals, who is also found by the name fan, or was \
made in this session while the name fan finds no one; and, if this session ended without failing, also \
if it finds no one by either name. Otherwise they go on reaching me under fan. What they said before \
now, and what I hold about them, may name them fan. A note of mine that names them only in its words \
has no link to them: `mem recall` reaches it by neither name, and `mem search` finds it only by a word \
of three or more characters that it holds.";

    #[test]
    fn a_session_that_reaches_no_one_reads_back_with_no_speaker() {
        let news = NOBODY.split_once("\n\n    ").unwrap().1;
        assert_eq!(parse_prompt(&prompt(NOBODY)),
                   ("2026-01-01".into(), "nobody".into(), String::new(), news.into()));
        // a look for someone called "no one" is still the clock's
        let look = "No message started this session. What I say now reaches no one alone, in a direct \
                    message.\n\n    It is Monday, 08:00, and this is my look at the day ahead for no one.";
        assert_eq!(parse_prompt(&prompt(look)),
                   ("2026-01-01".into(), "clock".into(), "no one".into(),
                    "It is Monday, 08:00, and this is my look at the day ahead for no one.".into()));
    }

    // what she said there was posted nowhere, and reads so; it was with no one
    #[test]
    fn a_session_that_reaches_no_one_is_shown_as_said_to_no_one() {
        let opened = format!("I am wanda.\n\nToday is 2031-01-13.\n\n{NOBODY}\n\nDo three things, in this order.\n");
        let ex = transcript("nobody", &opened, &["I renamed fan's node to Fan Zhu."]);
        let shown = render(&ex, false, false);
        assert!(shown.starts_with("session s1 \u{b7} 2031-01-13 \u{b7} nobody \u{b7} 0 actions\n\n\
                                   16:00:00  unprompted, to no one: The person I have known in this Slack \
                                   as fan is named Fan Zhu there now."), "{shown}");
        assert!(shown.ends_with("\n16:01:00  I said, to no one: I renamed fan's node to Fan Zhu."), "{shown}");
        let listed = line(&ex);
        assert!(listed.starts_with("s1  2031-01-13  unprompted, to no one: The person I have known")
                && listed.ends_with("\n          me, to no one: I renamed fan's node to Fan Zhu."), "{listed}");
        for name in ["fan", "Fan Zhu", "no one", "nobody", ""] {
            assert!(!was_with(&ex, name, &["fan".into(), "Fan Zhu".into()]), "{name}");
        }
        // with no answer, or an empty one
        let silent = transcript("nobody-silent", &opened, &[]);
        assert!(render(&silent, false, false)
                .ends_with("\n          I said, to no one: (nothing \u{2014} I did not answer)"));
        assert!(line(&silent).ends_with("\n          me, to no one: (silent)"));
        assert!(render(&transcript("nobody-empty", &opened, &[""]), false, false)
                .ends_with("\n16:01:00  I said, to no one: (nothing)"));
    }
}
