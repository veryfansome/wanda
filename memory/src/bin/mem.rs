//! `mem` — what a session calls to read the graph and write to it.
//!
//! Everything here is mechanical. Traversal and ranking are graph arithmetic,
//! and writing is bookkeeping: stable ids, reverse edges, index regeneration.
//! What to recall from, and what is worth recording, are the session's
//! decisions.
//!
//! Arguments are names, not ids, because names are what a session reads in an
//! index. Anything unresolved is created rather than refused.

use clap::{Parser, Subcommand};
use memory::index;
use memory::recall::{self, HOPS, LIMIT};
use memory::transcript;
use memory::fm::Edge;
use memory::text::{line_for, marks, one_line, py_repr, py_strip};
use memory::vault::Vault;
use std::collections::BTreeSet;
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Parser)]
#[command(name = "mem", about = "read and write my memory", disable_help_subcommand = true)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// expand from things I have identified
    Recall {
        #[arg(required = true)]
        refs: Vec<String>,
        #[arg(long, default_value_t = HOPS)]
        hops: i64,
        #[arg(long, default_value_t = LIMIT)]
        limit: i64,
    },
    /// full text, when I do not know the name
    Search {
        text: String,
        #[arg(long, default_value_t = 10)]
        limit: i64,
    },
    /// one node and its edges
    Show { r#ref: String },
    /// record a person, place, org, group, thing or topic
    Entity {
        #[arg(long, required = true,
              value_parser = ["person","place","org","group","thing","topic"])]
        kind: String,
        #[arg(long, required = true,
              help = format!("the label, at most {} characters; the same name is the same node", memory::SUMMARY_MAX))]
        name: String,
        #[arg(long, default_value = "",
              help = format!("the index line: one line, at most {} characters", memory::SUMMARY_MAX))]
        summary: String,
        #[arg(long, default_value = "")]
        body: String,
        /// a second node with a name one already has: two different people, one name
        #[arg(long)]
        new: bool,
        /// update this node, when two share the name
        #[arg(long, default_value = "")]
        id: String,
    },
    /// record something that happened and stays true; not a message
    Event {
        #[arg(long, required = true,
              help = format!("the index line: one line, at most {} characters", memory::SUMMARY_MAX))]
        summary: String,
        #[arg(long, default_value = "")]
        body: String,
        /// when it happened, if not today
        #[arg(long, default_value = "", value_name = "YYYY-MM-DD")]
        when: String,
        /// names, comma-separated
        #[arg(long, default_value = "")]
        participants: String,
        #[arg(long, default_value = "")]
        place: String,
        /// a second event with the same summary and date
        #[arg(long)]
        new: bool,
    },
    /// record how two things stand to each other
    Relate {
        #[arg(long, required = true)]
        subject: String,
        #[arg(long, required = true)]
        rel: String,
        #[arg(long, required = true)]
        object: String,
        /// the same relation seen from the object: sibling_of, employs
        #[arg(long, default_value = "")]
        inverse: String,
    },
    /// record a standing preference or instruction
    Pref {
        /// whose preference it is
        #[arg(long, required = true)]
        whose: String,
        #[arg(long, required = true,
              help = format!("the index line: one line, at most {} characters", memory::SUMMARY_MAX))]
        summary: String,
        #[arg(long, default_value = "")]
        body: String,
        /// what sort of rule. A new rule is a preference unless told
        /// otherwise; a restated rule keeps the kind it has
        #[arg(long, value_parser = ["mail-disposition","preference","etiquette"])]
        kind: Option<String>,
        #[arg(long, default_value = "")]
        about: String,
        #[arg(long)]
        new: bool,
    },
    /// open something mid-sequence
    Trajectory {
        #[arg(long, required = true,
              help = format!("the index line: one line, at most {} characters", memory::SUMMARY_MAX))]
        summary: String,
        #[arg(long, default_value = "")]
        body: String,
        /// what would close it
        #[arg(long, required = true)]
        expect: String,
        /// the date this should have resolved by, if there is one; not who
        #[arg(long, default_value = "", value_name = "YYYY-MM-DD")]
        by: String,
        /// names, comma-separated: who or what it involves
        #[arg(long, default_value = "")]
        about: String,
        #[arg(long)]
        new: bool,
    },
    /// move or close a trajectory that already exists
    Advance {
        r#ref: String,
        #[arg(long, default_value = "", value_parser = ["", "open", "closed"])]
        status: String,
        /// a revised date this should resolve by; not who
        #[arg(long, default_value = "", value_name = "YYYY-MM-DD")]
        by: String,
        #[arg(long, default_value = "")]
        note: String,
    },
    /// give a node a new name or summary; its id and every edge to it stay
    Rename {
        node: String,
        #[arg(default_value = "",
              help = format!("a new name, at most {} characters", memory::SUMMARY_MAX))]
        name: String,
        #[arg(long, default_value = "",
              help = format!("a new summary — the index line, at most {} characters. An event, a trajectory or a preference is named by its summary: a new name or a new summary, not both", memory::SUMMARY_MAX))]
        summary: String,
        #[arg(long, default_value = "")]
        because: String,
    },
    /// remove a node that should never have existed; refused while anything links to it
    Forget {
        r#ref: String,
        #[arg(long, default_value = "")]
        because: String,
    },
    /// unsay something that was never true
    Retract {
        #[arg(long, required = true)]
        subject: String,
        #[arg(long, default_value = "")]
        rel: String,
        #[arg(long, default_value = "")]
        object: String,
        #[arg(long, default_value = "")]
        inverse: String,
        /// strike a body line containing this text
        #[arg(long, default_value = "")]
        line: String,
        #[arg(long, default_value = "")]
        because: String,
    },
    /// an exchange from the transcripts: what was said both ways, and what I did
    Session {
        /// a session id, or a prefix of one
        #[arg(default_value = "")]
        r#ref: String,
        /// list the exchanges of one day
        #[arg(long, default_value = "", value_name = "YYYY-MM-DD")]
        day: String,
        /// list exchanges with this person
        #[arg(long = "with", default_value = "", value_name = "NAME")]
        with_: String,
        /// only the most recent N
        #[arg(long, default_value_t = 0, value_name = "N")]
        last: i64,
        /// tool calls and asides untruncated
        #[arg(long)]
        full: bool,
    },
    /// this list
    Help,
}

