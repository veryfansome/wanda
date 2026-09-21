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

#[derive(Parser)]
#[command(name = "mem", about = "read and write wanda's memory", disable_help_subcommand = true)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// expand from things you have identified
    Recall {
        #[arg(required = true)]
        refs: Vec<String>,
        #[arg(long, default_value_t = HOPS)]
        hops: i64,
        #[arg(long, default_value_t = LIMIT)]
        limit: i64,
    },
    /// full text, when you do not know the name
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
        /// the label; the same name is the same node
        #[arg(long, required = true)]
        name: String,
        #[arg(long, default_value = "")]
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
        #[arg(long, required = true)]
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
        #[arg(long, required = true)]
        summary: String,
        #[arg(long, default_value = "")]
        body: String,
        #[arg(long, default_value = "preference",
              value_parser = ["mail-disposition","preference","etiquette"])]
        kind: String,
        #[arg(long, default_value = "")]
        about: String,
        #[arg(long)]
        new: bool,
    },
    /// open something mid-sequence
    Trajectory {
        #[arg(long, required = true)]
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
        #[arg(default_value = "")]
        name: String,
        /// a new summary — the index line — instead of or as well as a new name
        #[arg(long, default_value = "")]
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
    /// an exchange from the transcripts: what was said both ways, and what you did
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

/// What was actually asked of memory, recorded by the thing being asked.
/// Reconstructing it from the outside undercounts: several `mem` calls chain
/// into one shell command, and only the first is visible there. Never fails the
/// command.
fn log(cmd: &str, rc: i32, argv: &[String]) {
    let Ok(path) = std::env::var("LAB_MEMLOG") else { return };
    if path.is_empty() {
        return;
    }
    let rec = serde_json::json!({
        "ts": iso_now(),
        "input_key": std::env::var("LAB_INPUT").unwrap_or_default(),
        // the date and session this call ran under, so a rebuild reads them
        // rather than recovering them from the key's shape
        "date": std::env::var("MEM_DATE").unwrap_or_default(),
        "session": std::env::var("MEM_SESSION").unwrap_or_default(),
        "cmd": cmd, "rc": rc, "argv": argv,
    });
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
            println!("({a}. Say which, by id.)");
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
            Err(a) => { complaints.insert(format!("({a}. Say which, by id.)")); }
        }
    }
    // sorted, so that what a session reads is the same whichever order it named
    // the refs in — the two streams are merged by the tool it runs this with
    for c in &complaints {
        eprintln!("{c}");
    }
    if seeds.is_empty() {
        println!("nothing to expand from");
        return 1;
    }
    let rows = match recall::walk(&con, &seeds, hops) {
        Ok(r) => r,
        Err(_) => return 1,
    };
    if let Some(w) = recall::dangling_warning(&con) {
        eprint!("{w}");
    }
    println!("expanded from {}: {}\n", seeds.len(),
             seeds.iter().cloned().collect::<Vec<_>>().join(", "));
    for r in recall::take_limit(&rows, limit) {
        println!("{}", r.line());
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
        println!("`{nid}`{}  {}", marks(status), line_for(name, summary));
    }
    if rows.is_empty() {
        println!("(nothing)");
    }
    0
}

