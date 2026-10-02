//! The vault: files on disk, and the rules for finding and writing one.
//!
//! The path is the id. `events/2026-09-07-e6799d.md` is `event:2026-09-07-e6799d`
//! — the directory is the kind, the stem is the rest, and nothing in the file
//! repeats either, because a second copy could only ever disagree with the first.

use crate::fm::{self, Edge, Meta};
use crate::text::{is_hash_id, one_line, py_repr};
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

/// Whether the call a reference is read for only reads, or writes.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Slot { Reading, Writing }

/// Why a value named no node.
#[derive(Clone, Debug)]
pub enum Miss {
    Nothing,
    /// a spelling of a kind with no id or name after it ("" for `entity`)
    KindAlone { kind: String, spelling: String },
    /// a kind written, and no node of it with that id, or with the id-like
    /// text after it
    NoneById { kind: String },
    NoneByName { kind: String, name: String },
    /// a kind written, and the id is another kind's node
    WrongKind { kind: String, id: String, node: String },
    /// a kind written, and the name is one node of another kind
    WrongKindName { kind: String, name: String, node: String },
    Ambiguous(Ambiguous),
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
    /// name made in one session are told apart by which came first. Under
    /// `ORACLE_CALL`, the ids printed by the tool call the call being replayed
    /// ran in, which is where a node the oracle no longer holds takes its id
    /// from.
    pub oracle_order: Option<HashMap<String, Vec<String>>>,
    /// The first write to this vault that could not be made: its path in the
    /// vault and the error.
    failed: std::sync::OnceLock<String>,
}

/// The key `oracle_order` holds the call's ids under. No session id can be it,
/// since an environment variable cannot hold a NUL.
const ORACLE_CALL: &str = "\0";