impl Cmd {
    fn name(&self) -> &'static str {
        match self {
            Cmd::Recall { .. } => "recall",
            Cmd::Search { .. } => "search",
            Cmd::Show { .. } => "show",
            Cmd::Entity { .. } => "entity",
            Cmd::Event { .. } => "event",
            Cmd::Relate { .. } => "relate",
            Cmd::Pref { .. } => "pref",
            Cmd::Trajectory { .. } => "trajectory",
            Cmd::Advance { .. } => "advance",
            Cmd::Rename { .. } => "rename",
            Cmd::Forget { .. } => "forget",
            Cmd::Retract { .. } => "retract",
            Cmd::Session { .. } => "session",
            Cmd::Help => "help",
        }
    }
}

/// The date this process was given — the story's, not the clock's.
fn today() -> String {
    std::env::var("MEM_DATE").unwrap_or_default()
}

/// One date in a file. MEM_DATE is the date this process was given, and
/// frontmatter is stamped from it already; free text a session composes is not,
/// so a date taken from the system clock lands in prose where a later session
/// reads it as the date the note was made. This rewrites those to MEM_DATE.
///
/// The system date exactly, and nothing near it: the history is shifted to end
/// short of that date and never level with it, so a date that matches it was
/// read off the clock rather than taken from the story.
fn restamp(text: &str) -> String {
    let today = today();
    if today.is_empty() || text.is_empty() {
        return text.to_string();
    }
    // a replay gives the date the recorded call ran on, because what this
    // scrubbed then decides what the store holds
    let clock = std::env::var("MEM_REAL_DATE").ok().filter(|s| !s.is_empty())
        .unwrap_or_else(today_local);
    text.replace(&clock, &today)
}

fn today_local() -> String {
    // the operator's local date, as CPython's date.today() is
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64).unwrap_or(0);
    let off = local_offset_seconds(now);
    civil_from_days((now + off).div_euclid(86_400))
}

fn local_offset_seconds(_now: i64) -> i64 {
    // TZ handling is the host's; the lab always runs in UTC and a replay pins
    // the date outright, so this is the one place the two can part.
    std::env::var("MEM_UTC_OFFSET").ok().and_then(|s| s.parse().ok()).unwrap_or(0)
}

fn civil_from_days(z: i64) -> String {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!("{y:04}-{m:02}-{d:02}")
}

/// None for a date not on the calendar, which `date_or_die` lets through.
fn days_from_civil(s: &str) -> Option<i64> {
    let y: i64 = s.get(0..4)?.parse().ok()?;
    let m: i64 = s.get(5..7)?.parse().ok()?;
    let d: i64 = s.get(8..10)?.parse().ok()?;
    let y2 = if m <= 2 { y - 1 } else { y };
    let era = y2.div_euclid(400);
    let yoe = y2 - era * 400;
    let doy = (153 * ((m + 9) % 12) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let z = era * 146_097 + doe - 719_468;
    (civil_from_days(z) == s.get(..10)?).then_some(z)
}

/// A deadline counted from the wrong today looks like any other date; its
/// distance from the right one does not.
fn by_from_today(by: &str) -> Option<String> {
    let today = today();
    let n = days_from_civil(by)? - days_from_civil(&today)?;
    Some(match n {
        0 => format!("(--by {by} is today, {today})"),
        1 => format!("(--by {by} is 1 day after today, {today})"),
        -1 => format!("(--by {by} is 1 day before today, {today})"),
        n if n > 0 => format!("(--by {by} is {n} days after today, {today})"),
        n => format!("(--by {by} is {} days before today, {today})", -n),
    })
}

fn vault() -> Vault {
    // MEM_SESSION is the id this session was given; every node written here is
    // stamped with it, so a node knows the exchange that made it
    let root = std::env::var("MEM_VAULT").map(PathBuf::from)
        .unwrap_or_else(|_| std::env::current_dir().unwrap_or_default());
    let mut v = Vault::new(root);
    v.session = std::env::var("MEM_SESSION").unwrap_or_default();
    // rebuilding a store from its own recorded calls keeps the ids it had
    if let Ok(o) = std::env::var("MEM_ORACLE") {
        if !o.is_empty() {
            v.oracle = Some(Box::new(Vault::new(o)));
            if let Ok(p) = std::env::var("MEM_ORACLE_ORDER") {
                if let Ok(text) = std::fs::read_to_string(&p) {
                    v.oracle_order = serde_json::from_str(&text).ok();
                }
            }
        }
    }
    v
}

/// A write failed because the reader left, as `head` does.
static CUT: AtomicBool = AtomicBool::new(false);

/// A line to stdout. A reader that has left does not stop the command, so the
/// call is still logged, with its exit code.
macro_rules! out {
    ($($a:tt)*) => { emit(&mut std::io::stdout(), format_args!($($a)*)) };
}

macro_rules! err {
    ($($a:tt)*) => { emit(&mut std::io::stderr(), format_args!($($a)*)) };
}

fn emit(w: &mut dyn Write, line: std::fmt::Arguments) {
    if writeln!(w, "{line}").is_err() {
        CUT.store(true, Ordering::Relaxed);
    }
}

/// What was actually asked of memory, recorded by the thing being asked.
/// Reconstructing it from the outside undercounts: several `mem` calls chain
/// into one shell command, and only the first is visible there. Never fails the
/// command.
fn log(cmd: &str, rc: i32, argv: &[String]) {
    let Ok(path) = std::env::var("LAB_MEMLOG") else { return };
    if path.is_empty() {
        return;
    }
    let mut rec = serde_json::json!({
        "ts": iso_now(),
        "input_key": std::env::var("LAB_INPUT").unwrap_or_default(),
        // the date and session this call ran under, so a rebuild reads them
        // rather than recovering them from the key's shape
        "date": std::env::var("MEM_DATE").unwrap_or_default(),
        "session": std::env::var("MEM_SESSION").unwrap_or_default(),
        "cmd": cmd, "rc": rc, "argv": argv,
    });
    if CUT.load(Ordering::Relaxed) {
        rec["cut"] = serde_json::Value::Bool(true);
    }
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(f, "{}", memory::text::py_json_utf8(&rec));
    }
}

