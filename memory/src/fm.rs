//! A node's frontmatter: what is written, in what order, and how it reads back.
//!
//! Valid YAML, so that anything reading Markdown frontmatter reads these files.
//! Every scalar is quoted, which makes a colon, a leading dash, a bare date and
//! `yes` all come back as the text they were.

use crate::text::{local_id, one_line, split_lines};
use indexmap::IndexMap;
use regex::Regex;
use std::path::Path;
use std::sync::LazyLock;

/// Directory per kind. The order is the order a bare id is looked up in.
pub const KIND_DIR: [(&str, &str); 9] = [
    ("person", "people"), ("place", "places"), ("org", "orgs"), ("group", "groups"),
    ("thing", "things"), ("topic", "topics"), ("event", "events"),
    ("preference", "prefs"), ("trajectory", "trajectories"),
];

pub const ENTITY_KINDS: [&str; 6] = ["person", "place", "org", "group", "thing", "topic"];

/// Written by `dump` from the fields above them and never read back as such:
/// what Obsidian needs, derived every time the file is written.
pub const DERIVED: [&str; 2] = ["aliases", "tags"];

const COMMON: [&str; 4] = ["summary", "created", "made", "aka"];

/// Our own field names, past and present. A relation sharing one of them is
/// written with a prefix, so the two never meet.
const FIELDS: [&str; 22] = [
    "name", "summary", "created", "made", "last_seen", "when", "permanence", "actor",
    "status", "opened", "expect", "expect_by", "closes", "closed", "ptype",
    "stated", "aka", "edges", "id", "kind", "aliases", "tags",
];

/// What each kind carries, and nothing else: the writer drops the rest, so a
/// file from before a schema change cleans itself on its next write.
fn schema_fields(kind: &str) -> Option<Vec<&'static str>> {
    let common = COMMON.to_vec();
    let mut v = common.clone();
    if ENTITY_KINDS.contains(&kind) {
        v.extend(["name", "last_seen"]);
        return Some(v);
    }
    match kind {
        "event" => Some(common),
        "trajectory" => { v.extend(["expect", "expect_by", "status", "closed"]); Some(v) }
        "preference" => { v.push("ptype"); Some(v) }
        _ => None,
    }
}

pub fn dir_for(kind: &str) -> Option<&'static str> {
    KIND_DIR.iter().find(|(k, _)| *k == kind).map(|(_, d)| *d)
}

pub fn kind_of_dir(dir: &str) -> Option<&'static str> {
    KIND_DIR.iter().find(|(_, d)| *d == dir).map(|(k, _)| *k)
}

#[derive(Clone, Debug, PartialEq)]
pub struct Edge {
    pub rel: String,
    pub to: String,
}

/// The fields in the order the file carries them, and the edges separately.
/// Order is not cosmetic: a file keeps the order it was first written in, and
/// rewriting it in another order is a difference in every byte after the first
/// field that moved.
#[derive(Clone, Debug, Default)]
pub struct Meta {
    pub fields: IndexMap<String, String>,
    pub edges: Vec<Edge>,
}

impl Meta {
    pub fn get(&self, k: &str) -> &str {
        self.fields.get(k).map(|s| s.as_str()).unwrap_or("")
    }
    pub fn set(&mut self, k: &str, v: impl Into<String>) {
        self.fields.insert(k.to_string(), v.into());
    }
    /// As `dict.setdefault`: keeps the value and the position it already had.
    pub fn set_default(&mut self, k: &str, v: impl Into<String>) {
        self.fields.entry(k.to_string()).or_insert_with(|| v.into());
    }
}

/// A YAML double-quoted scalar. JSON strings are a subset of YAML, and this
/// matches `json.dumps(v, ensure_ascii=False)` byte for byte.
pub fn q(v: &str) -> String {
    crate::text::json_str(v)
}

