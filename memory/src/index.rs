//! The derived surfaces: the SQLite index, and the generated `CLAUDE.md` files.
//!
//! Nothing here is the truth. The vault is, and all of this is rebuilt from it
//! on every call that needs it, so a file edited by hand is picked up and a
//! stale index cannot outlive the thing it indexes.

use crate::fm;
use crate::text::{clip, line_for, live_body, local_id, marks, split_lines};
use crate::vault::Vault;
use rusqlite::Connection;
use std::path::{Path, PathBuf};

pub const INDEX_CAP_LINES: usize = 200;

const SCHEMA: &str = "
CREATE TABLE nodes(id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT,
                   created TEXT, last_seen TEXT, body TEXT, status TEXT,
                   made TEXT, summary TEXT);
CREATE TABLE edges(src TEXT, rel TEXT, dst TEXT);
CREATE INDEX ix_edges_src ON edges(src); CREATE INDEX ix_edges_dst ON edges(dst);
CREATE VIRTUAL TABLE fts USING fts5(id UNINDEXED, text);
";

/// Where the texts that ship into a vault are read from.
///
/// Beside the executable, which is where a session's container is given them,
/// and `MEM_TEMPLATES` otherwise — on a host checkout the binary sits in a
/// build directory and the product's own copy is elsewhere.
/// Where a caller that is not a binary of its own ships its texts from. The
/// Python module's `current_exe` is the interpreter, whose directory says
/// nothing about where the module was loaded from.
pub fn set_templates(dir: PathBuf) {
    let _ = BESIDE.set(dir);
}

static BESIDE: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();

pub fn templates() -> PathBuf {
    if let Some(d) = BESIDE.get() {
        return d.clone();
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            let beside = d.join("templates");
            if beside.is_dir() {
                return beside;
            }
        }
    }
    PathBuf::from(std::env::var("MEM_TEMPLATES").unwrap_or_else(|_| "templates".into()))
}

pub fn template(name: &str) -> String {
    let p = templates().join(format!("{name}.md"));
    // a template that cannot be read would otherwise write an empty standing
    // instruction or an empty skill, which reads as a store that says nothing
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!(
        "cannot read the {name} template at {}: {e}. MEM_TEMPLATES names the \
         directory when the texts are not beside the binary.", p.display()))
}

/// Build the index from the vault, in the order the files sort in. That order
/// reaches the generated index lines: rows are sorted by date afterwards, and
/// the sort is stable, so everything of one date keeps the order it went in.
pub fn build_index(vault: &Vault, db_path: &Path) -> rusqlite::Result<Connection> {
    let _ = std::fs::remove_file(db_path);
    let con = Connection::open(db_path)?;
    con.execute_batch(SCHEMA)?;
    // one transaction for the whole build. Left to autocommit, every insert
    // is its own transaction and fsyncs, which for a store of this size is a
    // few hundred of them per call and the difference between a command that
    // takes a fifth of a second and one that takes over a second.
    con.execute_batch("BEGIN")?;
    for n in vault.nodes() {
        let body = live_body(&n.body);
        // a node from before summaries — an unmigrated store, or a stub — is
        // shown as its old index line showed it: the body's first live line
        let summary = if n.meta.get("summary").is_empty() {
            clip(split_lines(&body).into_iter().find(|l| !l.trim().is_empty()).unwrap_or(""),
                 crate::SUMMARY_MAX)
        } else {
            n.meta.get("summary").to_string()
        };
        // newest first, per kind: a person by when they last came up, an event
        // by when it happened, anything else by when it was written
        let seen = match (n.meta.get("last_seen"), fm::event_date(&n.id)) {
            (ls, _) if !ls.is_empty() => ls.to_string(),
            (_, ed) if !ed.is_empty() => ed,
            _ => n.meta.get("created").to_string(),
        };
        let label = fm::label(&n.meta);
        let path = vault.path_for(&n.id);
        con.execute(
            "INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?)",
            rusqlite::params![
                &n.id, n.kind(), &label, path.to_string_lossy(),
                n.meta.get("created"), &seen, body.trim(),
                n.meta.get("status"), n.meta.get("made"), &summary],
        )?;
        // the names this node used to have go in the searchable text too, so a
        // session that knows it by the name it had last month finds the node
        // rather than the other nodes that happen to mention that name. `body`
        // is the live body, with struck lines stripped, so without this the
        // former name is searchable nowhere.
        let former = crate::fm::former_names(&n.body).join(" ");
        con.execute("INSERT INTO fts VALUES(?,?)",
            rusqlite::params![&n.id,
                format!("{label} {} {body} {former}", n.meta.get("summary"))])?;
        for e in &n.meta.edges {
            if !e.to.is_empty() {
                let rel = if e.rel.is_empty() { "related" } else { &e.rel };
                con.execute("INSERT INTO edges VALUES(?,?,?)",
                    rusqlite::params![&n.id, rel, &e.to])?;
            }
        }
    }
    con.execute_batch("COMMIT")?;
    Ok(con)
}

