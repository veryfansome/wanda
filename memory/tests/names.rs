//! `mem session --with` and `mem due --for` finding a person by a name they
//! had before a rename, where that name still finds them alone.

use memory::fm::Edge;
use memory::vault::Vault;
use std::path::PathBuf;
use std::process::Command;

const DAY: &str = "2031-01-10";

/// A vault and its transcripts in a fresh directory, removed when the test
/// ends.
struct Store(PathBuf);

impl Drop for Store {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn store(tag: &str) -> Store {
    let root = std::env::temp_dir().join(format!("mem-names-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(root.join("vault")).unwrap();
    std::fs::create_dir_all(root.join("transcripts")).unwrap();
    Store(root)
}

fn dm(who: &str, text: &str) -> String {
    format!("{who} says to me, in a direct message:\n\n    {text}")
}

const GROUP: &str = "In a group direct message that fan, mei and I read. Everyone in it sees what I say there.";

impl Store {
    fn vault(&self) -> Vault {
        Vault::new(self.0.join("vault"))
    }

    /// One exchange as Claude Code keeps it: the product's frame for what
    /// arrived, a message from each of `added` taken in while it worked, and
    /// an answer.
    fn exchange(&self, sid: &str, minute: u32, arrival: &str, added: &[&str]) {
        let at = format!("{DAY}T09:{minute:02}:00Z");
        let prompt = format!("I am wanda.\n\nToday is {DAY}.\n\n{arrival}\n\nDo three things, in this order.\n");
        let mut lines = vec![serde_json::json!({"type": "user", "timestamp": at,
                                                "message": {"content": prompt}})];
        for who in added {
            lines.push(serde_json::json!({"type": "attachment", "timestamp": at, "attachment": {
                "type": "queued_command", "commandMode": "prompt", "prompt": [{"type": "text", "text": format!(
                    "{who} adds this in the same group direct message at 09:00, before anything I say \
                     back has been sent:\n\n    me too\n\nNothing I have said back in this session has \
                     been sent yet. The last answer I give in this session that says something is the \
                     one sent, so that is where anything said here gets its answer.")}]}}));
        }
        lines.push(serde_json::json!({"type": "attachment", "timestamp": at, "attachment": {
            "type": "structured_output", "data": {"answer": "Noted.", "recalled": [], "recorded": []}}}));
        let text: String = lines.iter().map(|l| format!("{l}\n")).collect();
        std::fs::write(self.0.join("transcripts").join(format!("{sid}.jsonl")), text).unwrap();
    }

    fn mem(&self, args: &[&str]) -> (i32, String) {
        let o = Command::new(env!("CARGO_BIN_EXE_mem"))
            .args(args)
            .env("MEM_VAULT", self.0.join("vault"))
            .env("MEM_TRANSCRIPTS", self.0.join("transcripts"))
            .env("MEM_DATE", DAY)
            .env("MEM_REAL_DATE", DAY)
            .env("MEM_TEMPLATES", concat!(env!("CARGO_MANIFEST_DIR"), "/templates"))
            .env_remove("LAB_MEMLOG")
            .env_remove("MEM_SESSION")
            .env_remove("MEM_ORACLE")
            .output().unwrap();
        (o.status.code().unwrap_or(-1), String::from_utf8_lossy(&o.stdout).into_owned())
    }

    /// The sessions `mem session --with <name>` lists, oldest first.
    fn with(&self, name: &str) -> Vec<String> {
        let (rc, out) = self.mem(&["session", "--with", name]);
        if rc == 1 && out.trim_end() == "(no exchanges match)" {
            return Vec::new();
        }
        assert_eq!(rc, 0, "{out}");
        out.lines().filter(|l| !l.starts_with(' '))
            .map(|l| l.split_whitespace().next().unwrap_or("").to_string()).collect()
    }
}

#[test]
fn with_lists_a_persons_exchanges_under_each_name_they_had() {
    let s = store("renamed");
    let v = s.vault();
    v.upsert("person:1a2b3c", "person", "fzhu", "", "", &[], &[], DAY);
    v.upsert("person:2b3c4d", "person", "mei", "", "", &[], &[], DAY);
    s.exchange("fzhu", 1, &dm("fzhu", "the boiler again"), &[]);
    s.exchange("fan", 2, &dm("fan", "lunch at one?"), &[]);
    s.exchange("stefan", 3, &dm("Stefan", "is the hall free on Friday"), &[]);
    s.exchange("then", 4, &format!("{GROUP}\n\nfan says:\n\n    lunch at one?"), &["mei"]);
    s.exchange("after", 5, &format!("{GROUP}\n\nThe conversation so far:\n\n    08:58 fan: lunch at \
                                     one?\n\nmei now says, after fan:\n\n    yes"), &[]);
    s.exchange("mei", 6, &dm("mei", "home by six"), &[]);
    // before the rename, each name lists its own exchanges alone
    assert_eq!(s.with("fzhu"), ["fzhu"]);
    assert_eq!(s.with("fan"), ["fan", "stefan", "then", "after"]);
    v.rename("person:1a2b3c", "fan", "", "", DAY);
    // the name given still matches inside a longer one, as before; the other
    // name only whole, so not Stefan, but each person a turn names
    assert_eq!(s.with("fzhu"), ["fzhu", "fan", "then", "after"]);
    assert_eq!(s.with("FZHU"), ["fzhu", "fan", "then", "after"]);
    assert_eq!(s.with("fan"), ["fzhu", "fan", "stefan", "then", "after"]);
    assert_eq!(s.with("mei"), ["then", "after", "mei"]);
}

// her own node, a thing, and a name two people answer to are not followed
#[test]
fn with_matches_the_name_alone_where_it_finds_no_one_person() {
    let s = store("alone");
    let v = s.vault();
    // her node as an older vault labelled it, renamed to her label now
    v.upsert("person:3c4d5e", "person", "wanda", "", "", &[], &[], DAY);
    v.rename("person:3c4d5e", "me", "", "", DAY);
    v.upsert("person:2b3c4d", "person", "mei", "", "", &[], &[], DAY);
    v.upsert("thing:4d5e6f", "thing", "Pip", "", "", &[], &[], DAY);
    v.rename("thing:4d5e6f", "Comet", "", "", DAY);
    v.upsert("person:7a8b9c", "person", "Bo", "", "", &[], &[], DAY);
    v.upsert("person:8b9c0d", "person", "Al", "", "", &[], &[], DAY);
    v.rename("person:8b9c0d", "Bo", "", "", DAY);
    for (i, (sid, who)) in [("wanda", "Wanda"), ("mei", "mei"), ("pip", "Pip"), ("bo", "Bo"), ("al", "Al")]
        .into_iter().enumerate()
    {
        s.exchange(sid, i as u32, &dm(who, "hello"), &[]);
    }
    assert_eq!(s.with("me"), ["mei"]);
    assert_eq!(s.with("wanda"), ["wanda"]);
    assert_eq!(s.with("Comet"), Vec::<String>::new());
    assert_eq!(s.with("Bo"), ["bo"]);
}

// a cousin renamed from the member's name keeps it struck: that name finds
// both, so it counts for neither, and each is found by the name they have now
#[test]
fn a_name_another_person_had_follows_no_one() {
    let s = store("cousin");
    let v = s.vault();
    v.upsert("person:1a2b3c", "person", "fan", "", "", &[], &[], DAY);
    v.upsert("person:4e5f6a", "person", "fan", "", "", &[], &[], DAY);
    v.rename("person:4e5f6a", "Fan Li", "", "", DAY);
    for (id, summary, who) in [("trajectory:5f6a7b", "call the plumber", "person:1a2b3c"),
                               ("trajectory:6a7b8c", "lend the ladder back", "person:4e5f6a")] {
        v.upsert(id, "trajectory", summary, summary, "",
                 &[("expect".into(), "done".into()), ("expect_by".into(), DAY.into()),
                   ("status".into(), "open".into())],
                 &[Edge { rel: "involves".into(), to: who.into() }], DAY);
    }
    s.exchange("fan", 1, &dm("fan", "the plumber comes at ten"), &[]);
    s.exchange("li", 2, &dm("Fan Li", "can I keep the ladder"), &[]);
    let (rc, look) = s.mem(&["due", "--for=fan", "--after", "2031-01-09"]);
    assert_eq!((rc, look.as_str()), (0, "Come due for fan after 2031-01-09:\n\
                                         `trajectory:5f6a7b`  2031-01-10, today  call the plumber\n    \
                                         involves: fan\n"));
    assert_eq!(s.with("Fan Li"), ["li"]);
}

// an earlier name given only as the speaker of a message added while a
// session worked still lists that exchange under the name they have now
#[test]
fn with_follows_a_name_given_only_by_an_added_message() {
    let s = store("added");
    let v = s.vault();
    v.upsert("person:1a2b3c", "person", "fzhu", "", "", &[], &[], DAY);
    v.upsert("person:2b3c4d", "person", "mei", "", "", &[], &[], DAY);
    s.exchange("meithen", 1, &format!("{GROUP}\n\nmei says:\n\n    lunch at one?"), &["fzhu"]);
    v.rename("person:1a2b3c", "fan", "", "", DAY);
    assert_eq!(s.with("fan"), ["meithen"]);
}