fn iso_now() -> String {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = now.as_secs() as i64;
    let ms = now.subsec_millis();
    let day = civil_from_days(secs.div_euclid(86_400));
    let t = secs.rem_euclid(86_400);
    format!("{day}T{:02}:{:02}:{:02}.{ms:03}+00:00", t / 3600, (t % 3600) / 60, t % 60)
}

/// resolve, with an ambiguous name refused out loud: the candidates are printed
/// and the command stops, because putting the fact on the wrong one is worse
/// than asking.
fn resolve(v: &Vault, r: &str) -> Result<Option<String>, i32> {
    match v.resolve(r, "") {
        Ok(x) => Ok(x),
        Err(a) => {
            out!("({a}. An id says which.)");
            Err(1)
        }
    }
}

fn cmd_recall(v: &Vault, refs: &[String], hops: i64, limit: i64) -> i32 {
    let con = match index::build_index(v, &v.root.join(".index.db")) {
        Ok(c) => c,
        Err(_) => return 1,
    };
    let mut seeds: BTreeSet<String> = BTreeSet::new();
    let mut complaints: BTreeSet<String> = BTreeSet::new();
    for r in refs {
        match v.resolve(r, "") {
            Ok(Some(nid)) => { seeds.insert(nid); }
            Ok(None) => { complaints.insert(format!("(no node for {})", py_repr(r))); }
            Err(a) => { complaints.insert(format!("({a}. An id says which.)")); }
        }
    }
    // sorted, so that what a session reads is the same whichever order it named
    // the refs in — the two streams are merged by the tool it runs this with
    for c in &complaints {
        err!("{c}");
    }
    if seeds.is_empty() {
        out!("nothing to expand from");
        return 1;
    }
    let rows = match recall::walk(&con, &seeds, hops) {
        Ok(r) => r,
        Err(_) => return 1,
    };
    if let Some(w) = recall::dangling_warning(&con) {
        err!("{}", w.strip_suffix('\n').unwrap_or(&w));
    }
    out!("expanded from {}: {}\n", seeds.len(),
             seeds.iter().cloned().collect::<Vec<_>>().join(", "));
    for r in recall::take_limit(&rows, limit) {
        out!("{}", r.line());
    }
    0
}

fn cmd_search(v: &Vault, text: &str, limit: i64) -> i32 {
    let Ok(con) = index::build_index(v, &v.root.join(".index.db")) else { return 1 };
    let terms: Vec<String> = text.split_whitespace()
        .filter(|t| t.chars().count() > 2)
        .map(|t| format!("\"{t}\"")).collect();
    if terms.is_empty() {
        return 1;
    }
    let rows: Vec<(String, String, String, String)> = (|| {
        let mut stmt = con.prepare(
            "SELECT f.id, n.name, n.summary, n.status FROM fts f \
             JOIN nodes n ON n.id=f.id WHERE fts MATCH ? ORDER BY rank LIMIT ?").ok()?;
        let out = stmt.query_map(rusqlite::params![terms.join(" OR "), limit], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
        }).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(out)
    })().unwrap_or_default();
    for (nid, name, summary, status) in &rows {
        out!("`{nid}`{}  {}", marks(status), line_for(name, summary));
    }
    if rows.is_empty() {
        out!("(nothing)");
    }
    0
}