/// Nodes by kind, and how many edges — the two numbers a run's report carries
/// about the store it ended with.
pub fn counts(vault: &Vault) -> rusqlite::Result<(Vec<(String, i64)>, i64)> {
    let con = build_index(vault, &vault.root.join(".index.db"))?;
    let mut stmt = con.prepare("SELECT kind, count(*) FROM nodes GROUP BY kind")?;
    let by_kind: Vec<(String, i64)> = stmt
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    let edges: i64 = con.query_row("SELECT count(*) FROM edges", [], |r| r.get(0))?;
    Ok((by_kind, edges))
}

/// An edge pointing at an id no node has. Writing one is silent — every `mem`
/// write reports ok — so anything that reads the graph should say so.
pub fn dangling_edges(con: &Connection) -> rusqlite::Result<Vec<(String, String, String)>> {
    let mut stmt = con.prepare(
        "SELECT src, rel, dst FROM edges WHERE dst NOT IN (SELECT id FROM nodes)")?;
    let out = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect();
    out
}

/// Every CLAUDE.md is generated. The root carries the standing instructions and
/// a map; each directory carries one line per node, newest first, capped.
pub fn regenerate_indexes(vault: &Vault) -> rusqlite::Result<()> {
    let con = build_index(vault, &vault.root.join(".index.db"))?;
    // the select has no order, so rows come back in the order they went in
    let mut stmt = con.prepare("SELECT id, kind, name, summary, last_seen, status FROM nodes")?;
    let rows: Vec<(String, String, String, String, String, String)> = stmt
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))?
        .collect::<rusqlite::Result<_>>()?;

    // insertion-ordered: the directory sections are written in the order the
    // kinds first appeared, and only the root map is sorted
    let mut dirs: indexmap::IndexMap<String, Vec<(String, String, String, String, String)>> =
        indexmap::IndexMap::new();
    for (nid, kind, name, summary, last_seen, status) in rows {
        let d = fm::dir_for(&kind).map(|s| s.to_string()).unwrap_or(kind);
        dirs.entry(d).or_default().push((nid, name, summary, last_seen, status));
    }

    let mut lines = vec![template("root").trim_end().to_string(), String::new()];
    let mut names: Vec<&String> = dirs.keys().collect();
    names.sort();
    for d in names {
        let rows = &dirs[d];
        let open_n = rows.iter().filter(|r| r.4 == "open").count();
        let extra = if open_n > 0 { format!(", {open_n} open") } else { String::new() };
        lines.push(format!("- `{d}/` \u{2014} {}{extra}", rows.len()));
    }
    lines.push(String::new());
    lines.push("Nothing else loads until I read a file in its directory.".into());
    let _ = std::fs::write(vault.root.join("CLAUDE.md"), lines.join("\n") + "\n");

    for (d, rows) in dirs.iter_mut() {
        let p = vault.root.join(d);
        let _ = std::fs::create_dir_all(&p);
        // stable and descending, so everything of one date keeps the order the
        // files sorted in
        rows.sort_by(|a, b| b.3.cmp(&a.3));
        let mut out = vec![format!("# {d}"), String::new(),
                           format!("{} here. One line each, newest first.", rows.len()),
                           String::new()];
        // the kind is the directory, so the id is shown without it; the line is
        // the name and the summary, and nothing from the body — a body is what
        // `mem show` is for
        for (nid, name, summary, _, status) in rows.iter().take(INDEX_CAP_LINES) {
            out.push(format!("- `{}`{}  {}", local_id(nid), marks(status), line_for(name, summary)));
        }
        if rows.len() > INDEX_CAP_LINES {
            out.push(format!("- \u{2026} {} more; `mem search` finds them", rows.len() - INDEX_CAP_LINES));
        }
        let _ = std::fs::write(p.join("CLAUDE.md"), out.join("\n") + "\n");
    }
    Ok(())
}

