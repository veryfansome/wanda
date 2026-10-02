//! `mem` calls made at once in one vault, as sessions running side by side
//! make them.

use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

/// A vault in a fresh directory, removed when the test ends.
struct Store(PathBuf);

impl Drop for Store {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn store(tag: &str) -> Store {
    let root = std::env::temp_dir().join(format!("mem-together-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    Store(root)
}

fn mem(vault: &Path, args: &[String]) -> Command {
    let mut c = Command::new(env!("CARGO_BIN_EXE_mem"));
    c.args(args)
        .env("MEM_VAULT", vault)
        .env("MEM_DATE", "2031-01-10")
        .env("MEM_REAL_DATE", "2031-01-10")
        .env("MEM_TEMPLATES", concat!(env!("CARGO_MANIFEST_DIR"), "/templates"))
        .env_remove("LAB_MEMLOG")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    c
}

/// Every call started before any is waited for.
fn together(vault: &Path, calls: &[Vec<String>]) -> Vec<Output> {
    let started: Vec<_> = calls.iter().map(|a| mem(vault, a).spawn().unwrap()).collect();
    started.into_iter().map(|c| c.wait_with_output().unwrap()).collect()
}

fn args(a: &[&str]) -> Vec<String> {
    a.iter().map(|s| s.to_string()).collect()
}

fn nodes_in(dir: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(dir).unwrap().flatten()
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| n != "CLAUDE.md")
        .collect();
    names.sort();
    names
}

#[test]
fn writes_at_once_to_one_name_make_one_node_and_keep_every_line() {
    let s = store("one");
    let calls: Vec<Vec<String>> = (0..12)
        .map(|i| args(&["entity", "--kind", "person", "--name", "Alpha", "--body",
                        &format!("Line {i} is here.")]))
        .collect();
    for o in together(&s.0, &calls) {
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stdout));
    }
    let people = nodes_in(&s.0.join("people"));
    assert_eq!(people.len(), 1, "{people:?}");
    let text = std::fs::read_to_string(s.0.join("people").join(&people[0])).unwrap();
    for i in 0..12 {
        assert!(text.contains(&format!("Line {i} is here.")), "{text}");
    }
}

#[test]
fn reads_beside_writes_all_answer_and_the_index_has_every_write() {
    let s = store("reads");
    assert!(mem(&s.0, &args(&["entity", "--kind", "person", "--name", "Alpha"]))
        .output().unwrap().status.success());
    let mut calls: Vec<Vec<String>> = (0..8)
        .map(|i| args(&["entity", "--kind", "topic", "--name", &format!("topic {i}")]))
        .collect();
    calls.extend((0..16).map(|_| args(&["recall", "Alpha"])));
    let outs = together(&s.0, &calls);
    for o in &outs[8..] {
        let out = String::from_utf8_lossy(&o.stdout);
        assert!(o.status.success() && out.starts_with("expanded from 1"), "{out}");
    }
    let index = std::fs::read_to_string(s.0.join("topics").join("CLAUDE.md")).unwrap();
    for i in 0..8 {
        assert!(index.contains(&format!("topic {i}")), "{index}");
    }
    assert_eq!(nodes_in(&s.0.join("topics")).len(), 8);
    assert!(!s.0.join(".index.db").exists());
}

/// The vault held exclusively by this test, as a stopped call would hold it,
/// until the file returned is closed.
fn hold(vault: &Path) -> std::fs::File {
    let holder = std::fs::File::open(vault).unwrap();
    holder.lock().unwrap();
    holder
}

#[test]
fn a_call_kept_waiting_goes_through_once_the_vault_is_let_go() {
    let s = store("wait");
    let holder = hold(&s.0);
    let mut call = mem(&s.0, &args(&["entity", "--kind", "person", "--name", "Alpha"]))
        .spawn().unwrap();
    std::thread::sleep(std::time::Duration::from_secs(2));
    assert!(call.try_wait().unwrap().is_none(), "the call waits while the vault is held");
    drop(holder);
    let o = call.wait_with_output().unwrap();
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stdout));
    assert_eq!(nodes_in(&s.0.join("people")).len(), 1);
}

#[test]
#[ignore = "waits out the 90 s a call waits for the vault; run with --ignored"]
fn a_call_kept_waiting_past_its_limit_is_refused_and_writes_nothing() {
    let s = store("limit");
    let _holder = hold(&s.0);
    let began = std::time::Instant::now();
    let o = mem(&s.0, &args(&["entity", "--kind", "person", "--name", "Alpha"]))
        .output().unwrap();
    let waited = began.elapsed();
    let out = String::from_utf8_lossy(&o.stdout);
    assert_eq!(o.status.code(), Some(1), "{out}");
    assert_eq!(out.trim_end(), "(the store could not be held for this call: it stayed busy \
                                for 90 s; nothing was read or written)");
    assert!(waited >= std::time::Duration::from_secs(90)
            && waited < std::time::Duration::from_secs(100), "{waited:?}");
    assert_eq!(std::fs::read_dir(&s.0).unwrap().count(), 0, "nothing written");
}