/// A scalar back: a double-quoted one exactly, an unquoted one — the format
/// before quoting — as it is.
pub fn unq(v: &str) -> String {
    let v = v.trim();
    let b = v.as_bytes();
    if b.len() >= 2 && b[0] == b'"' && b[b.len() - 1] == b'"' {
        return serde_json::from_str::<String>(v)
            .unwrap_or_else(|_| v[1..v.len() - 1].to_string());
    }
    v.to_string()
}

/// What a node is called: its name if it has one, else its summary.
pub fn label(meta: &Meta) -> String {
    let n = meta.get("name");
    one_line(if n.is_empty() { meta.get("summary") } else { n })
}

static EVENT_DATE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^event:(\d{4}-\d{2}-\d{2})").unwrap());

pub fn event_date(nid: &str) -> String {
    EVENT_DATE.captures(nid).map(|c| c[1].to_string()).unwrap_or_default()
}

static NON_KEY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9_]+").unwrap());

/// The property an edge is written under: its relation, as a word.
pub fn rel_key(rel: &str) -> String {
    let rel = if rel.is_empty() { "related" } else { rel };
    let lower = rel.to_lowercase();
    let r = NON_KEY.replace_all(&lower, "_");
    let r = r.trim_matches('_');
    let r = if r.is_empty() { "related" } else { r };
    if FIELDS.contains(&r) || DERIVED.contains(&r) {
        format!("rel_{r}")
    } else {
        r.to_string()
    }
}

/// An edge is a relation and a target, written as one list property per
/// relation — `member_of: ["[[235b7e]]"]` — which is both what the store means
/// by it and what Obsidian draws: an id is unique across kinds, so the bare
/// stem resolves.
pub fn dump(meta: &Meta, kind: &str) -> String {
    let mut by_rel: IndexMap<String, Vec<String>> = IndexMap::new();
    for e in &meta.edges {
        if e.to.is_empty() || !e.to.contains(':') {
            continue;
        }
        let key = rel_key(&e.rel);
        let targets = by_rel.entry(key).or_default();
        if !targets.contains(&e.to) {
            targets.push(e.to.clone());
        }
    }
    let allowed = schema_fields(kind);
    let mut out = vec!["---".to_string()];
    for (k, v) in &meta.fields {
        // the path says id and kind; edges are written below as relations;
        // and a field the kind does not carry is dropped
        if k == "id" || k == "kind" || k == "edges"
            || DERIVED.contains(&k.as_str()) || by_rel.contains_key(k) || v.is_empty()
        {
            continue;
        }
        if let Some(a) = &allowed {
            if !a.contains(&k.as_str()) {
                continue;
            }
        }
        out.push(format!("{k}: {}", q(v)));
    }
    // an alias is for a thing with a name; a sentence is not a name
    let mut aliases: Vec<String> = Vec::new();
    if ENTITY_KINDS.contains(&kind) {
        let mut cand = vec![one_line(meta.get("name"))];
        cand.extend(meta.get("aka").split("; ").filter(|a| !a.is_empty()).map(one_line));
        for a in cand {
            if !a.is_empty() && !aliases.contains(&a) {
                aliases.push(a);
            }
        }
    }
    let mut tags: Vec<String> = vec![kind.to_string()];
    if meta.get("status") == "open" {
        tags.push("open".into());
    }
    if kind == "preference" && !meta.get("ptype").is_empty() {
        tags.push(meta.get("ptype").to_string());
    }
    tags.retain(|t| !t.is_empty());

    if !aliases.is_empty() {
        out.push(format!("aliases: [{}]",
            aliases.iter().map(|a| q(a)).collect::<Vec<_>>().join(", ")));
    }
    if !tags.is_empty() {
        out.push(format!("tags: [{}]",
            tags.iter().map(|t| q(t)).collect::<Vec<_>>().join(", ")));
    }
    for (key, targets) in &by_rel {
        let items: Vec<String> = targets.iter()
            .map(|t| q(&format!("[[{}]]", local_id(t)))).collect();
        out.push(format!("{key}: [{}]", items.join(", ")));
    }
    out.push("---".to_string());
    out.join("\n")
}