fn cmd_show(v: &Vault, r: &str) -> i32 {
    let nid = match resolve(v, r) {
        Ok(Some(n)) => n,
        Ok(None) => { out!("(no node for {})", py_repr(r)); return 1; }
        Err(rc) => return rc,
    };
    let p = v.path_for(&nid);
    // the id is the path, not a line in the file; said here so a session
    // reading this has it to copy
    out!("{nid}\n{}", std::fs::read_to_string(&p).unwrap_or_default());
    let Ok(con) = index::build_index(v, &v.root.join(".index.db")) else { return 0 };
    let back: Vec<(String, String)> = (|| {
        let mut stmt = con.prepare("SELECT src, rel FROM edges WHERE dst=?").ok()?;
        let out = stmt.query_map([&nid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?
            .collect::<Result<Vec<_>, _>>().ok()?;
        Some(out)
    })().unwrap_or_default();
    if !back.is_empty() {
        out!("referred to by:");
        for (src, rel) in back {
            out!("  {src} --{rel}-->");
        }
    }
    0
}


/// The index line, one line, at most SUMMARY_MAX characters. Over the cap it is
/// refused, not cut: the session that has the context rewrites it, and the rest
/// goes in --body.
fn summary_or_die(text: &str, flag: &str) -> Result<String, i32> {
    let t = one_line(text);
    if t.is_empty() {
        out!("({flag} is empty)");
        return Err(1);
    }
    let n = t.chars().count();
    if n > memory::SUMMARY_MAX {
        out!("({flag} is {n} characters; the cap is {}. It is the index line: \
                  the thing in a phrase, and the rest in --body.)",
                 memory::SUMMARY_MAX);
        return Err(1);
    }
    Ok(t)
}

/// `--by` reads as "by whom" as readily as "by when", so a name or an id
/// arrives where a date belongs. It is a date, and a non-date is refused here
/// rather than stored.
fn date_or_die(s: &str, flag: &str) -> Result<String, i32> {
    if s.is_empty() {
        return Ok(String::new());
    }
    static RE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(||
        regex::Regex::new(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$").unwrap());
    if !RE.is_match(s) {
        out!("({flag} must be a date, YYYY-MM-DD; got {}. It says when, not who.)",
                 py_repr(s));
        return Err(1);
    }
    Ok(s.to_string())
}

fn list_of(text: &str) -> Vec<String> {
    text.split(',').map(|x| py_strip(x).to_string()).filter(|x| !x.is_empty()).collect()
}

/// A node of this kind already called this — on this date for an event, for the
/// same person for a preference, still open for a trajectory. Two with one name
/// is allowed, but only on purpose; when two already exist, which one is meant
/// has to be said by id.
fn existing(v: &Vault, kind: &str, name: &str, when: &str, whose: &str, open_only: bool)
    -> Result<Option<String>, i32>
{
    let want = one_line(name).to_lowercase();
    let mut hits: Vec<(String, String)> = Vec::new();
    for n in v.nodes() {
        if n.kind() != kind || memory::fm::label(&n.meta).to_lowercase() != want {
            continue;
        }
        if !when.is_empty() && memory::fm::event_date(&n.id) != when[..10.min(when.len())] {
            continue;
        }
        // `whose` is a node id here: the owner of a rule is the node its
        // `whose` edge points at, however the session spelled them
        if !whose.is_empty()
            && !n.meta.edges.iter().any(|e| e.rel == "whose" && e.to == whose) {
            continue;
        }
        if open_only && n.meta.get("status") != "open" {
            continue;
        }
        let shown = if n.meta.get("summary").is_empty() {
            n.meta.get("name").to_string()
        } else {
            n.meta.get("summary").to_string()
        };
        hits.push((n.id.clone(), shown));
    }
    if hits.len() > 1 {
        let listed: Vec<String> = hits.iter().map(|(id, sh)| format!("{id} ({sh})")).collect();
        let ways = match kind {
            "trajectory" => "`mem advance <id>` moves one, \
                             `mem rename <id> --summary \"...\"` tells them apart, or --new opens another",
            "event" | "preference" => "`mem rename <id> --summary \"...\"` tells them apart, \
                                       or --new makes another",
            _ => "`--id <id>` updates one, `mem rename <id> \"<new name>\"` tells them apart, \
                  or --new makes another",
        };
        out!("({} is already more than one {kind}: {}. {ways}.)", py_repr(name), listed.join("; "));
        return Err(1);
    }
    Ok(hits.into_iter().next().map(|(id, _)| id))
}

/// What was stored, which is not always what was passed: an existing name is
/// kept, the clock's date rewritten, a struck line refused.
fn stored(v: &Vault, nid: &str) -> (memory::fm::Meta, String) {
    let text = std::fs::read_to_string(v.path_for(nid)).unwrap_or_default();
    memory::fm::load(&text, Some(&v.root))
}

fn ok_stored(v: &Vault, nid: &str) {
    let (meta, _) = stored(v, nid);
    out!("ok {nid}\n  {}", line_for(&memory::fm::label(&meta), meta.get("summary")));
}

fn stub(v: &Vault, name: &str, kind: &str) -> String {
    let nid = v.mint(kind, "", None, name);
    v.upsert(&nid, kind, name, "", "", &[], &[], &today());
    nid
}

/// (reference, kind to mint, kind to prefer) → node ids.
///
/// A name nobody has recorded gets a stub — an edge to a node nobody created is
/// a dangling edge, invisible until traversal quietly returns nothing. Her own
/// names are the exception: they find her node, or seed it in a vault without
/// one, since a stub under either would be a second self of whatever kind the
/// flag mints. Every reference is resolved before any stub is minted, so a
/// refusal on the last leaves nothing behind from the first; the same new name
/// twice gets one stub.
/// An id that names no node is refused: there is nothing to guess from a
/// mistyped hash, and minting a person called `34432f` is worse.
fn refs(v: &Vault, wanted: &[(String, &str, &str)]) -> Result<Vec<String>, i32> {
    enum Item { Have(String), Mint(String, String), Me }
    let mut out: Vec<Item> = Vec::new();
    for (r, mint_kind, prefer) in wanted {
        if py_strip(r).is_empty() {
            out!("(a blank where a name was expected)");
            return Err(1);
        }
        let nid = match v.resolve(r, prefer) {
            Ok(x) => x,
            Err(a) => { out!("({a}. An id says which.)"); return Err(1); }
        };
        if nid.is_none() && memory::text::id_shaped(r) {
            out!("(no node {}; a name goes here, or an id from an index)", py_repr(r));
            return Err(1);
        }
        out.push(match nid {
            Some(n) => Item::Have(n),
            None if memory::is_self_name(r) => Item::Me,
            None => Item::Mint(one_line(r), mint_kind.to_string()),
        });
    }
    let mut minted: std::collections::HashMap<(String, String), String> = Default::default();
    let mut ids = Vec::new();
    for item in out {
        match item {
            Item::Have(n) => ids.push(n),
            Item::Me => ids.push(index::seed(v, &today())),
            Item::Mint(name, kind) => {
                let key = (name.to_lowercase(), kind.clone());
                let id = minted.entry(key).or_insert_with(|| stub(v, &name, &kind)).clone();
                ids.push(id);
            }
        }
    }
    Ok(ids)
}

/// A name only a renamed node had. It may have been renamed because the name
/// was wrong, so writing there is unsafe, and minting beside it duplicates.
fn former_only(v: &Vault, kind: &str, name: &str) -> Result<(), i32> {
    let want = one_line(name).to_lowercase();
    let had: Vec<(String, String)> = v.nodes().into_iter()
        .filter(|n| n.kind() == kind
                && memory::fm::former_names(&n.body).iter().any(|a| a.to_lowercase() == want))
        .map(|n| (n.id.clone(), memory::fm::label(&n.meta)))
        .collect();
    match had.as_slice() {
        [] => Ok(()),
        [(id, now)] => {
            out!("({} is a name {id} had; it is named {} now. \
                  --id {id} to update it, or --new for another.)",
                 py_repr(name), py_repr(now));
            Err(1)
        }
        _ => {
            let listed: Vec<String> = had.iter()
                .map(|(id, now)| format!("{id}, named {} now", py_repr(now))).collect();
            out!("({} is a name more than one {kind} had: {}. \
                  --id <id> to update one, or --new for another.)",
                 py_repr(name), listed.join("; "));
            Err(1)
        }
    }
}

/// Her node is the one person labelled "me". Beside a second, hers could not be
/// told apart, and every later "me" would be refused as ambiguous.
fn not_mine(v: &Vault) -> i32 {
    match v.me() {
        Some(me) => out!("('{}' is my own node, {me}; no other person takes that name)",
                         memory::SELF_LABEL),
        None => out!("('{}' names my own node and no other person)", memory::SELF_LABEL),
    }
    1
}

/// A person, place, org, group, thing or topic. The same name again is the same
/// node, updated — two files for one person is the failure that costs most —
/// unless --new says it is a second one, as with two people who share a name.
/// A person named by either of her own names is her node, seeded if the vault
/// lacks it, and a second person labelled "me" is refused.
fn cmd_entity(v: &Vault, kind: &str, name: &str, summary: &str, body: &str,
              new: bool, id: &str) -> i32 {
    let name = match summary_or_die(name, "--name") { Ok(x) => x, Err(rc) => return rc };
    if memory::text::id_shaped(&name) {
        out!("(--name is the label, not an id: {}. To update a node by id, --id <id>)",
                 py_repr(&name));
        return 1;
    }
    let summary = if summary.is_empty() { String::new() } else {
        match summary_or_die(summary, "--summary") { Ok(x) => x, Err(rc) => return rc }
    };
    if kind == "person" && new && name.to_lowercase() == memory::SELF_LABEL {
        return not_mine(v);
    }
    let nid = if !id.is_empty() {
        match resolve(v, id) {
            Ok(Some(n)) if n.starts_with(&format!("{kind}:")) => Some(n),
            Err(rc) => return rc,
            _ => { out!("(no {kind} {})", py_repr(id)); return 1; }
        }
    } else if new {
        None
    } else if kind == "person" && memory::is_self_name(&name) {
        match v.by_name(&name, "person") {
            Ok(Some(n)) if n.starts_with("person:") => Some(n),
            Ok(_) => Some(index::seed(v, &today())),
            Err(a) => { out!("({a}. An id says which.)"); return 1; }
        }
    } else {
        match existing(v, kind, &name, "", "", false) {
            Ok(None) => match former_only(v, kind, &name) { Ok(()) => None, Err(rc) => return rc },
            Ok(x) => x,
            Err(rc) => return rc,
        }
    };
    let nid = nid.unwrap_or_else(|| v.mint(kind, "", None, &name));
    v.upsert(&nid, kind, &name, &summary, body, &[], &[], &today());
    regen(v);
    ok_stored(v, &nid);
    0
}

fn cmd_event(v: &Vault, summary: &str, body: &str, when: &str, participants: &str,
             place: &str, new: bool) -> i32 {
    // when the event happened, which is not always when it was mentioned: an
    // appointment made one week for the next is an event on the day of the
    // visit. `created`/`last_seen` stay stamped from the environment, so when
    // we learned a thing stays separable from when it happened.
    let when = match date_or_die(when, "--when") { Ok(x) => x, Err(rc) => return rc };
    let when = if when.is_empty() { today() } else { when };
    let summary = match summary_or_die(summary, "--summary") { Ok(x) => x, Err(rc) => return rc };
    // the same thing on the same day is the same event; a retried command must
    // not make two of it. A second one on purpose is --new
    if !new {
        match existing(v, "event", &summary, &when, "", false) {
            Ok(Some(dup)) => {
                out!("(already here as {dup}; --new if this is a second one)");
                return 1;
            }
            Err(rc) => return rc,
            _ => {}
        }
    }
    let mut wanted: Vec<(String, &str, &str)> = list_of(participants).into_iter()
        .map(|x| (x, "person", "person")).collect();
    if !place.is_empty() {
        wanted.push((place.to_string(), "place", "place"));
    }
    let ids = match refs(v, &wanted) { Ok(x) => x, Err(rc) => return rc };
    let (people, place_id) = if place.is_empty() {
        (&ids[..], String::new())
    } else {
        (&ids[..ids.len() - 1], ids[ids.len() - 1].clone())
    };
    let nid = v.mint("event", &when[..10.min(when.len())], None, &summary);
    let mut edges: Vec<Edge> = people.iter()
        .map(|p| Edge { rel: "involves".into(), to: p.clone() }).collect();
    if !place_id.is_empty() {
        edges.push(Edge { rel: "at".into(), to: place_id });
    }
    v.upsert(&nid, "event", &summary, &summary, body, &[], &edges, &today());
    regen(v);
    ok_stored(v, &nid);
    0
}

fn cmd_relate(v: &Vault, subject: &str, rel: &str, object: &str, inverse: &str) -> i32 {
    let ids = match refs(v, &[(subject.to_string(), "person", ""),
                              (object.to_string(), "person", "")]) {
        Ok(x) => x, Err(rc) => return rc };
    let (sid, oid) = (ids[0].clone(), ids[1].clone());
    let skind = sid.split(':').next().unwrap_or("").to_string();
    v.upsert(&sid, &skind, "", "", "", &[], &[Edge { rel: rel.into(), to: oid.clone() }], &today());
    if !inverse.is_empty() {
        let okind = oid.split(':').next().unwrap_or("").to_string();
        v.upsert(&oid, &okind, "", "", "", &[],
                 &[Edge { rel: inverse.into(), to: sid.clone() }], &today());
    }
    regen(v);
    out!("ok {sid} --{rel}--> {oid}");
    0
}

/// A standing preference or instruction. Restated, it is the same node.
fn cmd_pref(v: &Vault, whose: &str, summary: &str, body: &str, kind: &str,
            about: &str, new: bool) -> i32 {
    let summary = match summary_or_die(summary, "--summary") { Ok(x) => x, Err(rc) => return rc };
    let mut wanted = vec![(whose.to_string(), "person", "person")];
    if !about.is_empty() {
        wanted.push((about.to_string(), "topic", ""));
    }
    let ids = match refs(v, &wanted) { Ok(x) => x, Err(rc) => return rc };
    let whose_id = ids[0].clone();
    let about_id = if about.is_empty() { String::new() } else { ids[1].clone() };
    let found = if new { None } else {
        match existing(v, "preference", &summary, "", &whose_id, false) {
            Ok(x) => x, Err(rc) => return rc }
    };
    // a kind given is written; none given is "preference" for a new rule and
    // nothing at all for a restated one, which keeps what it has. Before this
    // the flag defaulted to "preference" and a restate without it — the common
    // case, since three rules in four are something else — silently
    // reclassified the rule. The flag is an Option so that the help says
    // nothing false: a rendered default of "" was the one thing a session
    // could read about the flag, and it was an artefact of the fix.
    let ptype = if !kind.is_empty() { kind.to_string() }
        else if found.is_none() { "preference".to_string() }
        else { String::new() };
    let nid = found.unwrap_or_else(|| v.mint("preference", "", None, &summary));
    let mut edges = vec![Edge { rel: "whose".into(), to: whose_id }];
    if !about_id.is_empty() {
        edges.push(Edge { rel: "concerns".into(), to: about_id });
    }
    v.upsert(&nid, "preference", &summary, &summary, body,
             &[("ptype".to_string(), ptype)], &edges, &today());
    regen(v);
    ok_stored(v, &nid);
    0
}

fn cmd_trajectory(v: &Vault, summary: &str, body: &str, expect: &str, by: &str,
                  about: &str, new: bool) -> i32 {
    let summary = match summary_or_die(summary, "--summary") { Ok(x) => x, Err(rc) => return rc };
    if !new {
        match existing(v, "trajectory", &summary, "", "", true) {
            Ok(Some(dup)) => {
                out!("(already open as {dup}; `mem advance` moves it, --new opens a second)");
                return 1;
            }
            Err(rc) => return rc,
            _ => {}
        }
    }
    let wanted: Vec<(String, &str, &str)> = list_of(about).into_iter()
        .map(|a| (a, "thing", "")).collect();
    let about_ids = match refs(v, &wanted) { Ok(x) => x, Err(rc) => return rc };
    let by = match date_or_die(by, "--by") { Ok(x) => x, Err(rc) => return rc };
    let distance = by_from_today(&by);
    let nid = v.mint("trajectory", "", None, &summary);
    let edges: Vec<Edge> = about_ids.into_iter()
        .map(|a| Edge { rel: "involves".into(), to: a }).collect();
    v.upsert(&nid, "trajectory", &summary, &summary, body,
             &[("expect".into(), expect.to_string()), ("expect_by".into(), by),
               ("status".into(), "open".into())],
             &edges, &today());
    regen(v);
    ok_stored(v, &nid);
    if let Some(line) = distance {
        out!("{line}");
    }
    0
}

/// Move an existing trajectory rather than opening a second one for the same
/// thing.
fn cmd_advance(v: &Vault, r: &str, status: &str, by: &str, note: &str) -> i32 {
    let nid = match resolve(v, r) {
        Ok(Some(n)) if n.starts_with("trajectory:") => n,
        Err(rc) => return rc,
        _ => { out!("(no trajectory for {})", py_repr(r)); return 1; }
    };
    let mut extra: Vec<(String, String)> = Vec::new();
    if !status.is_empty() {
        extra.push(("status".into(), status.to_string()));
    }
    if !by.is_empty() {
        match date_or_die(by, "--by") {
            Ok(d) => extra.push(("expect_by".into(), d)),
            Err(rc) => return rc,
        }
    }
    if status == "closed" {
        extra.push(("closed".into(), today()));
    }
    v.upsert(&nid, "trajectory", "", "", note, &extra, &[], &today());
    regen(v);
    out!("ok {nid} {}", if status.is_empty() { "noted" } else { status });
    let (_, body) = stored(v, &nid);
    let held = memory::text::split_lines(&body);
    let mut shown: BTreeSet<&str> = BTreeSet::new();
    for line in memory::text::split_lines(note) {
        let line = line.trim_end();
        if !line.trim().is_empty() && held.contains(&line) && shown.insert(line) {
            out!("  {line}");
        }
    }
    if let Some(line) = by_from_today(by) {
        out!("{line}");
    }
    0
}

/// A new name, or a new summary, on the same node. The id stays, so nothing
/// else changes; what it used to say is kept in the body, struck.
fn cmd_rename(v: &Vault, node: &str, name: &str, summary: &str, because: &str) -> i32 {
    let nid = match resolve(v, node) {
        Ok(Some(n)) => n,
        Ok(None) => { out!("(no node for {})", py_repr(node)); return 1; }
        Err(rc) => return rc,
    };
    let name = one_line(name);
    let summary = one_line(summary);
    if name.is_empty() && summary.is_empty() {
        out!("(no new name and no --summary; rename takes one or both)");
        return 1;
    }
    let to_own_label = name.to_lowercase() == memory::SELF_LABEL;
    if v.me().as_deref() == Some(nid.as_str()) {
        // the standing texts name her node "me"; under another label every
        // index would show her as someone else
        if !name.is_empty() && !to_own_label {
            out!("({nid} is my own node; the one name it takes is '{}', and its summary \
                  can change)", memory::SELF_LABEL);
            return 1;
        }
    } else if to_own_label && nid.starts_with("person:") {
        return not_mine(v);
    }
    for (text, what) in [(&name, "name"), (&summary, "--summary")] {
        let n = one_line(text).chars().count();
        if !text.is_empty() && n > memory::SUMMARY_MAX {
            out!("({what} is {n} characters; the cap is {}. It is what every index \
                      shows: the thing in a phrase.)", memory::SUMMARY_MAX);
            return 1;
        }
    }
    v.rename(&nid, &name, &summary, because, &today());
    regen(v);
    let kind = nid.split(':').next().unwrap_or("");
    let named = memory::fm::ENTITY_KINDS.contains(&kind);
    let (meta, _) = stored(v, &nid);
    let mut out = format!("ok {nid}");
    if named && !name.is_empty() {
        out += &format!(" now named {}", py_repr(meta.get("name")));
    }
    if !summary.is_empty() || !named {
        out += &format!(" now summarised {}", py_repr(meta.get("summary")));
    }
    if !named && !name.is_empty() && !summary.is_empty() {
        let a = if kind.starts_with(['a', 'e', 'i', 'o', 'u']) { "an" } else { "a" };
        out += &format!(" ({a} {kind} is named by its summary; the name given was not kept)");
    }
    out!("{out}");
    0
}

/// Remove a node that should never have existed — a person minted for a place
/// name by a relate that misread it — rather than rename it to "stray node" and
/// leave it in every index. Refused while anything links to it: unlink first,
/// so nothing is left dangling.
fn cmd_forget(v: &Vault, r: &str) -> i32 {
    let nid = match resolve(v, r) {
        Ok(Some(n)) => n,
        Ok(None) => { out!("(no node for {})", py_repr(r)); return 1; }
        Err(rc) => return rc,
    };
    // once her node is gone, a person labelled "wanda" would be taken for her
    if v.me().as_deref() == Some(nid.as_str()) {
        out!("({nid} is my own node; it stays)");
        return 1;
    }
    let back: Vec<(String, String)> = match index::build_index(v, &v.root.join(".index.db")) {
        Ok(con) => {
            let mut stmt = con.prepare("SELECT src, rel FROM edges WHERE dst=?").unwrap();
            let out = stmt.query_map([&nid], |x| Ok((x.get(0)?, x.get(1)?))).unwrap()
                .collect::<Result<Vec<_>, _>>().unwrap_or_default();
            out
        }
        Err(_) => Vec::new(),
    };
    if !back.is_empty() {
        let shown: Vec<String> = back.iter().take(6)
            .map(|(s, r)| format!("{s} --{r}-->")).collect();
        out!("({nid} is still linked from {}; `mem forget` takes it once those are retracted.)",
             shown.join("; "));
        return 1;
    }
    let _ = std::fs::remove_file(v.path_for(&nid));
    regen(v);
    out!("ok forgot {nid}");
    0
}

/// Something recorded that turns out never to have been true. An edge is
/// removed — from both ends, when the inverse is named — and a body line is
/// struck in place, since there the sentence is the record. Nothing is written
/// about the removal: what is true now is recorded as a fact.
fn cmd_retract(v: &Vault, subject: &str, rel: &str, object: &str, inverse: &str,
               line: &str, because: &str) -> i32 {
    let nid = match resolve(v, subject) {
        Ok(Some(n)) => n,
        Ok(None) => { out!("(no node for {})", py_repr(subject)); return 1; }
        Err(rc) => return rc,
    };
    let oid = if object.is_empty() { String::new() } else {
        match resolve(v, object) {
            Ok(Some(n)) => n,
            Ok(None) => { out!("(no node for {})", py_repr(object)); return 1; }
            Err(rc) => return rc,
        }
    };
    let why = if because.is_empty() {
        format!(" (retracted {})", today())
    } else {
        format!(" (retracted {}: {because})", today())
    };
    let mut pairs = vec![(nid.clone(), rel.to_string(), oid.clone())];
    if !inverse.is_empty() && !oid.is_empty() {
        pairs.push((oid.clone(), inverse.to_string(), nid.clone()));
    }
    let mut hit = 0usize;
    for (src, rel, dst) in pairs {
        let path = v.path_for(&src);
        let Ok(text) = std::fs::read_to_string(&path) else { continue };
        let (mut meta, body) = memory::fm::load(&text, Some(&v.root));
        let before = meta.edges.len();
        meta.edges.retain(|e| !(!rel.is_empty() && e.rel == rel
                                && (dst.is_empty() || e.to == dst)));
        hit += before - meta.edges.len();
        let mut lines: Vec<String> = memory::text::split_lines(&body)
            .into_iter().map(|l| l.to_string()).collect();
        if !line.is_empty() {
            let needle = line.to_lowercase();
            lines = lines.into_iter().map(|l| {
                if l.to_lowercase().contains(&needle) && !l.starts_with("~~") {
                    hit += 1;
                    format!("~~{l}~~{why}")
                } else {
                    l
                }
            }).collect();
        }
        let kept: Vec<String> = lines.into_iter().filter(|l| !py_strip(l).is_empty()).collect();
        let kind = src.split(':').next().unwrap_or("").to_string();
        let _ = std::fs::write(&path,
            format!("{}\n\n{}\n", memory::fm::dump(&meta, &kind,
                &memory::fm::former_names(&kept.join("\n"))), kept.join("\n")));
    }
    if hit == 0 {
        // ok here would be a silent success: nothing matched, so nothing was unsaid
        out!("(nothing matched, nothing retracted)");
        return 1;
    }
    regen(v);
    out!("ok retracted {hit}");
    0
}

/// An exchange, or a list of them, from the transcripts Claude Code keeps. This
/// is the belt: what was said, both sides, and what wanda did about it, for as
/// long as the transcripts last. Nothing in the vault duplicates it; a node's
/// `made:` points here.
fn cmd_session(v: &Vault, r: &str, day: &str, with_: &str, last: i64, full: bool) -> i32 {
    if !r.is_empty() {
        let Some(p) = transcript::find(&v.root, r) else {
            out!("(no session {}: the transcript is gone, or the id is not one)", py_repr(r));
            return 1;
        };
        let ex = transcript::load(&p);
        let in_progress = !v.session.is_empty() && ex.session == v.session && !ex.answered();
        out!("{}", transcript::render(&ex, full, in_progress));
        return 0;
    }
    let mut exchanges = transcript::load_all(&v.root);
    // unanswered, the caller's own exchange reads as an earlier one left silent
    exchanges.retain(|e| v.session.is_empty() || e.session != v.session);
    if !day.is_empty() {
        exchanges.retain(|e| e.date == day);
    }
    let on_that_day = exchanges.len();
    if !with_.is_empty() {
        let w = with_.to_lowercase();
        exchanges.retain(|e| e.speaker.to_lowercase().contains(&w));
    }
    if last > 0 {
        let keep = exchanges.len().saturating_sub(last as usize);
        exchanges = exchanges.split_off(keep);
    }
    if exchanges.is_empty() {
        // which filter emptied it, and no more than that: the days a store does
        // hold would tell a caller asking about the wrong one why it is wrong
        if !day.is_empty() && on_that_day == 0 {
            out!("(nothing on {day})");
        } else {
            out!("(no exchanges match)");
        }
        return 1;
    }
    for e in &exchanges {
        out!("{}", transcript::line(e));
    }
    0
}

fn regen(v: &Vault) {
    let _ = index::regenerate_indexes(v);
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    // --help and a malformed call end in clap, and both are calls to log
    let cli = match Cli::try_parse() {
        Ok(c) => c,
        Err(e) => {
            // clap ends every refusal with an order to the reader, and nothing
            // mem prints addresses her
            let text = e.render().to_string()
                .replace("For more information, try '--help'.", "'--help' says more.");
            let wrote = if e.use_stderr() {
                write!(std::io::stderr(), "{text}")
            } else {
                write!(std::io::stdout(), "{text}")
            };
            if wrote.is_err() {
                CUT.store(true, Ordering::Relaxed);
            }
            let rc = e.exit_code();
            let verb = argv.first().map(String::as_str).unwrap_or("");
            let known = <Cli as clap::CommandFactory>::command()
                .get_subcommands().any(|c| c.get_name() == verb);
            log(if rc == 0 { "help" } else if known { verb } else { "" }, rc, &argv);
            std::process::exit(rc);
        }
    };
    let v = vault();
    // one choke point rather than per-command: every string a session passes
    // goes through restamp, so no free-text field carries a stray date past it
    let cmd = match cli.cmd {
        // the Python scrubs `isinstance(val, str)`, and recall's refs is a
        // list, so it is the one string argument the scrub never reaches
        Cmd::Recall { refs, hops, limit } => Cmd::Recall { refs, hops, limit },
        Cmd::Search { text, limit } => Cmd::Search { text: restamp(&text), limit },
        Cmd::Show { r#ref } => Cmd::Show { r#ref: restamp(&r#ref) },
        // `--by` is exempt: a deadline somebody stated can fall on the clock's
        // date, and one counted from the clock lands after it, out of an exact
        // match's reach. The verbs that take it say how far from today it lies
        Cmd::Entity { kind, name, summary, body, new, id } => Cmd::Entity {
            kind: restamp(&kind), name: restamp(&name), summary: restamp(&summary),
            body: restamp(&body), new, id: restamp(&id) },
        Cmd::Event { summary, body, when, participants, place, new } => Cmd::Event {
            summary: restamp(&summary), body: restamp(&body), when: restamp(&when),
            participants: restamp(&participants), place: restamp(&place), new },
        Cmd::Relate { subject, rel, object, inverse } => Cmd::Relate {
            subject: restamp(&subject), rel: restamp(&rel),
            object: restamp(&object), inverse: restamp(&inverse) },
        Cmd::Pref { whose, summary, body, kind, about, new } => Cmd::Pref {
            whose: restamp(&whose), summary: restamp(&summary), body: restamp(&body),
            kind: kind.map(|k| restamp(&k)), about: restamp(&about), new },
        Cmd::Trajectory { summary, body, expect, by, about, new } => Cmd::Trajectory {
            summary: restamp(&summary), body: restamp(&body), expect: restamp(&expect),
            by, about: restamp(&about), new },
        Cmd::Advance { r#ref, status, by, note } => Cmd::Advance {
            r#ref: restamp(&r#ref), status: restamp(&status), by, note: restamp(&note) },
        Cmd::Rename { node, name, summary, because } => Cmd::Rename {
            node: restamp(&node), name: restamp(&name),
            summary: restamp(&summary), because: restamp(&because) },
        Cmd::Forget { r#ref, because } => Cmd::Forget {
            r#ref: restamp(&r#ref), because: restamp(&because) },
        Cmd::Retract { subject, rel, object, inverse, line, because } => Cmd::Retract {
            subject: restamp(&subject), rel: restamp(&rel), object: restamp(&object),
            inverse: restamp(&inverse), line: restamp(&line), because: restamp(&because) },
        Cmd::Session { r#ref, day, with_, last, full } => Cmd::Session {
            r#ref: restamp(&r#ref), day: restamp(&day), with_: restamp(&with_), last, full },
        c => c,
    };
    let name = cmd.name();
    let rc = match &cmd {
        Cmd::Recall { refs, hops, limit } => cmd_recall(&v, refs, *hops, *limit),
        Cmd::Search { text, limit } => cmd_search(&v, text, *limit),
        Cmd::Show { r#ref } => cmd_show(&v, r#ref),
        Cmd::Entity { kind, name, summary, body, new, id } =>
            cmd_entity(&v, kind, name, summary, body, *new, id),
        Cmd::Event { summary, body, when, participants, place, new } =>
            cmd_event(&v, summary, body, when, participants, place, *new),
        Cmd::Relate { subject, rel, object, inverse } =>
            cmd_relate(&v, subject, rel, object, inverse),
        Cmd::Pref { whose, summary, body, kind, about, new } =>
            cmd_pref(&v, whose, summary, body, kind.as_deref().unwrap_or(""), about, *new),
        Cmd::Trajectory { summary, body, expect, by, about, new } =>
            cmd_trajectory(&v, summary, body, expect, by, about, *new),
        Cmd::Advance { r#ref, status, by, note } => cmd_advance(&v, r#ref, status, by, note),
        Cmd::Rename { node, name, summary, because } =>
            cmd_rename(&v, node, name, summary, because),
        Cmd::Forget { r#ref, .. } => cmd_forget(&v, r#ref),
        Cmd::Retract { subject, rel, object, inverse, line, because } =>
            cmd_retract(&v, subject, rel, object, inverse, line, because),
        Cmd::Session { r#ref, day, with_, last, full } =>
            cmd_session(&v, r#ref, day, with_, *last, *full),
        // what the root instructions tell a session to run
        Cmd::Help => { let _ = <Cli as clap::CommandFactory>::command().print_help(); out!(""); 0 }
    };
    log(name, rc, &argv);
    std::process::exit(rc);
}
