//! Writes the store cannot take: each ends its call with exit 1 and a sentence
//! saying so, never with `ok`.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

/// A vault in a fresh directory, removed when the test ends.
struct Store(PathBuf);

impl Drop for Store {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn store(tag: &str) -> Store {
    let root = std::env::temp_dir().join(format!("mem-unwritable-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    Store(root)
}

fn mem(vault: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_mem"))
        .args(args)
        .env("MEM_VAULT", vault)
        .env("MEM_DATE", "2031-01-10")
        .env("MEM_REAL_DATE", "2031-01-10")
        .env("MEM_TEMPLATES", concat!(env!("CARGO_MANIFEST_DIR"), "/templates"))
        .env_remove("LAB_MEMLOG")
        .output()
        .unwrap()
}

fn said(o: &Output) -> String {
    String::from_utf8_lossy(&o.stdout).into_owned()
}

fn read(p: &Path) -> String {
    std::fs::read_to_string(p).unwrap_or_default()
}

/// The one node file in a kind's directory.
fn node(dir: &Path) -> PathBuf {
    let mut found: Vec<PathBuf> = std::fs::read_dir(dir).unwrap().flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "md")
                    && p.file_name().is_some_and(|n| n != "CLAUDE.md"))
        .collect();
    assert_eq!(found.len(), 1, "{found:?}");
    found.pop().unwrap()
}

/// A directory named `.<name>.part` beside `file`, the file a write of `file`
/// fills before renaming it into place, so that write fails for any user, root
/// included.
fn block(file: &Path) {
    let name = file.file_name().unwrap().to_string_lossy();
    std::fs::create_dir(file.with_file_name(format!(".{name}.part"))).unwrap();
}

/// What the call says when the write of `rel` fails.
fn refused_at(o: &Output, rel: &str) {
    let out = said(o);
    assert_eq!(o.status.code(), Some(1), "{out}");
    assert!(out.starts_with(&format!("(the store could not be written at {rel}: ")), "{out}");
    assert!(out.trim_end().ends_with("; nothing after that was written, and what this call \
                                      wrote before it stays)"), "{out}");
    assert!(!out.contains("ok "), "{out}");
}

#[test]
fn a_node_the_store_cannot_take_stays_as_it_was() {
    let s = store("node");
    assert!(mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha"]).status.success());
    let alpha = node(&s.0.join("people"));
    let before = read(&alpha);
    block(&alpha);
    let o = mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha",
                        "--body", "Alpha keeps bees."]);
    refused_at(&o, &format!("people/{}", alpha.file_name().unwrap().to_string_lossy()));
    assert_eq!(read(&alpha), before);
}

#[test]
fn an_index_the_store_cannot_take_leaves_the_node_written() {
    let s = store("index");
    assert!(mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha"]).status.success());
    let index = s.0.join("people").join("CLAUDE.md");
    let before = read(&index);
    block(&index);
    let o = mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha",
                        "--body", "Alpha keeps bees."]);
    refused_at(&o, "people/CLAUDE.md");
    assert!(read(&node(&s.0.join("people"))).contains("Alpha keeps bees."));
    assert_eq!(read(&index), before);
}