static QUOTED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#""((?:[^"\\]|\\.)*)""#).unwrap());
static LINK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\[\[([^\]|]+)(?:\|[^\]]*)?\]\]$").unwrap());
static FLOW_PAIR: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(\w+):\s*("(?:[^"\\]|\\.)*"|[^,}]*)"#).unwrap());

/// Python's `$` also matches just before a trailing newline; Rust's does not.
fn link_local(s: &str) -> Option<String> {
    let s = s.strip_suffix('\n').unwrap_or(s);
    LINK.captures(s).map(|c| c[1].trim().to_string())
}

/// `kind:local` for a bare id, by which directory holds it.
pub fn kind_for(root: &Path, local: &str) -> String {
    for (kind, d) in KIND_DIR {
        let p = root.join(d).join(format!("{local}.md"));
        // the exact name, because a case-insensitive volume answers
        // `Abc123.md` for `abc123.md` and that is not the same node
        if p.is_file() {
            if let Ok(rd) = std::fs::read_dir(root.join(d)) {
                if rd.flatten().any(|e| e.file_name() == *format!("{local}.md")) {
                    return format!("{kind}:{local}");
                }
            }
        }
    }
    String::new()
}

/// Reads what `dump` writes, and what it wrote before: an old store's `edges:`
/// block is read as the relations it holds and nothing else, and an edge it
/// had retracted is not an edge.
pub fn load(text: &str, root: Option<&Path>) -> (Meta, String) {
    let empty = || (Meta::default(), text.to_string());
    if !text.starts_with("---\n") {
        return empty();
    }
    let Some(rel_end) = text[4..].find("\n---") else { return empty() };
    let end = 4 + rel_end;
    let head = &text[4..end];
    let body = text[end + 4..].trim_start_matches('\n').to_string();

    let mut meta = Meta::default();
    let mut edges: Vec<Edge> = Vec::new();
    let mut in_edges = false;
    for line in split_lines(head) {
        if line.starts_with("edges:") {
            in_edges = true;
            continue;
        }
        if in_edges && line.starts_with("  - ") {
            let item = line[4..].trim();
            let mut e: IndexMap<String, String> = IndexMap::new();
            if item.starts_with('{') && item.ends_with('}') {
                for c in FLOW_PAIR.captures_iter(&item[1..item.len() - 1]) {
                    if !c[2].trim().is_empty() {
                        e.insert(c[1].to_string(), unq(&c[2]));
                    }
                }
            } else {
                for part in item.split(", ") {
                    if let Some((ek, ev)) = part.split_once(": ") {
                        e.insert(ek.trim().to_string(), ev.trim().to_string());
                    }
                }
            }
            let to = e.get("to").cloned().unwrap_or_default();
            if !to.is_empty() && !e.contains_key("retracted") {
                edges.push(Edge {
                    rel: e.get("rel").cloned().unwrap_or_else(|| "related".into()),
                    to,
                });
            }
            continue;
        }
        in_edges = false;
        let Some((k, v)) = line.split_once(": ") else { continue };
        let v = v.trim();
        if v.starts_with('[') && v.ends_with(']') {
            if DERIVED.contains(&k) {
                continue;
            }
            // a relation list: one link per target
            for c in QUOTED.captures_iter(v) {
                let item = unq(&format!("\"{}\"", &c[1]));
                let Some(local) = link_local(&item) else { continue };
                let to = match root {
                    Some(r) => kind_for(r, &local),
                    None => String::new(),
                };
                edges.push(Edge {
                    rel: k.strip_prefix("rel_").unwrap_or(k).to_string(),
                    to: if to.is_empty() { format!("?:{local}") } else { to },
                });
            }
            continue;
        }
        meta.set(k, unq(v));
    }
    meta.edges = edges;
    (meta, body)
}