impl Vault {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Vault { root: root.into(), session: String::new(), oracle: None, oracle_order: None,
                failed: std::sync::OnceLock::new() }
    }

    /// A file in the vault written whole (`write_whole`). After one write has
    /// failed no other is made, so a call stops writing at the file it could
    /// not write, as a call killed there would, and `failed` says which.
    pub fn write(&self, path: &Path, text: &str) {
        self.keep(path, || {
            // a directory that could not be made is the cause, and the file
            // missing from it only the symptom
            if let Some(d) = path.parent() {
                std::fs::create_dir_all(d)?;
            }
            write_whole(path, text)
        });
    }

    /// A node file removed, a failure kept as `write` keeps one.
    pub fn remove(&self, path: &Path) {
        self.keep(path, || std::fs::remove_file(path));
    }

    fn keep(&self, path: &Path, made: impl FnOnce() -> std::io::Result<()>) {
        if self.failed.get().is_some() {
            return;
        }
        if let Err(e) = made() {
            let at = path.strip_prefix(&self.root).unwrap_or(path);
            let _ = self.failed.set(format!("{}: {e}", at.display()));
        }
    }

    /// A node file a write has to read first and could not, kept as that
    /// write's failure: rewritten from what was read, the node would lose
    /// what could not be.
    pub fn unreadable(&self, path: &Path, e: std::io::Error) {
        self.keep(path, || Err(e));
    }

    /// The first write that could not be made, as `<path in the vault>: <error>`.
    pub fn failed(&self) -> Option<&str> {
        self.failed.get().map(String::as_str)
    }

    /// The failed write as an error, for whoever keeps one `Vault` across
    /// calls, as the lab's rebuild does: after one failure every later write
    /// is skipped, and a caller that did not ask would go on writing nothing
    /// and saying nothing.
    pub fn written(&self) -> Result<(), String> {
        match self.failed() {
            None => Ok(()),
            Some(what) => Err(format!("the store could not be written at {what}; \
                                       nothing after that was written")),
        }
    }

    /// The vault held for one call, until the file returned is closed: shared
    /// to read, exclusive to write. A write then never shows half done to
    /// another call, two writes to one node both land, and a node looked for
    /// and not found is still not there when the new one is written. The lock
    /// is the kernel's, on the vault's own directory, so it goes when the
    /// process does, however it ends. Not had within `within`, it is given up
    /// with an error of kind `TimedOut`.
    pub fn lock(&self, exclusive: bool, within: std::time::Duration)
        -> std::io::Result<std::fs::File>
    {
        std::fs::create_dir_all(&self.root)?;
        let dir = std::fs::File::open(&self.root)?;
        // a flock that waits cannot be called off, so it waits in a thread,
        // on a second handle of the open directory; the lock is the open
        // directory's, so it stays with `dir` when that handle is closed
        let waiting = dir.try_clone()?;
        let (taken, wait) = std::sync::mpsc::channel();
        std::thread::Builder::new().spawn(move || {
            let r = if exclusive { waiting.lock() } else { waiting.lock_shared() };
            drop(waiting);
            let _ = taken.send(r);
        })?;
        match wait.recv_timeout(within) {
            Ok(r) => r.map(|()| dir),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                format!("it stayed busy for {} s", within.as_secs_f32()))),
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                Err(std::io::Error::other("the wait for it ended without an answer"))
            }
        }
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
    /// Her own two names are never ambiguous: they find her node, and a
    /// namesake is reached by its id.
    pub fn by_name(&self, name: &str, kind: &str) -> Result<Option<String>, Ambiguous> {
        let want = one_line(name).to_lowercase();
        if want.is_empty() {
            return Ok(None);
        }
        let nodes = self.nodes();
        if crate::is_self_name(&want) {
            if let Some(me) = me_in(&nodes) {
                return Ok(Some(nodes[me].id.clone()));
            }
        }
        let mut hits: Vec<Node> = Vec::new();
        for n in nodes {
            let label = fm::label(&n.meta).to_lowercase();
            let former: Vec<String> = fm::former_names(&n.body).iter()
                .map(|a| a.to_lowercase()).collect();
            if label == want || former.contains(&want) {
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

    /// The assistant's own node, if this vault has exactly one.
    pub fn me(&self) -> Option<String> {
        let nodes = self.nodes();
        me_in(&nodes).map(|i| nodes[i].id.clone())
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
            names.extend(fm::former_labels(&n.body).iter().map(|a| a.to_lowercase()));
            if names.contains(&want) && !exclude(&n.id) {
                hits.push(n.id);
            }
        }
        hits.into_iter().min_by_key(|n| rank(n))
    }

    /// Every node id on disk, from the kind directories' listings.
    fn all_ids(&self) -> Vec<String> {
        let mut out = Vec::new();
        for (k, d) in fm::KIND_DIR {
            let Ok(rd) = std::fs::read_dir(self.root.join(d)) else { continue };
            let mut names: Vec<String> = rd.flatten()
                .filter_map(|e| e.file_name().to_str().map(str::to_string))
                .filter(|n| n.ends_with(".md") && n != "CLAUDE.md").collect();
            names.sort();
            out.extend(names.iter().map(|n| format!("{k}:{}", n.trim_end_matches(".md"))));
        }
        out
    }

    /// Whether any node of this kind is stored, the condition under which its
    /// directory's index is written.
    pub fn has_nodes(&self, kind: &str) -> bool {
        self.all_ids().iter().any(|id| id.split(':').next() == Some(kind))
    }

    fn ambiguous(&self, ids: &[String], name: &str) -> Ambiguous {
        let nodes = self.nodes();
        Ambiguous {
            name: name.to_string(),
            candidates: ids.iter().map(|id| match nodes.iter().find(|n| &n.id == id) {
                Some(n) => (id.clone(), fm::label(&n.meta), n.meta.get("summary").to_string()),
                None => (id.clone(), String::new(), String::new()),
            }).collect(),
        }
    }

    /// An id under `kind`, or under each kind in turn: the exact id, then, for
    /// six characters with no date, the node whose id ends in them.
    fn by_id(&self, kind: Option<&str>, local: &str, written: &str)
        -> Result<Option<String>, Ambiguous>
    {
        let kinds: Vec<&str> = match kind {
            Some(k) => vec![k],
            None => fm::KIND_DIR.iter().map(|(k, _)| *k).collect(),
        };
        for k in &kinds {
            let cand = format!("{k}:{local}");
            if self.exists(&cand) {
                return Ok(Some(cand));
            }
        }
        if local.len() == 6 {
            let tail = format!("-{local}");
            let hits: Vec<String> = self.all_ids().into_iter()
                .filter(|id| kinds.iter().any(|k| id.split(':').next() == Some(*k)))
                .filter(|id| id.ends_with(&tail))
                .collect();
            match hits.len() {
                0 => {}
                1 => return Ok(Some(hits[0].clone())),
                _ => return Err(self.ambiguous(&hits, written)),
            }
        }
        Ok(None)
    }

    /// `<kind>:<name>` looked up among that kind's nodes, and, when none is so
    /// named, among the others, the one found there taken only where the slot
    /// reads; with no kind written, by_name.
    fn by_spelled_name(&self, kind: &str, name: &str, prefer: &str, slot: Slot)
        -> Result<Option<String>, Miss>
    {
        if kind.is_empty() {
            return self.by_name(name, prefer).map_err(Miss::Ambiguous);
        }
        let want = one_line(name).to_lowercase();
        if want.is_empty() {
            return Ok(None);
        }
        let nodes = self.nodes();
        if kind == "person" && crate::is_self_name(&want) {
            if let Some(me) = me_in(&nodes) {
                return Ok(Some(nodes[me].id.clone()));
            }
        }
        let (same, others): (Vec<&Node>, Vec<&Node>) = nodes.iter()
            .filter(|n| named(n, &want)).partition(|n| n.kind() == kind);
        let ids = |ns: &[&Node]| -> Vec<String> { ns.iter().map(|n| n.id.clone()).collect() };
        match (same.len(), others.len(), slot) {
            (1, _, _) => Ok(Some(same[0].id.clone())),
            (0, 0, _) => Ok(None),
            (0, 1, Slot::Reading) => Ok(Some(others[0].id.clone())),
            (0, 1, Slot::Writing) => Err(Miss::WrongKindName {
                kind: kind.to_string(), name: name.to_string(), node: others[0].id.clone() }),
            (0, _, _) => Err(Miss::Ambiguous(self.ambiguous(&ids(&others), name))),
            _ => Err(Miss::Ambiguous(self.ambiguous(&ids(&same), name))),
        }
    }

    /// The node a value names, or why none: an id, bare or with any spelling of
    /// its kind and anything after it; a name; a kind and a name. Where the
    /// kind written is not the kind of the node an id or a name belongs to,
    /// that node is used where the slot only reads, and the value is refused
    /// where it writes: either part may be the wrong one, and an edge written
    /// to the node nobody meant is silent.
    pub fn find(&self, r: &str, prefer: &str, slot: Slot) -> Result<String, Miss> {
        let owned = crate::text::unbracket(r);
        let r = owned.as_str();
        if r.is_empty() {
            return Err(Miss::Nothing);
        }
        let head = r.split_whitespace().next().unwrap_or("");
        let mut miss = Miss::Nothing;
        if let Some((kind, local)) = crate::text::id_word(head) {
            match self.by_id(kind, &local, head) {
                Ok(Some(n)) => return Ok(n),
                Err(a) => return Err(Miss::Ambiguous(a)),
                Ok(None) => {}
            }
            if let Some(k) = kind {
                miss = Miss::NoneById { kind: k.to_string() };
                match self.by_id(None, &local, head) {
                    Ok(Some(n)) if slot == Slot::Reading => return Ok(n),
                    Ok(Some(n)) => return Err(Miss::WrongKind {
                        kind: k.to_string(), id: local, node: n }),
                    Err(a) => return Err(Miss::Ambiguous(a)),
                    Ok(None) => {}
                }
            }
        } else if let Some(nid) = crate::text::legacy_word(head) {
            if self.exists(&nid) {
                return Ok(nid);
            }
        } else if head == r && crate::text::is_local_id(&head.to_lowercase()) {
            let local = head.to_lowercase();
            for (k, _) in fm::KIND_DIR {
                let cand = format!("{k}:{local}");
                if self.exists(&cand) {
                    return Ok(cand);
                }
            }
        }
        match self.by_name(r, prefer) {
            Ok(Some(n)) => return Ok(n),
            Err(a) => return Err(Miss::Ambiguous(a)),
            Ok(None) => {}
        }
        if let Some((kind, spelling)) = crate::text::kind_alone(r) {
            return Err(Miss::KindAlone { kind: kind.to_string(), spelling });
        }
        if let Some((kind, rest)) = crate::text::spelled(r) {
            let rest = rest.trim();
            if !is_hash_id(rest) && !crate::text::hash_md(rest) {
                if let Some(n) = self.by_spelled_name(kind, rest, prefer, slot)? {
                    return Ok(n);
                }
                if !kind.is_empty() {
                    miss = if crate::text::id_like(rest) {
                        Miss::NoneById { kind: kind.to_string() }
                    } else {
                        Miss::NoneByName { kind: kind.to_string(), name: rest.to_string() }
                    };
                }
            }
        }
        Err(miss)
    }

    /// A value holding a comma, in a slot that takes a list, tried whole only
    /// as a name: a name with a comma in it is still found, and anything else
    /// is split.
    pub fn find_whole_name(&self, r: &str) -> Result<Option<String>, Miss> {
        let owned = crate::text::unbracket(r);
        let r = owned.as_str();
        match self.by_name(r, "") {
            Ok(Some(n)) => return Ok(Some(n)),
            Err(a) => return Err(Miss::Ambiguous(a)),
            Ok(None) => {}
        }
        if let Some((kind, rest)) = crate::text::spelled(r) {
            let rest = rest.trim();
            if !is_hash_id(rest) && !crate::text::hash_md(rest) {
                return self.by_spelled_name(kind, rest, "", Slot::Writing);
            }
        }
        Ok(None)
    }

    /// `find` where the slot only reads, with every miss but an ambiguous one
    /// as none.
    pub fn resolve(&self, r: &str, kind: &str) -> Result<Option<String>, Ambiguous> {
        match self.find(r, kind, Slot::Reading) {
            Ok(n) => Ok(Some(n)),
            Err(Miss::Ambiguous(a)) => Err(a),
            Err(_) => Ok(None),
        }
    }

    /// A fresh id for a new node: random, short, dated if it is an event. A
    /// drawn id is unique across every kind and ends in six characters no id
    /// on disk ends in, so from then on a bare id, or an event's six characters
    /// without its date, names one node; a replay takes the recorded id as it
    /// is. `taken` is for a batch that mints before it writes.
    pub fn mint(&self, kind: &str, when: &str, taken: Option<&mut Vec<String>>, name: &str) -> String {
        self.mint_shown(kind, when, taken, name, true)
    }

    /// `mint`, told whether the call making the node prints its id as `ok <id>`.
    /// A stub made from a name is not, but for a relate's subject; where the
    /// oracle names such a stub, an id its tool call printed that the oracle
    /// lacks is taken to be another node's, so a replay keeps the oracle's.
    /// Live, `shown` is not read.
    pub fn mint_shown(&self, kind: &str, when: &str, taken: Option<&mut Vec<String>>, name: &str,
                      shown: bool) -> String {
        if let (Some(oracle), false) = (&self.oracle, name.is_empty()) {
            let printed = |key: &str| self.oracle_order.as_ref()
                .and_then(|m| m.get(key)).cloned().unwrap_or_default();
            let (order, call) = (printed(&self.session), printed(ORACLE_CALL));
            let n = order.len();
            let rank = |x: &str| order.iter().position(|o| o == x).unwrap_or(n);
            let nid = oracle.id_of(kind, name, &self.session, &|n| self.exists(n), &rank);
            // a node the run forgot is not in the oracle, so its id is one the
            // tool call it was made in printed: the first there of this kind,
            // and of this date for an event, that neither vault holds. A node
            // the oracle names keeps its id where that tool call printed the id
            // too, since the calls in one are not told apart; where its session
            // never printed it, as with a node whose output its command sent
            // elsewhere; or where its making call does not print its id. The
            // oracle's ids are listed, not looked up, since a lookup makes the
            // kind's directory in the run's own vault
            let kept = oracle.all_ids();
            let gone = call.iter().find(|x| !kept.contains(x)
                && x.split_once(':').is_some_and(|(k, local)| k == kind
                    && (kind != "event" || local.starts_with(&format!("{when}-"))))
                && !self.exists(x));
            match (gone, nid) {
                (Some(g), Some(h)) if shown && !call.contains(&h) && rank(&h) < n => return g.clone(),
                (Some(g), None) => return g.clone(),
                (_, Some(h)) => return h,
                (None, None) => {}
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
            if !h.chars().any(|c| c.is_ascii_digit()) || self.all_ids().iter().any(|id| id.ends_with(&h)) {
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
    /// used to be called. A node named by its summary takes a new name as its
    /// summary when no summary is given with it.
    /// Returns the struck line written, if one was.
    pub fn rename(&self, old: &str, new_name: &str, summary: &str, because: &str, when: &str)
        -> Option<String>
    {
        let src = self.path_for(old);
        let text = match std::fs::read_to_string(&src) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return None,
            // a node there that cannot be read cannot be renamed, and printing
            // `ok` would say it was: the call fails as a write the store
            // cannot take
            Err(e) => {
                self.keep(&src, || Err(e));
                return None;
            }
        };
        let (mut meta, body) = fm::load(&text, Some(&self.root));
        let kind = old.split(':').next().unwrap_or("").to_string();
        let entity = fm::ENTITY_KINDS.contains(&kind.as_str());
        let mut new_name = one_line(new_name);
        let mut summary = one_line(summary);
        let old_name = one_line(meta.get("name"));
        let old_summary = one_line(meta.get("summary"));
        // an event, a thread or a rule is named by its summary, so a new name
        // for one of those is a new summary. It leaves `~~was summarised:~~`
        // below rather than `~~was named:~~`, and only the second is read back
        // as a name — what this node used to say is not what it used to be
        // called.
        if !entity {
            if summary.is_empty() {
                summary = new_name.clone();
            }
            new_name = String::new();
        }
        let mut notes: Vec<String> = Vec::new();
        let mut changed = false;
        let renamed = !new_name.is_empty() && new_name != old_name;
        if renamed {
            meta.set("name", new_name.clone());
            // the struck line below is the whole record: `by_name` reads the
            // old name back out of it, so a session that knew this node by the
            // name it had before still finds it and does not mint a second one
            notes.push(format!("~~was named: {old_name}~~"));
        }
        if !summary.is_empty() && summary != old_summary {
            if !old_summary.is_empty() {
                notes.push(format!("~~was summarised: {old_summary}~~"));
            }
            meta.set("summary", take_chars(&summary, SUMMARY_MAX));
            changed = true;
        }
        if notes.is_empty() && !changed {
            return None;
        }
        let verb = if renamed { "renamed" } else { "resummarised" };
        let because = one_line(because);
        let why = if because.is_empty() {
            format!(" ({verb} {when})")
        } else {
            format!(" ({verb} {when}: {because})")
        };
        let note = notes.join(" ") + &why;
        let body = if notes.is_empty() {
            body
        } else if crate::text::py_strip(&body).is_empty() {
            note.clone() + "\n"
        } else {
            format!("{}\n\n{note}\n", body.trim_end_matches(crate::text::is_py_space))
        };
        let former = fm::former_names(&body);
        self.write(&src, &format!("{}\n\n{body}", fm::dump(&meta, &kind, &former)));
        (!notes.is_empty()).then_some(note)
    }

    /// Write or update a node. `name` is its label; `summary` is its index
    /// line, one line, at most SUMMARY_MAX; `body` is lines to add below the
    /// ones the node has. Returns what became of each non-blank line of `body`.
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
    ) -> Vec<Taken> {
        let p = self.path_for(nid);
        let (mut meta, old_body) = match std::fs::read_to_string(&p) {
            Ok(t) => fm::load(&t, Some(&self.root)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (Meta::default(), String::new()),
            // a node there that cannot be read would be replaced by what this
            // call knows of it, losing its history and edges, since
            // `write_whole` replaces the file by renaming over it, which needs
            // only the directory: the call fails as a write the store cannot
            // take
            Err(e) => {
                self.keep(&p, || Err(e));
                return Vec::new();
            }
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
        // what the body says already: each live line, and each struck line's text
        // with how it was struck (`struck_parts`)
        let mut said: Vec<(String, Option<String>)> = lines.iter().map(|l| {
            if l.starts_with("~~") {
                let (text, how) = struck_parts(l);
                (text.to_string(), Some(how.to_string()))
            } else {
                (l.clone(), None)
            }
        }).collect();
        let mut taken = Vec::new();
        for new_line in crate::text::split_lines(body) {
            let new_line = new_line.trim_end();
            if new_line.trim().is_empty() {
                continue;
            }
            let mut t = Taken::default();
            // a line the file holds, or one struck from it, is not written again,
            // or a retraction lasts until the next mention. Checked before any
            // front is cut, or a shorter line's front would go and the rest of it
            // be written
            let mut rest = if t.held_back(new_line, &lines, &said) { "" } else { new_line };
            // a line that repeats the body and adds a fact keeps only the fact,
            // or each such line repeats all above it. Longest front first, so a
            // line opening with two earlier lines, or with a struck one, loses all
            // it repeats
            while let Some((front, how)) = said.iter()
                .filter(|(s, _)| opens_with(rest, s))
                .max_by_key(|(s, _)| s.len())
            {
                match how {
                    None => t.held = true,
                    Some(how) => t.unsaid.push((front.clone(), how.clone())),
                }
                rest = rest[front.len()..].trim_start_matches(crate::text::is_py_space);
            }
            if !rest.is_empty() && !t.held_back(rest, &lines, &said) {
                lines.push(rest.to_string());
                if !rest.starts_with("~~") {
                    said.push((rest.to_string(), None));
                }
                t.added = Some(rest.to_string());
            }
            taken.push(t);
        }
        let joined = lines.join("\n");
        let text = format!("{}\n\n{joined}\n", fm::dump(&meta, kind, &fm::former_names(&joined)));
        self.write(&p, &text);
        taken
    }
}

/// What became of one line `upsert` was passed.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Taken {
    /// what was written for it: the line, or what was left of it once its
    /// front was found in the body already
    pub added: Option<String>,
    /// some or all of it is in the body already, and was not written again
    pub held: bool,
    /// parts of it struck from the body earlier and not written again, each as
    /// it read, with how it was struck (`struck_parts`)
    pub unsaid: Vec<(String, String)>,
}

impl Taken {
    /// `text` is a line the file holds, or the text of a struck one, and is
    /// not written. Which one is noted.
    fn held_back(&mut self, text: &str, lines: &[String], said: &[(String, Option<String>)]) -> bool {
        if lines.iter().any(|l| l == text) {
            self.held = true;
            return true;
        }
        let struck = said.iter().find(|(s, how)| how.is_some() && s == text.trim());
        if let Some((s, Some(how))) = struck {
            self.unsaid.push((s.clone(), how.clone()));
            return true;
        }
        false
    }
}

/// A struck line's text as it read before it was struck, and the word its note
/// opens with: `retracted` or `amended` for a line those struck, `struck` for
/// any other.
pub fn struck_parts(line: &str) -> (&str, &str) {
    let inner = line.trim_start_matches('~');
    let (text, note) = inner.split_once("~~").unwrap_or((inner, ""));
    let word = note.trim_start().trim_start_matches('(').split(' ').next().unwrap_or("");
    let how = if matches!(word, "retracted" | "amended") { word } else { "struck" };
    (text.trim(), how)
}

/// `text` opens with `said`, a finished sentence, and goes on after whitespace.
/// A phrase never counts: it can open a longer sentence that says something
/// else, and a line extending a struck phrase is usually its correction.
pub fn opens_with(text: &str, said: &str) -> bool {
    !said.is_empty()
        && ends_sentence(said)
        && text.len() > said.len()
        && text.starts_with(said)
        && text[said.len()..].starts_with(crate::text::is_py_space)
}

/// Ends at a full stop, question mark, exclamation mark or ellipsis, before
/// any closing quotes or brackets.
pub fn ends_sentence(s: &str) -> bool {
    s.trim_end_matches(['"', '\'', '\u{2019}', '\u{201d}', ')', ']']).ends_with(['.', '!', '?', '\u{2026}'])
}

/// The node's label, or a name it had, is this one, lower case.
fn named(n: &Node, want: &str) -> bool {
    fm::label(&n.meta).to_lowercase() == want
        || fm::former_names(&n.body).iter().any(|a| a.to_lowercase() == want)
}

/// The one person labelled `SELF_LABEL`; failing that, the one person labelled
/// with her name, which is how an older vault labels her node.
fn me_in(nodes: &[Node]) -> Option<usize> {
    for label in [crate::SELF_LABEL, crate::SELF_NAME] {
        let found: Vec<usize> = (0..nodes.len())
            .filter(|&i| nodes[i].kind() == "person"
                    && fm::label(&nodes[i].meta).to_lowercase() == label)
            .collect();
        match found.as_slice() {
            [] => continue,
            [one] => return Some(*one),
            _ => return None,
        }
    }
    None
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

/// A file written whole or not at all, for whatever reads the vault without
/// holding it: Claude Code loading CLAUDE.md as a session starts, a session's
/// own Read or Grep, a snapshot. The text goes to a file beside it, which then
/// takes its name, so a reader finds the old text or the new and never one cut
/// short. The name does not end in `.md`, so a file left by a call killed
/// part way is not read as a node.
fn write_whole(path: &Path, text: &str) -> std::io::Result<()> {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let part = path.with_file_name(format!(".{name}.part"));
    let made = std::fs::write(&part, text).and_then(|()| std::fs::rename(&part, path));
    if made.is_err() {
        // what never took the name is nothing a reader wants, and on a full
        // disk it holds space the next write needs
        let _ = std::fs::remove_file(&part);
    }
    made
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

#[cfg(test)]
mod tests {
    use super::*;

    const DAY: &str = "2031-01-10";

    /// A vault in a fresh directory, removed when the test ends.
    struct Store(Vault);

    impl Drop for Store {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0.root);
        }
    }

    impl std::ops::Deref for Store {
        type Target = Vault;
        fn deref(&self) -> &Vault { &self.0 }
    }

    /// A node of every kind, with a topic of each of three shapes and two events
    /// (one whose summary starts with a number that reads as an id).
    fn store(tag: &str) -> Store {
        let root = std::env::temp_dir().join(format!("mem-find-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let v = Vault::new(root);
        v.upsert("person:1a2b3c", "person", "Alpha", "a neighbour", "", &[], &[], DAY);
        v.upsert("topic:4d5e6f", "topic", "garden plans", "", "", &[], &[], DAY);
        v.upsert("topic:5e6f7a", "topic", "Events", "", "", &[], &[], DAY);
        v.upsert("topic:6f7a8b", "topic", "rain, wind", "", "", &[], &[], DAY);
        v.upsert("event:2031-01-02-7a8b9c", "event", "the kettle descaling",
                 "the kettle descaling", "", &[], &[], DAY);
        v.upsert("event:2031-01-03-8b9c0d", "event", "123456 steps a day", "123456 steps a day",
                 "", &[], &[], DAY);
        v.upsert("preference:9c0d1e", "preference", "tea rule", "tea rule", "", &[], &[], DAY);
        v.upsert("place:3a4b5c", "place", "the lido", "", "", &[], &[], DAY);
        v.upsert("org:4b5c6d", "org", "the lido trust", "", "", &[], &[], DAY);
        v.upsert("group:5c6d7e", "group", "the choir", "", "", &[], &[], DAY);
        v.upsert("thing:6d7e8f", "thing", "the ladder", "", "", &[], &[], DAY);
        v.upsert("trajectory:7e8f9a", "trajectory", "the roof repair", "the roof repair", "",
                 &[], &[], DAY);
        Store(v)
    }

    fn found(v: &Vault, r: &str, slot: Slot) -> String {
        v.find(r, "", slot).unwrap_or_else(|m| panic!("{r}: {m:?}"))
    }

    #[test]
    fn an_id_in_any_spelling() {
        let v = store("spell");
        for r in ["preference:9c0d1e", "pref:9c0d1e", "prefs:9c0d1e", "prefs/9c0d1e",
                  "./prefs/9c0d1e.md", "PREFERENCES:9c0d1e", "[[9c0d1e]]", "entity:1a2b3c",
                  "9c0d1e", "pref:9c0d1e (the tea one)", "[[9c0d1e]] (a note)"] {
            let want = if r.contains("1a2b3c") { "person:1a2b3c" } else { "preference:9c0d1e" };
            assert_eq!(found(&v, r, Slot::Writing), want, "{r}");
        }
    }

    #[test]
    fn every_spelling_with_an_id_and_a_name() {
        let v = store("every");
        let named: [(&str, &str, &str); 10] = [
            ("person", "person:1a2b3c", "Alpha"),
            ("place", "place:3a4b5c", "the lido"),
            ("org", "org:4b5c6d", "the lido trust"),
            ("group", "group:5c6d7e", "the choir"),
            ("thing", "thing:6d7e8f", "the ladder"),
            ("topic", "topic:4d5e6f", "garden plans"),
            ("event", "event:2031-01-02-7a8b9c", "the kettle descaling"),
            ("preference", "preference:9c0d1e", "tea rule"),
            ("trajectory", "trajectory:7e8f9a", "the roof repair"),
            ("", "person:1a2b3c", "Alpha"),
        ];
        // written out here rather than read from fm::SPELLINGS, so a wrong entry
        // there fails
        let spellings: [(&str, &str); 23] = [
            ("person", "person"), ("persons", "person"), ("people", "person"),
            ("place", "place"), ("places", "place"), ("org", "org"), ("orgs", "org"),
            ("group", "group"), ("groups", "group"), ("thing", "thing"), ("things", "thing"),
            ("topic", "topic"), ("topics", "topic"), ("event", "event"), ("events", "event"),
            ("preference", "preference"), ("preferences", "preference"),
            ("pref", "preference"), ("prefs", "preference"),
            ("trajectory", "trajectory"), ("trajectories", "trajectory"),
            ("entity", ""), ("entities", ""),
        ];
        let mut table: Vec<_> = fm::SPELLINGS.to_vec();
        let mut want = spellings.to_vec();
        table.sort();
        want.sort();
        assert_eq!(table, want);
        for (spelling, kind) in spellings {
            let (_, id, name) = named.iter().find(|(k, _, _)| *k == kind).unwrap();
            let local = id.split(':').nth(1).unwrap();
            for sep in [":", "/"] {
                assert_eq!(found(&v, &format!("{spelling}{sep}{local}"), Slot::Writing), *id,
                           "{spelling}{sep}<id>");
                assert_eq!(found(&v, &format!("{spelling}{sep}{name}"), Slot::Writing), *id,
                           "{spelling}{sep}<name>");
            }
        }
    }

    #[test]
    fn an_event_by_its_six_characters() {
        let v = store("tail");
        assert_eq!(found(&v, "7a8b9c", Slot::Writing), "event:2031-01-02-7a8b9c");
        assert_eq!(found(&v, "event:7a8b9c", Slot::Writing), "event:2031-01-02-7a8b9c");
        v.upsert("event:2031-01-05-7a8b9c", "event", "a second", "a second", "", &[], &[], DAY);
        assert!(matches!(v.find("7a8b9c", "", Slot::Reading), Err(Miss::Ambiguous(_))));
        v.upsert("thing:7a8b9c", "thing", "a shadow", "", "", &[], &[], DAY);
        assert_eq!(found(&v, "7a8b9c", Slot::Reading), "thing:7a8b9c", "an exact id comes first");
    }

    #[test]
    fn a_wrong_kind_is_read_and_not_written() {
        let v = store("wrong");
        assert_eq!(found(&v, "topic:9c0d1e", Slot::Reading), "preference:9c0d1e");
        assert!(matches!(v.find("topic:9c0d1e", "", Slot::Writing),
                         Err(Miss::WrongKind { ref node, .. }) if node == "preference:9c0d1e"));
        assert_eq!(found(&v, "topic:7a8b9c", Slot::Reading), "event:2031-01-02-7a8b9c");
        assert!(matches!(v.find("topic:7a8b9c", "", Slot::Writing), Err(Miss::WrongKind { .. })));
        assert_eq!(found(&v, "topic:Alpha", Slot::Reading), "person:1a2b3c");
        assert!(matches!(v.find("topic:Alpha", "", Slot::Writing),
                         Err(Miss::WrongKindName { ref node, .. }) if node == "person:1a2b3c"));
        v.upsert("place:2b3c4d", "place", "Alpha", "", "", &[], &[], DAY);
        for slot in [Slot::Reading, Slot::Writing] {
            assert!(matches!(v.find("topic:Alpha", "", slot), Err(Miss::Ambiguous(_))));
        }
    }

    #[test]
    fn a_kind_and_a_name() {
        let v = store("name");
        assert_eq!(found(&v, "topic:garden plans", Slot::Writing), "topic:4d5e6f");
        assert_eq!(found(&v, "topics/Garden Plans", Slot::Writing), "topic:4d5e6f");
        assert_eq!(found(&v, "[[Alpha]]", Slot::Writing), "person:1a2b3c");
        assert_eq!(found(&v, "event:the kettle descaling", Slot::Writing), "event:2031-01-02-7a8b9c");
        assert_eq!(found(&v, "./topics/garden plans", Slot::Writing), "topic:4d5e6f");
        v.upsert("event:2031-01-04-2c3d4e", "event", "Descaling", "Descaling", "", &[], &[], DAY);
        assert_eq!(found(&v, "event:descaling", Slot::Writing), "event:2031-01-04-2c3d4e");
        assert_eq!(found(&v, "person:alpha", Slot::Writing), "person:1a2b3c",
                   "a legacy-shaped id with no such file is read as a name");
        assert_eq!(found(&v, "entity:Alpha", Slot::Writing), "person:1a2b3c");
        for r in ["topic:greenhouse", "topic:greenhouse plans", "event:the tap repair", "event:gutters"] {
            assert!(matches!(v.find(r, "", Slot::Writing), Err(Miss::NoneByName { .. })), "{r}");
        }
        for r in ["event:2031-04-26-", "topic:topic:3c4d5e", "event:2031-09-20-*x*", "topic:_last",
                  "topic:0d1e2f", "topics/0d1e2f.md"] {
            assert!(matches!(v.find(r, "", Slot::Writing), Err(Miss::NoneById { .. })), "{r}");
        }
    }

    #[test]
    fn a_kind_alone_and_names_that_look_like_one() {
        let v = store("alone");
        assert_eq!(found(&v, "Events", Slot::Reading), "topic:5e6f7a", "a label is found first");
        v.upsert("topic:0a1b2c", "topic", "Event: school fair", "", "", &[], &[], DAY);
        assert_eq!(found(&v, "Event: school fair", Slot::Writing), "topic:0a1b2c");
        v.upsert("topic:1b2c3d", "topic", "Things/odds and ends", "", "", &[], &[], DAY);
        assert_eq!(found(&v, "Things/odds and ends", Slot::Writing), "topic:1b2c3d");
        for (r, want) in [("people", "person"), ("people:", "person"), ("topics/", "topic"),
                          ("pref:*", "preference"), ("entity", "")] {
            assert!(matches!(v.find(r, "", Slot::Reading),
                             Err(Miss::KindAlone { ref kind, .. }) if kind == want), "{r}");
        }
        assert!(["person", "topic", "preference"].iter().all(|k| v.has_nodes(k)));
    }

    #[test]
    fn a_kind_alone_with_no_nodes_of_it() {
        let root = std::env::temp_dir().join(format!("mem-find-empty-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let v = Store(Vault::new(root));
        v.upsert("person:1a2b3c", "person", "Alpha", "", "", &[], &[], DAY);
        for r in ["groups", "groups:", "groups/", "groups*"] {
            assert!(matches!(v.find(r, "", Slot::Reading),
                             Err(Miss::KindAlone { ref kind, .. }) if kind == "group"), "{r}");
        }
        assert!(!v.has_nodes("group"));
    }

    #[test]
    fn names_stay_names() {
        let v = store("names");
        assert_eq!(found(&v, "123456 steps a day", Slot::Reading), "event:2031-01-03-8b9c0d",
                   "a first word that reads as an id and names nothing falls through to a name");
        assert_eq!(found(&v, "rain, wind", Slot::Reading), "topic:6f7a8b");
        for r in ["Re: invoice", "Acme - east branch", "flight BA2490", "garden/shed plans"] {
            assert!(matches!(v.find(r, "", Slot::Writing), Err(Miss::Nothing)), "{r}");
        }
    }

    #[test]
    fn a_legacy_id_bare_or_with_its_kind() {
        let v = store("legacy");
        v.upsert("person:oldname", "person", "Someone Old", "", "", &[], &[], DAY);
        assert_eq!(found(&v, "oldname", Slot::Writing), "person:oldname");
        assert_eq!(found(&v, "person:oldname", Slot::Writing), "person:oldname");
    }

    #[test]
    fn a_list_is_tried_whole_as_a_name_first() {
        let v = store("whole");
        assert_eq!(v.find_whole_name("rain, wind").unwrap(), Some("topic:6f7a8b".into()));
        assert_eq!(v.find_whole_name("topic:rain, wind").unwrap(), Some("topic:6f7a8b".into()));
        assert_eq!(v.find_whole_name("topic:4d5e6f,topic:6f7a8b").unwrap(), None);
        assert_eq!(v.find_whole_name("Alpha, garden plans").unwrap(), None);
        assert!(matches!(v.find_whole_name("topic:Alpha"), Err(Miss::WrongKindName { .. })));
    }

    #[test]
    fn a_write_keeps_every_other_call_out_and_reads_share() {
        let v = store("lock");
        let other = std::fs::File::open(&v.root).unwrap();
        let writing = v.lock(true, std::time::Duration::from_secs(10)).unwrap();
        assert!(other.try_lock_shared().is_err(), "a read waits for a write");
        drop(writing);
        let reading = v.lock(false, std::time::Duration::from_secs(10)).unwrap();
        assert!(other.try_lock_shared().is_ok(), "two reads at once");
        other.unlock().unwrap();
        assert!(other.try_lock().is_err(), "a write waits for a read");
        drop(reading);
        assert!(other.try_lock().is_ok());
    }

    #[test]
    fn a_lock_not_had_in_time_is_given_up_and_one_let_go_in_time_is_had() {
        let v = store("limit");
        let other = std::fs::File::open(&v.root).unwrap();
        other.lock().unwrap();
        let began = std::time::Instant::now();
        let e = v.lock(false, std::time::Duration::from_millis(300)).unwrap_err();
        assert_eq!(e.kind(), std::io::ErrorKind::TimedOut, "{e}");
        assert!(began.elapsed() >= std::time::Duration::from_millis(300));
        let letting_go = std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(300));
            drop(other);
        });
        let writing = v.lock(true, std::time::Duration::from_secs(10)).unwrap();
        letting_go.join().unwrap();
        let after = std::fs::File::open(&v.root).unwrap();
        assert!(after.try_lock_shared().is_err(), "held once had");
        drop(writing);
    }

    #[test]
    fn a_vault_kept_across_calls_reports_its_failed_write_after_each() {
        use crate::index::{regenerate_indexes, set_templates};
        set_templates(PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/templates")));
        let v = store("written");
        regenerate_indexes(&v).unwrap();
        assert_eq!(v.written(), Ok(()));
        let root = v.root.join("CLAUDE.md");
        // a directory where the people index's text goes before it takes the name
        let part = v.root.join("people").join(".CLAUDE.md.part");
        std::fs::create_dir(&part).unwrap();
        regenerate_indexes(&v).unwrap();
        let e = v.written().unwrap_err();
        assert!(e.starts_with("the store could not be written at people/CLAUDE.md: "), "{e}");
        assert!(e.ends_with("; nothing after that was written"), "{e}");
        std::fs::remove_dir(&part).unwrap();
        // the block gone, the same vault still writes nothing, and says so again
        std::fs::write(&root, "stale\n").unwrap();
        regenerate_indexes(&v).unwrap();
        assert_eq!(v.written(), Err(e));
        assert_eq!(std::fs::read_to_string(&root).unwrap(), "stale\n");
        let fresh = Vault::new(v.root.clone());
        regenerate_indexes(&fresh).unwrap();
        assert_eq!(fresh.written(), Ok(()));
        assert_ne!(std::fs::read_to_string(&root).unwrap(), "stale\n");
    }

    #[test]
    fn resolve_reads_and_keeps_its_signature() {
        let v = store("resolve");
        assert_eq!(v.resolve("pref:9c0d1e", "").unwrap(), Some("preference:9c0d1e".into()));
        assert_eq!(v.resolve("topic:9c0d1e", "").unwrap(), Some("preference:9c0d1e".into()));
        assert_eq!(v.resolve("people", "").unwrap(), None);
        assert_eq!(v.resolve("nobody here", "").unwrap(), None);
    }

    /// A store written by session `s1`, in a fresh directory.
    fn by_s1(tag: &str) -> Store {
        let root = std::env::temp_dir().join(format!("mem-mint-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let mut v = Vault::new(root);
        v.session = "s1".into();
        Store(v)
    }

    /// A rebuild of `run` by session `s1`, with the ids `s1` printed, in order,
    /// and those the tool call being replayed printed.
    fn rebuild_of(run: &Vault, tag: &str, order: &[&str], call: &[&str]) -> Store {
        let mut v = by_s1(tag);
        let ids = |xs: &[&str]| xs.iter().map(|x| x.to_string()).collect();
        v.0.oracle = Some(Box::new(Vault::new(run.root.clone())));
        v.0.oracle_order = Some(HashMap::from(
            [("s1".to_string(), ids(order)), (ORACLE_CALL.to_string(), ids(call))]));
        v
    }

    #[test]
    fn a_forgotten_node_takes_the_id_its_call_printed() {
        let run = by_s1("forgot-run");
        let (forgot, again) = ("event:2031-01-02-1a2b3c", "event:2031-01-02-2b3c4d");
        run.upsert(again, "event", "lunch", "lunch", "", &[], &[], DAY);
        let order = [forgot, again];
        let v = rebuild_of(&run, "forgot", &order, &[forgot]);
        assert_eq!(v.mint("event", "2031-01-02", None, "lunch"), forgot,
                   "not the id of the node made again later");
        let v = rebuild_of(&run, "again", &order, &[again]);
        assert_eq!(v.mint("event", "2031-01-02", None, "lunch"), again);
        let v = rebuild_of(&run, "both", &order, &[forgot, again]);
        assert_eq!(v.mint("event", "2031-01-02", None, "lunch"), again,
                   "the call printed the id the oracle names as well");
        let v = rebuild_of(&run, "none", &order, &[]);
        assert_eq!(v.mint("event", "2031-01-02", None, "lunch"), again);
    }

    #[test]
    fn a_forgotten_id_is_read_by_kind_and_date_alone() {
        let run = by_s1("kind-run");
        let call = ["person:3c4d5e", "event:2031-01-03-4d5e6f", "event:2031-01-02-5e6f7a",
                    "event:2031-01-02-6f7a8b"];
        let v = rebuild_of(&run, "kind", &call, &call);
        assert_eq!(v.mint("event", "2031-01-02", None, "anything"), "event:2031-01-02-5e6f7a");
        v.upsert("event:2031-01-02-5e6f7a", "event", "anything", "anything", "", &[], &[], DAY);
        assert_eq!(v.mint("event", "2031-01-02", None, "else"), "event:2031-01-02-6f7a8b",
                   "one held here is passed over");
        assert_eq!(v.mint("person", "", None, "Alpha"), "person:3c4d5e");
        let topic = v.mint("topic", "", None, "garden");
        assert!(topic.starts_with("topic:") && !call.contains(&topic.as_str()), "{topic}");
        assert!(!run.root.join("events").exists(), "the run's vault is listed, never written");
    }

    #[test]
    fn a_node_the_oracle_names_keeps_its_id_where_its_session_never_printed_it() {
        let run = by_s1("names-run");
        run.upsert("person:7a8b9c", "person", "Alpha", "", "", &[], &[], DAY);
        let other = "person:8b9c0d";
        let v = rebuild_of(&run, "names", &[other], &[other]);
        assert_eq!(v.mint("person", "", None, "Alpha"), "person:7a8b9c",
                   "made beside another node, its output sent elsewhere");
        let v = rebuild_of(&run, "names-later", &[other, "person:7a8b9c"], &[other]);
        assert_eq!(v.mint("person", "", None, "Alpha"), other);
        let v = rebuild_of(&run, "names-earlier", &["person:7a8b9c", other], &[other]);
        assert_eq!(v.mint("person", "", None, "Alpha"), other,
                   "printed by an earlier call, which the rebuild did not make it in");
    }

    #[test]
    fn a_stub_its_call_does_not_print_keeps_the_id_the_oracle_names() {
        let run = by_s1("stub-run");
        run.upsert("person:7a8b9c", "person", "Alpha", "", "", &[], &[], DAY);
        let other = "person:8b9c0d";
        let v = rebuild_of(&run, "stub", &[other, "person:7a8b9c"], &[other]);
        assert_eq!(v.mint_shown("person", "", None, "Alpha", false), "person:7a8b9c",
                   "its session printed it later, restating it");
        assert_eq!(v.mint_shown("person", "", None, "Beta", false), other,
                   "a stub the run forgot takes what its tool call printed");
    }

    #[test]
    fn live_a_mint_ignores_what_it_is_offered_or_told() {
        let mut v = by_s1("live");
        let offered = vec!["person:8b9c0d".to_string()];
        v.0.oracle_order = Some(HashMap::from(
            [("s1".to_string(), offered.clone()), (ORACLE_CALL.to_string(), offered)]));
        for shown in [true, false] {
            let id = v.mint_shown("person", "", None, "Alpha", shown);
            assert!(id.starts_with("person:") && id != "person:8b9c0d", "{id}");
        }
    }
}