fn cmd_show(v: &Vault, r: &str) -> i32 {
    let nid = match resolve(v, r) {
        Ok(Some(n)) => n,
        Ok(None) => { println!("(no node for {})", py_repr(r)); return 1; }
        Err(rc) => return rc,
    };
    let p = v.path_for(&nid);
    // the id is the path, not a line in the file; said here so a session
    // reading this has it to copy
    println!("{nid}\n{}", std::fs::read_to_string(&p).unwrap_or_default());
    let Ok(con) = index::build_index(v, &v.root.join(".index.db")) else { return 0 };
    let back: Vec<(String, String)> = (|| {
        let mut stmt = con.prepare("SELECT src, rel FROM edges WHERE dst=?").ok()?;
        let out = stmt.query_map([&nid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?
            .collect::<Result<Vec<_>, _>>().ok()?;
        Some(out)
    })().unwrap_or_default();
    if !back.is_empty() {
        println!("referred to by:");
        for (src, rel) in back {
            println!("  {src} --{rel}-->");
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
        println!("({flag} is empty)");
        return Err(1);
    }
    let n = t.chars().count();
    if n > memory::SUMMARY_MAX {
        println!("({flag} is {n} characters; the cap is {}. It is the index line \
                  — say the thing in a phrase, and put the rest in --body.)",
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
        println!("({flag} must be a date, YYYY-MM-DD; got {}. It says when, not who.)",
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
        println!("({} is already more than one {kind}: {}. Say which, by id — \
                  `--id <id>` on entity, the id itself elsewhere — or --new for another.)",
                 py_repr(name), listed.join("; "));
        return Err(1);
    }
    Ok(hits.into_iter().next().map(|(id, _)| id))
}

fn stub(v: &Vault, name: &str, kind: &str) -> String {
    let nid = v.mint(kind, "", None, name);
    v.upsert(&nid, kind, name, "", "", &[], &[], &today());
    nid
}

/// (reference, kind to mint, kind to prefer) → node ids.
///
/// A name nobody has recorded gets a stub — an edge to a node nobody created is
/// a dangling edge, invisible until traversal quietly returns nothing. Every
/// reference is resolved before any stub is minted, so a refusal on the last
/// leaves nothing behind from the first; the same new name twice gets one stub.
/// An id that names no node is refused: there is nothing to guess from a
/// mistyped hash, and minting a person called `34432f` is worse.
fn refs(v: &Vault, wanted: &[(String, &str, &str)]) -> Result<Vec<String>, i32> {
    enum Item { Have(String), Mint(String, String) }
    let mut out: Vec<Item> = Vec::new();
    for (r, mint_kind, prefer) in wanted {
        if py_strip(r).is_empty() {
            println!("(a blank where a name was expected)");
            return Err(1);
        }
        let nid = match v.resolve(r, prefer) {
            Ok(x) => x,
            Err(a) => { println!("({a}. Say which, by id.)"); return Err(1); }
        };
        if nid.is_none() && memory::text::id_shaped(r) {
            println!("(no node {}; give a name, or an id from an index)", py_repr(r));
            return Err(1);
        }
        out.push(match nid {
            Some(n) => Item::Have(n),
            None => Item::Mint(one_line(r), mint_kind.to_string()),
        });
    }
    let mut minted: std::collections::HashMap<(String, String), String> = Default::default();
    let mut ids = Vec::new();
    for item in out {
        match item {
            Item::Have(n) => ids.push(n),
            Item::Mint(name, kind) => {
                let key = (name.to_lowercase(), kind.clone());
                let id = minted.entry(key).or_insert_with(|| stub(v, &name, &kind)).clone();
                ids.push(id);
            }
        }
    }
    Ok(ids)
}

/// A person, place, org, group, thing or topic. The same name again is the same
/// node, updated — two files for one person is the failure that costs most —
/// unless --new says it is a second one, as with two people who share a name.
fn cmd_entity(v: &Vault, kind: &str, name: &str, summary: &str, body: &str,
              new: bool, id: &str) -> i32 {
    let name = match summary_or_die(name, "--name") { Ok(x) => x, Err(rc) => return rc };
    if memory::text::id_shaped(&name) {
        println!("(--name is the label, not an id: {}. To update a node by id, --id <id>)",
                 py_repr(&name));
        return 1;
    }
    let summary = if summary.is_empty() { String::new() } else {
        match summary_or_die(summary, "--summary") { Ok(x) => x, Err(rc) => return rc }
    };
    let nid = if !id.is_empty() {
        match resolve(v, id) {
            Ok(Some(n)) if n.starts_with(&format!("{kind}:")) => Some(n),
            Err(rc) => return rc,
            _ => { println!("(no {kind} {})", py_repr(id)); return 1; }
        }
    } else if new {
        None
    } else {
        match existing(v, kind, &name, "", "", false) { Ok(x) => x, Err(rc) => return rc }
    };
    let nid = nid.unwrap_or_else(|| v.mint(kind, "", None, &name));
    v.upsert(&nid, kind, &name, &summary, body, &[], &[], &today());
    regen(v);
    println!("ok {nid}");
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
                println!("(already here as {dup}; --new if this is a second one)");
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
    println!("ok {nid}");
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
    println!("ok {sid} --{rel}--> {oid}");
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
    let nid = found.unwrap_or_else(|| v.mint("preference", "", None, &summary));
    let mut edges = vec![Edge { rel: "whose".into(), to: whose_id }];
    if !about_id.is_empty() {
        edges.push(Edge { rel: "concerns".into(), to: about_id });
    }
    v.upsert(&nid, "preference", &summary, &summary, body,
             &[("ptype".to_string(), kind.to_string())], &edges, &today());
    regen(v);
    println!("ok {nid}");
    0
}

fn cmd_trajectory(v: &Vault, summary: &str, body: &str, expect: &str, by: &str,
                  about: &str, new: bool) -> i32 {
    let summary = match summary_or_die(summary, "--summary") { Ok(x) => x, Err(rc) => return rc };
    if !new {
        match existing(v, "trajectory", &summary, "", "", true) {
            Ok(Some(dup)) => {
                println!("(already open as {dup}; `mem advance` moves it, --new opens a second)");
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
    let nid = v.mint("trajectory", "", None, &summary);
    let edges: Vec<Edge> = about_ids.into_iter()
        .map(|a| Edge { rel: "involves".into(), to: a }).collect();
    v.upsert(&nid, "trajectory", &summary, &summary, body,
             &[("expect".into(), expect.to_string()), ("expect_by".into(), by),
               ("status".into(), "open".into())],
             &edges, &today());
    regen(v);
    println!("ok {nid}");
    0
}

/// Move an existing trajectory rather than opening a second one for the same
/// thing.
fn cmd_advance(v: &Vault, r: &str, status: &str, by: &str, note: &str) -> i32 {
    let nid = match resolve(v, r) {
        Ok(Some(n)) if n.starts_with("trajectory:") => n,
        Err(rc) => return rc,
        _ => { println!("(no trajectory for {})", py_repr(r)); return 1; }
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
    println!("ok {nid} {}", if status.is_empty() { "noted" } else { status });
    0
}

/// A new name, or a new summary, on the same node. The id stays, so nothing
/// else changes; what it used to say is kept in the body, struck.
fn cmd_rename(v: &Vault, node: &str, name: &str, summary: &str, because: &str) -> i32 {
    let nid = match resolve(v, node) {
        Ok(Some(n)) => n,
        Ok(None) => { println!("(no node for {})", py_repr(node)); return 1; }
        Err(rc) => return rc,
    };
    let name = one_line(name);
    let summary = one_line(summary);
    if name.is_empty() && summary.is_empty() {
        println!("(give a new name, or --summary, or both)");
        return 1;
    }
    for (text, what) in [(&name, "name"), (&summary, "--summary")] {
        let n = one_line(text).chars().count();
        if !text.is_empty() && n > memory::SUMMARY_MAX {
            println!("({what} is {n} characters; the cap is {}. It is what every index \
                      shows — say it in a phrase.)", memory::SUMMARY_MAX);
            return 1;
        }
    }
    v.rename(&nid, &name, &summary, because, &today());
    regen(v);
    let mut out = format!("ok {nid}");
    if !name.is_empty() {
        out += &format!(" now named {}", py_repr(&one_line(&name)));
    }
    if !summary.is_empty() {
        out += &format!(" now summarised {}", py_repr(&one_line(&summary)));
    }
    println!("{out}");
    0
}

/// Remove a node that should never have existed — a person minted for a place
/// name by a relate that misread it — rather than rename it to "stray node" and
/// leave it in every index. Refused while anything links to it: unlink first,
/// so nothing is left dangling.
fn cmd_forget(v: &Vault, r: &str) -> i32 {
    let nid = match resolve(v, r) {
        Ok(Some(n)) => n,
        Ok(None) => { println!("(no node for {})", py_repr(r)); return 1; }
        Err(rc) => return rc,
    };
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
        println!("({nid} is still linked from {}. Retract those first.)", shown.join("; "));
        return 1;
    }
    let _ = std::fs::remove_file(v.path_for(&nid));
    regen(v);
    println!("ok forgot {nid}");
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
        Ok(None) => { println!("(no node for {})", py_repr(subject)); return 1; }
        Err(rc) => return rc,
    };
    let oid = if object.is_empty() { String::new() } else {
        match resolve(v, object) {
            Ok(Some(n)) => n,
            Ok(None) => { println!("(no node for {})", py_repr(object)); return 1; }
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
        println!("(nothing matched, nothing retracted)");
        return 1;
    }
    regen(v);
    println!("ok retracted {hit}");
    0
}

/// An exchange, or a list of them, from the transcripts Claude Code keeps. This
/// is the belt: what was said, both sides, and what wanda did about it, for as
/// long as the transcripts last. Nothing in the vault duplicates it; a node's
/// `made:` points here.
fn cmd_session(v: &Vault, r: &str, day: &str, with_: &str, last: i64, full: bool) -> i32 {
    if !r.is_empty() {
        let Some(p) = transcript::find(&v.root, r) else {
            println!("(no session {}: the transcript is gone, or the id is not one)", py_repr(r));
            return 1;
        };
        println!("{}", transcript::render(&transcript::load(&p), full));
        return 0;
    }
    let mut exchanges = transcript::load_all(&v.root);
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
            println!("(nothing on {day})");
        } else {
            println!("(no exchanges match)");
        }
        return 1;
    }
    for e in &exchanges {
        println!("{}", transcript::line(e));
    }
    0
}

fn regen(v: &Vault) {
    let _ = index::regenerate_indexes(v);
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let cli = Cli::parse();
    let v = vault();
    // one choke point rather than per-command: every string a session passes
    // goes through restamp, so no free-text field carries a stray date past it
    let cmd = match cli.cmd {
        // the Python scrubs `isinstance(val, str)`, and recall's refs is a
        // list, so it is the one string argument the scrub never reaches
        Cmd::Recall { refs, hops, limit } => Cmd::Recall { refs, hops, limit },
        Cmd::Search { text, limit } => Cmd::Search { text: restamp(&text), limit },
        Cmd::Show { r#ref } => Cmd::Show { r#ref: restamp(&r#ref) },
        // `--by` and `--day` are exempt: what the scrub catches is a session
        // writing what it takes to be now, while those hold a date somebody
        // stated and the session copied
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
            kind: restamp(&kind), about: restamp(&about), new },
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
            r#ref: restamp(&r#ref), day, with_: restamp(&with_), last, full },
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
            cmd_pref(&v, whose, summary, body, kind, about, *new),
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
        Cmd::Help => { let _ = <Cli as clap::CommandFactory>::command().print_help(); println!(); 0 }
    };
    log(name, rc, &argv);
    std::process::exit(rc);
}
