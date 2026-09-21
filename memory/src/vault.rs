//! The vault: files on disk, and the rules for finding and writing one.
//!
//! The path is the id. `events/2026-09-07-e6799d.md` is `event:2026-09-07-e6799d`
//! — the directory is the kind, the stem is the rest, and nothing in the file
//! repeats either, because a second copy could only ever disagree with the first.

use crate::fm::{self, Edge, Meta};
use crate::text::{bare_ref, is_hash_id, is_local_id, one_line, py_repr};
use crate::SUMMARY_MAX;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// A name more than one node carries. The caller shows the candidates and asks
/// for an id; guessing would put the fact on the wrong one.
#[derive(Clone, Debug)]
pub struct Ambiguous {
    pub name: String,
    /// id, label, summary
    pub candidates: Vec<(String, String, String)>,
}

impl std::fmt::Display for Ambiguous {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let shown: Vec<String> = self.candidates.iter()
            .map(|(nid, name, summary)| {
                format!("{nid} ({})", if summary.is_empty() { name } else { summary })
            })
            .collect();
        write!(f, "{} is more than one node: {}", py_repr(&self.name), shown.join("; "))
    }
}

pub struct Node {
    pub id: String,
    pub meta: Meta,
    pub body: String,
}

impl Node {
    pub fn kind(&self) -> &str {
        self.id.split(':').next().unwrap_or("")
    }
}

pub struct Vault {
    pub root: PathBuf,
    /// The session writing to it, if known. Every node made under it is stamped
    /// `made: <session>` — the exchange that produced it. Set from the
    /// environment by `mem`, never by a session.
    pub session: String,
    /// A vault to take ids from instead of minting them, matched by kind, name
    /// and the session that made the node: a vault built a second time from the
    /// same writes keeps the ids it had, so writes that name an id still land.
    /// Never set when wanda is the one writing.
    pub oracle: Option<Box<Vault>>,
    /// And, per session, the order those ids were minted in — two nodes of one
    /// name made in one session are told apart by which came first.
    pub oracle_order: Option<HashMap<String, Vec<String>>>,
}