/// The one node a vault starts with: wanda herself, labelled `me`, and its id.
///
/// What she said and did is not marked on a node — the notes are hers and she
/// writes her own acts in the first person. The only edges to this node are
/// from her own commitments, which are few and are what she must be held to;
/// an edge from everything she ever said would make her the best-connected
/// node in the store within a month and warp every recall through her.
///
/// A vault that has her node already is left as it is, whichever of her names
/// it is labelled with: both resolve to it.
pub fn seed(vault: &Vault, date: &str) -> String {
    if let Some(nid) = vault.me() {
        return nid;
    }
    // a rebuild takes the id the original store gave her node, whichever
    // label it had there, so recorded calls that name that id still land
    let nid = vault.oracle.as_ref().and_then(|o| o.me()).filter(|n| !vault.exists(n))
        .unwrap_or_else(|| vault.mint("person", "", None, crate::SELF_LABEL));
    let name = crate::SELF_NAME;
    vault.upsert(
        &nid, "person", crate::SELF_LABEL,
        &format!("my name is {name}, the assistant keeping this memory; linked only \
                  from my own commitments"),
        &format!("My name is {name}, the assistant keeping this memory. These notes \
                  are mine, in my own voice: what I did, I write in the first person, \
                  with \"I\" as the one who did it. Linked only from my own \
                  commitments, so I am not the hub of all I have touched."),
        &[], &[], date,
    );
    nid
}

/// One colour per top-level directory. Taken from the vault rather than from a
/// list of kinds, so a directory added later is coloured too; the named ones
/// keep their colour and anything else takes one from the same palette by
/// name, so the same directory looks the same in every vault.
const GRAPH_COLOURS: [(&str, i64); 9] = [
    ("people", 0x4C8BF5), ("places", 0x2FBF71), ("orgs", 0xF5A623),
    ("groups", 0xB388FF), ("things", 0x8D6E63), ("topics", 0x26C6DA),
    ("events", 0xEF5350), ("prefs", 0xFFD54F), ("trajectories", 0xAB47BC),
];
/// spare hues, for a directory the map does not name
const SPARE_COLOURS: [i64; 6] = [0x66BB6A, 0x7E57C2, 0xFF7043, 0x29B6F6, 0xD4E157, 0xEC407A];

/// Obsidian's graph settings: a colour per top-level directory, and the
/// generated indexes left out — an index that links to everything is a hub
/// that says nothing about the memory.
///
/// Obsidian owns this file and writes its own state over it, so this is
/// written whenever a vault is built, and a vault already open in Obsidian has
/// to be reloaded before the settings take. Everything else in the file is
/// left as it was found.
pub fn write_graph_config(root: &Path) -> Vec<String> {
    let mut dirs: Vec<String> = match std::fs::read_dir(root) {
        Ok(rd) => rd.flatten()
            .filter(|e| e.path().is_dir())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|n| !n.starts_with('.'))
            .collect(),
        Err(_) => return Vec::new(),
    };
    dirs.sort();
    let named = |d: &str| GRAPH_COLOURS.iter().find(|(k, _)| *k == d).map(|(_, c)| *c);
    let taken: Vec<i64> = dirs.iter().filter_map(|d| named(d)).collect();
    // a directory the map does not name takes the first spare colour no other
    // directory here is using, so two of them never come out the same
    let mut spare: Vec<i64> = SPARE_COLOURS.iter().copied()
        .chain(GRAPH_COLOURS.iter().map(|(_, c)| *c))
        .filter(|c| !taken.contains(c))
        .collect();
    let mut colour: Vec<(String, i64)> = Vec::new();
    for d in &dirs {
        let c = match named(d) {
            Some(c) => c,
            None if !spare.is_empty() => spare.remove(0),
            None => SPARE_COLOURS[colour.len() % SPARE_COLOURS.len()],
        };
        colour.push((d.clone(), c));
    }
    let cfg = root.join(".obsidian");
    let _ = std::fs::create_dir_all(&cfg);
    let graph = cfg.join("graph.json");
    let mut settings: serde_json::Map<String, serde_json::Value> =
        std::fs::read_to_string(&graph).ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or_default();
    settings.insert("search".into(), "-file:CLAUDE".into());
    settings.insert("colorGroups".into(), serde_json::Value::Array(
        colour.iter().map(|(d, c)| serde_json::json!({
            "query": format!("path:{d}/"), "color": {"a": 1, "rgb": c}
        })).collect()));
    settings.entry("showTags").or_insert(false.into());
    settings.entry("showAttachments").or_insert(false.into());
    let _ = std::fs::write(&graph,
        serde_json::to_string_pretty(&serde_json::Value::Object(settings)).unwrap_or_default() + "\n");
    dirs
}