#[test]
fn a_two_file_write_stops_at_the_file_it_cannot_write() {
    let relate = ["relate", "--subject", "Alpha", "--rel", "owns", "--object", "garden plans",
                  "--inverse", "owned_by"];
    for (tag, blocked_first) in [("second", false), ("first", true)] {
        let s = store(tag);
        assert!(mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha"]).status.success());
        assert!(mem(&s.0, &["entity", "--kind", "topic", "--name", "garden plans"])
            .status.success());
        let (alpha, plans) = (node(&s.0.join("people")), node(&s.0.join("topics")));
        let before = (read(&alpha), read(&plans));
        let (blocked, dir) = if blocked_first { (&alpha, "people") } else { (&plans, "topics") };
        block(blocked);
        let o = mem(&s.0, &relate);
        refused_at(&o, &format!("{dir}/{}", blocked.file_name().unwrap().to_string_lossy()));
        // the subject is written first: it keeps its edge when only the
        // object fails, and when the subject fails the object is not touched
        assert_eq!(read(&alpha).contains("owns"), !blocked_first);
        assert_eq!(read(&plans), before.1);
    }
}

#[test]
fn a_node_file_that_cannot_be_read_is_not_written_over() {
    let s = store("unread");
    assert!(mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha"]).status.success());
    assert!(mem(&s.0, &["entity", "--kind", "topic", "--name", "garden plans"]).status.success());
    let alpha = node(&s.0.join("people"));
    // bytes that are not UTF-8 cannot be read as text by any user, root included
    let mut before = std::fs::read(&alpha).unwrap();
    before.extend_from_slice(b"Alpha \xff keeps bees.\n");
    std::fs::write(&alpha, &before).unwrap();
    let id = format!("person:{}", alpha.file_stem().unwrap().to_string_lossy());
    let o = mem(&s.0, &["relate", "--subject", &id, "--rel", "owns", "--object", "garden plans"]);
    refused_at(&o, &format!("people/{}", alpha.file_name().unwrap().to_string_lossy()));
    assert_eq!(std::fs::read(&alpha).unwrap(), before);
}

#[test]
fn a_node_file_that_cannot_be_read_is_not_renamed() {
    let s = store("rename");
    assert!(mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha"]).status.success());
    let alpha = node(&s.0.join("people"));
    let mut before = std::fs::read(&alpha).unwrap();
    before.extend_from_slice(b"Alpha \xff keeps bees.\n");
    std::fs::write(&alpha, &before).unwrap();
    let id = format!("person:{}", alpha.file_stem().unwrap().to_string_lossy());
    let o = mem(&s.0, &["rename", &id, "Beta"]);
    refused_at(&o, &format!("people/{}", alpha.file_name().unwrap().to_string_lossy()));
    assert_eq!(std::fs::read(&alpha).unwrap(), before);
}

#[test]
fn a_two_file_retract_stops_at_the_file_it_cannot_read() {
    let s = store("retract");
    assert!(mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha"]).status.success());
    assert!(mem(&s.0, &["entity", "--kind", "topic", "--name", "garden plans"]).status.success());
    assert!(mem(&s.0, &["relate", "--subject", "Alpha", "--rel", "owns", "--object", "garden plans",
                        "--inverse", "owned_by"]).status.success());
    let (alpha, plans) = (node(&s.0.join("people")), node(&s.0.join("topics")));
    let mut before = std::fs::read(&plans).unwrap();
    before.extend_from_slice(b"Plans \xff for the beds.\n");
    std::fs::write(&plans, &before).unwrap();
    let id = format!("topic:{}", plans.file_stem().unwrap().to_string_lossy());
    let o = mem(&s.0, &["retract", "--subject", "Alpha", "--rel", "owns", "--object", &id,
                        "--inverse", "owned_by"]);
    refused_at(&o, &format!("topics/{}", plans.file_name().unwrap().to_string_lossy()));
    // the subject is written first and keeps its half, as when the second
    // file of any two-file write fails
    assert!(!read(&alpha).contains("owns"));
    assert_eq!(std::fs::read(&plans).unwrap(), before);
}

#[cfg(unix)]
#[test]
fn a_directory_this_user_may_not_write_fails_the_write() {
    use std::os::unix::fs::PermissionsExt;
    let s = store("mode");
    assert!(mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha"]).status.success());
    let people = s.0.join("people");
    let alpha = node(&people);
    let before = read(&alpha);
    std::fs::set_permissions(&people, std::fs::Permissions::from_mode(0o555)).unwrap();
    // root writes it anyway, and then this proves nothing
    let probe = people.join("probe");
    let ignored = std::fs::write(&probe, "").is_ok();
    let o = (!ignored).then(|| mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha",
                                           "--body", "Alpha keeps bees."]));
    let _ = std::fs::remove_file(&probe);
    std::fs::set_permissions(&people, std::fs::Permissions::from_mode(0o755)).unwrap();
    if let Some(o) = o {
        refused_at(&o, &format!("people/{}", alpha.file_name().unwrap().to_string_lossy()));
        assert!(said(&o).contains("Permission denied"), "{}", said(&o));
        assert_eq!(read(&alpha), before);
    }
}

#[cfg(unix)]
#[test]
fn a_node_forget_cannot_remove_stays() {
    use std::os::unix::fs::PermissionsExt;
    let s = store("forget");
    assert!(mem(&s.0, &["entity", "--kind", "person", "--name", "Alpha"]).status.success());
    let people = s.0.join("people");
    let alpha = node(&people);
    let id = format!("person:{}", alpha.file_stem().unwrap().to_string_lossy());
    std::fs::set_permissions(&people, std::fs::Permissions::from_mode(0o555)).unwrap();
    // root removes it anyway, and then this proves nothing
    let probe = people.join("probe");
    let ignored = std::fs::write(&probe, "").is_ok();
    let o = (!ignored).then(|| mem(&s.0, &["forget", &id]));
    let _ = std::fs::remove_file(&probe);
    std::fs::set_permissions(&people, std::fs::Permissions::from_mode(0o755)).unwrap();
    if let Some(o) = o {
        refused_at(&o, &format!("people/{}", alpha.file_name().unwrap().to_string_lossy()));
        assert!(said(&o).contains("Permission denied"), "{}", said(&o));
        assert!(alpha.exists(), "the node is still there");
    }
}