impl Vault {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Vault { root: root.into(), session: String::new(), oracle: None, oracle_order: None }
    }

    /// A node's file is its id. An event carries its date inside the id, so the
    /// lookup is exact for every kind: a glob on the stem would also match any
    /// longer file name ending in the same suffix.
    ///
    /// Creating the directory is a side effect of asking where a node would
    /// live, so a read probe for a kind with no nodes leaves an empty one
    /// behind. Kept as it is: the behaviour is recorded in every run that has
    /// been scored, and changing it is a change to the instrument.
    pub fn path_for(&self, nid: &str) -> PathBuf {
        let (kind, stem) = nid.split_once(':').unwrap_or(("", nid));
        let d = self.root.join(fm::dir_for(kind).unwrap_or(kind));
        let _ = std::fs::create_dir_all(&d);
        d.join(format!("{stem}.md"))
    }

    /// The file for this id, by exact name — a case-insensitive volume answers
    /// `Abc123.md` for `abc123.md`, and that is not the same node.
    pub fn exists(&self, nid: &str) -> bool {
        let p = self.path_for(nid);
        let Some(name) = p.file_name() else { return false };
        if !p.exists() {
            return false;
        }
        match std::fs::read_dir(p.parent().unwrap_or(&self.root)) {
            Ok(rd) => rd.flatten().any(|e| e.file_name() == *name),
            Err(_) => false,
        }
    }

    /// Every node file, in the order `sorted(root.rglob("*.md"))` gives.
    ///
    /// CPython compares paths by their parts, not by the whole string, so
    /// `a/b/c.md` comes before `a-b/c.md` where a plain string sort reverses
    /// them. The order decides which of two nodes sharing a name is found
    /// first, so it is not cosmetic.
    pub fn nodes(&self) -> Vec<Node> {
        let mut found: Vec<(Vec<String>, PathBuf)> = Vec::new();
        collect_md(&self.root, &mut Vec::new(), &mut found);
        found.sort_by(|a, b| a.0.cmp(&b.0));
        let mut out = Vec::new();
        for (parts, p) in found {
            if parts.len() != 2 || parts[1] == "CLAUDE.md" {
                continue;
            }
            let Some(kind) = fm::kind_of_dir(&parts[0]) else { continue };
            let Ok(text) = std::fs::read_to_string(&p) else { continue };
            let (meta, body) = fm::load(&text, Some(&self.root));
            let stem = parts[1].trim_end_matches(".md");
            out.push(Node { id: format!("{kind}:{stem}"), meta, body });
        }
        out
    }

    /// The node called this, by label, case and spacing aside. `None` if no
    /// node is; `Ambiguous` if more than one is — unless `kind` picks exactly
    /// one of them out, as `--place` does when an org carries the same name.
    pub fn by_name(&self, name: &str, kind: &str) -> Result<Option<String>, Ambiguous> {
        let want = one_line(name).to_lowercase();
        if want.is_empty() {
            return Ok(None);
        }
        let mut hits: Vec<Node> = Vec::new();
        for n in self.nodes() {
            let label = fm::label(&n.meta).to_lowercase();
            let aka = n.meta.get("aka").split("; ")
                .filter(|a| !a.is_empty())
                .map(|a| one_line(a).to_lowercase())
                .collect::<Vec<_>>();
            if label == want || aka.contains(&want) {
                hits.push(n);
            }
        }
        if hits.len() > 1 && !kind.is_empty() {
            let same: Vec<usize> = (0..hits.len()).filter(|&i| hits[i].kind() == kind).collect();
            if same.len() == 1 {
                let keep = same[0];
                hits = hits.drain(..).enumerate()
                    .filter(|(i, _)| *i == keep).map(|(_, n)| n).collect();
            }
        }
        match hits.len() {
            0 => Ok(None),
            1 => Ok(Some(hits[0].id.clone())),
            _ => Err(Ambiguous {
                name: name.to_string(),
                candidates: hits.iter()
                    .map(|n| (n.id.clone(), fm::label(&n.meta), n.meta.get("summary").to_string()))
                    .collect(),
            }),
        }
    }

    /// The id this vault gave a node of this kind and name in this session, by
    /// its name now or a name it had; of several, the lowest ranked not
    /// excluded.
    pub fn id_of(
        &self,
        kind: &str,
        name: &str,
        session: &str,
        exclude: &dyn Fn(&str) -> bool,
        rank: &dyn Fn(&str) -> usize,
    ) -> Option<String> {
        let want = one_line(name).to_lowercase();
        let mut hits: Vec<String> = Vec::new();
        for n in self.nodes() {
            if n.kind() != kind || (!session.is_empty() && n.meta.get("made") != session) {
                continue;
            }
            let mut names = vec![fm::label(&n.meta).to_lowercase()];
            names.extend(n.meta.get("aka").split("; ")
                .filter(|a| !a.is_empty())
                .map(|a| one_line(a).to_lowercase()));
            if names.contains(&want) && !exclude(&n.id) {
                hits.push(n.id);
            }
        }
        hits.into_iter().min_by_key(|n| rank(n))
    }

    fn by_id(&self, r: &str) -> Option<String> {
        // a probe, not a claim: nothing with whitespace is an id, and any
        // complaint about a probe means "not a node"
        if r.is_empty() || r.chars().any(|c| c.is_whitespace()) {
            return None;
        }
        let lower = r.to_lowercase();
        let (kind, local) = match lower.rsplit_once(':') {
            Some((k, l)) => (k.to_string(), l.to_string()),
            None => (String::new(), lower.clone()),
        };
        if !is_local_id(&local) {
            return None;
        }
        if !kind.is_empty() {
            let known = fm::KIND_DIR.iter().any(|(k, _)| *k == kind);
            return if known && self.exists(&lower) { Some(lower) } else { None };
        }
        for (k, _) in fm::KIND_DIR {
            let cand = format!("{k}:{local}");
            if self.exists(&cand) {
                return Some(cand);
            }
        }
        None
    }

    /// An id, a bare id without its kind, or a name — possibly with a gloss
    /// stuck to it, since a session reports `person:7f3a2c - the neighbour` to
    /// a reader as well as to a tool.
    pub fn resolve(&self, r: &str, kind: &str) -> Result<Option<String>, Ambiguous> {
        let r = r.trim();
        if let Some(nid) = self.by_id(r) {
            return Ok(Some(nid));
        }
        let head = r.split_whitespace().next().unwrap_or("");
        if head != r && (head.contains(':') || is_hash_id(head)) {
            if let Some(nid) = self.by_id(&bare_ref(r)) {
                return Ok(Some(nid));
            }
        }
        self.by_name(r, kind)
    }

    /// A fresh id for a new node: random, short, dated if it is an event.
    /// Unique across every kind, so a bare id names one node; `taken` is for a
    /// batch that mints before it writes.
    pub fn mint(&self, kind: &str, when: &str, taken: Option<&mut Vec<String>>, name: &str) -> String {
        if let (Some(oracle), false) = (&self.oracle, name.is_empty()) {
            let order: Vec<String> = self.oracle_order.as_ref()
                .and_then(|m| m.get(&self.session)).cloned().unwrap_or_default();
            let n = order.len();
            let nid = oracle.id_of(kind, name, &self.session,
                &|n| self.exists(n),
                &|x| order.iter().position(|o| o == x).unwrap_or(n));
            if let Some(nid) = nid {
                return nid;
            }
        }
        // A replay mints from a digest rather than a random source, because
        // two replays of one recorded run have to produce the same ids: a
        // harness comparing two implementations cannot tell a difference in
        // behaviour from a difference in the draw. The oracle is set only when
        // replaying. Live, an id stays unguessable.
        let mut taken = taken;
        let mut attempt: u64 = 0;
        loop {
            let h = if self.oracle.is_some() {
                let seed = format!("{kind}|{when}|{name}|{}|{attempt}", self.session);
                attempt += 1;
                hex6(&seed)
            } else {
                random_hex6()
            };
            if !h.chars().any(|c| c.is_ascii_digit()) {
                continue;
            }
            let local = if when.is_empty() { h.clone() } else { format!("{when}-{h}") };
            let nid = format!("{kind}:{local}");
            if let Some(t) = taken.as_deref() {
                if t.contains(&local) {
                    continue;
                }
            }
            if fm::KIND_DIR.iter().all(|(k, _)| !self.exists(&format!("{k}:{local}"))) {
                if let Some(t) = taken.as_deref_mut() {
                    t.push(local);
                }
                return nid;
            }
        }
    }

    /// A new label, or a new summary, on the same node.
    ///
    /// The id is opaque, so nothing else in the vault has to change — no file
    /// moves, no edge is rewritten — and what it used to say is kept in the
    /// body, struck, with the date and reason, so the file still says what it
    /// used to be called. When the summary was the name, it follows the name.
    pub fn rename(&self, old: &str, new_name: &str, summary: &str, because: &str, when: &str)
        -> String
    {
        let src = self.path_for(old);
        let Ok(text) = std::fs::read_to_string(&src) else { return old.to_string() };
        let (mut meta, body) = fm::load(&text, Some(&self.root));
        let kind = old.split(':').next().unwrap_or("").to_string();
        let entity = fm::ENTITY_KINDS.contains(&kind.as_str());
        let mut new_name = one_line(new_name);
        let mut summary = one_line(summary);
        let mut old_name = one_line(meta.get("name"));
        let old_summary = one_line(meta.get("summary"));
        // an event, a thread or a rule is named by its summary: a new name is a
        // new summary, and the old one is kept as a former label
        if !entity {
            if summary.is_empty() {
                summary = new_name.clone();
            }
            new_name = String::new();
            old_name = old_summary.clone();
        }
        let mut notes: Vec<String> = Vec::new();
        let push_aka = |meta: &mut Meta, held: &str| {
            let mut aka: Vec<String> = meta.get("aka").split("; ")
                .filter(|a| !a.is_empty()).map(|a| a.to_string()).collect();
            if !held.is_empty()
                && !aka.iter().any(|a| a.to_lowercase() == held.to_lowercase()) {
                aka.push(held.to_string());
            }
            meta.set("aka", aka.join("; "));
        };
        if !summary.is_empty() && !entity && summary != old_summary {
            push_aka(&mut meta, &old_summary);
        }
        if !new_name.is_empty() && new_name != old_name {
            meta.set("name", new_name.clone());
            // the old name still finds the node: a session that knew it by the
            // name it had before must not mint a second one
            if old_name.to_lowercase() != new_name.to_lowercase() {
                push_aka(&mut meta, &old_name);
            }
            notes.push(format!("~~was named: {old_name}~~"));
        }
        if !summary.is_empty() && summary != old_summary {
            if !old_summary.is_empty() {
                notes.push(format!("~~was summarised: {old_summary}~~"));
            }
            meta.set("summary", take_chars(&summary, SUMMARY_MAX));
        }
        if notes.is_empty() {
            return old.to_string();
        }
        let verb = if new_name.is_empty() { "resummarised" } else { "renamed" };
        let why = if because.is_empty() {
            format!(" ({verb} {when})")
        } else {
            format!(" ({verb} {when}: {because})")
        };
        let note = notes.join(" ") + &why;
        let body = if crate::text::py_strip(&body).is_empty() {
            note + "\n"
        } else {
            format!("{}\n\n{note}\n", body.trim_end_matches(crate::text::is_py_space))
        };
        let _ = std::fs::write(&src, format!("{}\n\n{body}", fm::dump(&meta, &kind)));
        old.to_string()
    }

    /// Write or update a node. `name` is its label; `summary` is its index
    /// line, one line, at most SUMMARY_MAX; `body` is a line to append.
    #[allow(clippy::too_many_arguments)]
    pub fn upsert(
        &self,
        nid: &str,
        kind: &str,
        name: &str,
        summary: &str,
        body: &str,
        meta_extra: &[(String, String)],
        add_edges: &[Edge],
        date: &str,
    ) -> PathBuf {
        let p = self.path_for(nid);
        let (mut meta, old_body) = match std::fs::read_to_string(&p) {
            Ok(t) => fm::load(&t, Some(&self.root)),
            Err(_) => (Meta::default(), String::new()),
        };
        let entity = fm::ENTITY_KINDS.contains(&kind);
        if !name.is_empty() && entity {
            meta.set_default("name", one_line(name));
        }
        if !summary.is_empty() {
            meta.set("summary", take_chars(&one_line(summary), SUMMARY_MAX));
        } else if !name.is_empty() && !entity {
            meta.set_default("summary", take_chars(&one_line(name), SUMMARY_MAX));
        }
        meta.set_default("created", date);
        if !self.session.is_empty() {
            meta.set_default("made", &self.session);
        }
        if !date.is_empty() && entity {
            meta.set("last_seen", date);
        }
        for (k, v) in meta_extra {
            if !v.is_empty() {
                // one line, always: a newline in a value would end the field
                // and a `---` on its own line would end the frontmatter
                meta.set(k, one_line(v));
            }
        }
        for e in add_edges {
            if !meta.edges.iter().any(|x| x.rel == e.rel && x.to == e.to) {
                meta.edges.push(e.clone());
            }
        }
        let mut lines: Vec<String> = crate::text::split_lines(&old_body)
            .into_iter().filter(|l| !l.trim().is_empty()).map(|s| s.to_string()).collect();
        // a line that has been retracted must not come back the next time the
        // same sentence is written, or a correction lasts until the next mention
        let struck: Vec<String> = lines.iter().filter(|l| l.starts_with("~~"))
            .map(|l| l.trim_start_matches('~').split("~~").next().unwrap_or("").trim().to_string())
            .collect();
        for new_line in crate::text::split_lines(body) {
            let new_line = new_line.trim_end();
            if !new_line.trim().is_empty()
                && !lines.iter().any(|l| l == new_line)
                && !struck.iter().any(|s| s == new_line.trim())
            {
                lines.push(new_line.to_string());
            }
        }
        let text = format!("{}\n\n{}\n", fm::dump(&meta, kind), lines.join("\n"));
        let _ = std::fs::write(&p, text);
        p
    }
}

/// `t[:cap]` by characters, which is what Python slices.
fn take_chars(s: &str, cap: usize) -> String {
    s.chars().take(cap).collect()
}

fn hex6(seed: &str) -> String {
    let d = Sha256::digest(seed.as_bytes());
    hex::encode(d)[..6].to_string()
}

fn random_hex6() -> String {
    let mut b = [0u8; 3];
    getrandom::fill(&mut b).expect("a random id");
    hex::encode(b)
}

fn collect_md(dir: &Path, parts: &mut Vec<String>, out: &mut Vec<(Vec<String>, PathBuf)>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        let p = e.path();
        parts.push(name.clone());
        if p.is_dir() {
            if name != ".claude" {
                collect_md(&p, parts, out);
            }
        } else if name.ends_with(".md") {
            out.push((parts.clone(), p));
        }
        parts.pop();
    }
}
