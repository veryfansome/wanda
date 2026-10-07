//! What `mem trajectory` and `mem advance` say back about the `--by` they were
//! given.

use std::path::{Path, PathBuf};
use std::process::Command;

/// A vault in a fresh directory, removed when the test ends.
struct Store(PathBuf);

impl Drop for Store {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn store(tag: &str) -> Store {
    let root = std::env::temp_dir().join(format!("mem-by-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    Store(root)
}

/// The lines a call printed that echo its `--by`.
fn echoed(vault: &Path, date: &str, args: &[&str]) -> Vec<String> {
    let o = Command::new(env!("CARGO_BIN_EXE_mem"))
        .args(args)
        .env("MEM_VAULT", vault)
        .env("MEM_DATE", date)
        .env("MEM_REAL_DATE", date)
        .env("MEM_TEMPLATES", concat!(env!("CARGO_MANIFEST_DIR"), "/templates"))
        .env_remove("LAB_MEMLOG")
        .env_remove("MEM_SESSION")
        .env_remove("MEM_ORACLE")
        .output().unwrap();
    let out = String::from_utf8_lossy(&o.stdout).into_owned();
    assert_eq!(o.status.code(), Some(0), "{out}");
    out.lines().filter(|l| l.starts_with("(--by ")).map(String::from).collect()
}

#[test]
fn a_trajectory_and_an_advance_name_the_weekday_of_their_by() {
    let s = store("echo");
    let today = "2026-10-06";
    assert_eq!(echoed(&s.0, today, &["trajectory", "--summary", "the conference", "--expect",
                                     "it happens", "--by", "2026-10-21", "--about", "Alpha"]),
               ["(--by 2026-10-21, a Wednesday, is 15 days after today, 2026-10-06)"]);
    assert_eq!(echoed(&s.0, today, &["advance", "the conference", "--by", "2026-10-21T09:00"]),
               ["(--by 2026-10-21T09:00, a Wednesday, is 15 days after today, 2026-10-06)"]);
    assert_eq!(echoed(&s.0, today, &["advance", "the conference", "--by", "2026-10-07"]),
               ["(--by 2026-10-07, a Wednesday, is 1 day after today, 2026-10-06)"]);
    assert_eq!(echoed(&s.0, today, &["advance", "the conference", "--by", "2026-10-06"]),
               ["(--by 2026-10-06 is today, 2026-10-06)"]);
    assert_eq!(echoed(&s.0, today, &["advance", "the conference", "--by", "2026-10-05"]),
               ["(--by 2026-10-05, a Monday, is 1 day before today, 2026-10-06)"]);
    // an advance with no --by says nothing about one
    assert!(echoed(&s.0, today, &["advance", "the conference", "--note", "moved"]).is_empty());
}

#[test]
fn with_no_today_a_by_is_written_and_nothing_is_echoed() {
    let s = store("no-today");
    assert!(echoed(&s.0, "", &["trajectory", "--summary", "the conference", "--expect",
                               "it happens", "--by", "2026-10-21", "--about", "Alpha"]).is_empty());
    assert!(echoed(&s.0, "", &["advance", "the conference", "--by", "2026-10-22"]).is_empty());
}
