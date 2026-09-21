//! Replay a scripted history, one session per arrival, and write down what
//! each one recalled.
//!
//! The arrivals arrive on stdin, already ordered and already shifted; whoever
//! prepared them holds the history, and this process is given the arrivals and
//! nothing else. Every session is a Claude Code session with the vault as its
//! working directory and `mem` as its only way into the store.

use harness::arrival::{check_prompt_shape, Input};
use harness::session::{
    install_mem, placeholder_fields, read_trace, resolve, run_session, summarise_trace,
    write_session_config, write_trace,
};
use memory::vault::Vault;
use memory::{index, transcript};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

fn py_dict(items: &[(String, Value)]) -> String {
    let shown: Vec<String> = items.iter().map(|(k, v)| {
        let val = match v {
            Value::String(s) => memory::text::py_repr(s),
            other => other.to_string(),
        };
        format!("{}: {val}", memory::text::py_repr(k))
    }).collect();
    format!("{{{}}}", shown.join(", "))
}

struct Args {
    inputs: String,
    vault: PathBuf,
    out: PathBuf,
    timeout: u64,
    limit: usize,
}

fn parse_args() -> Args {
    let mut a = Args {
        inputs: "-".into(),
        vault: PathBuf::from("runs/vault"),
        out: PathBuf::from("runs/report.md"),
        timeout: 420,
        limit: 0,
    };
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < argv.len() {
        let next = || argv.get(i + 1).cloned().unwrap_or_default();
        match argv[i].as_str() {
            "--inputs" => { a.inputs = next(); i += 1 }
            "--vault" => { a.vault = PathBuf::from(next()); i += 1 }
            "--out" => { a.out = PathBuf::from(next()); i += 1 }
            "--timeout" => { a.timeout = next().parse().unwrap_or(420); i += 1 }
            "--limit" => { a.limit = next().parse().unwrap_or(0); i += 1 }
            other => { eprintln!("unknown argument {other}"); std::process::exit(2) }
        }
        i += 1;
    }
    a
}

fn append(path: &Path, value: &Value) {
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "{}", memory::text::py_json(value));
    }
}

fn main() {
    std::process::exit(match run() {
        Ok(rc) => rc,
        Err(e) => { eprintln!("{e}"); 2 }
    });
}

fn run() -> Result<i32, String> {
    check_prompt_shape()?;
    let args = parse_args();

    let raw = if args.inputs == "-" {
        let mut s = String::new();
        std::io::stdin().read_to_string(&mut s).map_err(|e| e.to_string())?;
        s
    } else {
        std::fs::read_to_string(&args.inputs).map_err(|e| e.to_string())?
    };
    let payload: Value = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
    let anchor = payload.get("anchor").cloned().unwrap_or(Value::Null);
    let history_digest = payload.get("history").cloned().unwrap_or(json!(""));
    let mut inputs: Vec<Input> = serde_json::from_value(payload["inputs"].clone())
        .map_err(|e| e.to_string())?;
    if inputs.is_empty() {
        eprintln!("no inputs");
        return Ok(2);
    }
    if args.limit > 0 {
        inputs.truncate(args.limit);
    }
    // absolute throughout: the session's cwd is the vault, so a relative path
    // in the environment resolves inside it and writes a nested duplicate
    // vault that swallows everything written to it
    let out_path = resolve(&args.out)?;
    let vault_path = resolve(&args.vault)?;
    let out_dir = out_path.parent().unwrap_or(Path::new(".")).to_path_buf();

    // before the directory is claimed and before anything is written
    let head = std::env::var("LAB_REV").unwrap_or_default();
    if head.is_empty() || head == "unknown" {
        eprintln!("LAB_REV is not set: this run cannot name the code that made it.\n\
                   take it from the build: LAB_REV=$(docker compose run --rm -T builder \
                   python3 /work/lab/build.py)");
        return Ok(2);
    }
    // one directory, one pass. Everything written afterwards lands here too,
    // and the whole directory is given to whatever runs against it next.
    // Anything already here means this directory has been used — except an
    // empty file, which is the operator's own redirect: the documented command
    // sends the run's stderr to a log in here, and the shell creates it before
    // the container starts.
    let used: Vec<String> = std::fs::read_dir(&out_dir).map(|rd| {
        let mut v: Vec<String> = rd.flatten()
            .filter(|e| e.file_name() != "transcripts")
            .filter(|e| e.metadata().map(|m| m.is_dir() || m.len() > 0).unwrap_or(true))
            .map(|e| e.file_name().to_string_lossy().to_string())
            .collect();
        v.sort();
        v
    }).unwrap_or_default();
    if !used.is_empty() {
        eprintln!("{} is not empty: {}\neach directory is used once — pick another",
                  out_dir.display(), used.iter().take(4).cloned().collect::<Vec<_>>().join(", "));
        return Ok(2);
    }

    let _ = std::fs::remove_dir_all(&vault_path);
    std::fs::create_dir_all(&vault_path).map_err(|e| e.to_string())?;
    // Claude Code scopes CLAUDE.md and auto-memory to the enclosing git repo.
    // A vault inside this one loads this project's own memory and reads it
    // back as instructions. Its own repo makes the vault its own project.
    std::process::Command::new("git").args(["init", "-q"]).current_dir(&vault_path)
        .status().map_err(|e| e.to_string())?;
    let mem_dir = PathBuf::from(std::env::var("HOME").unwrap_or_default())
        .join(".claude").join("projects")
        .join(vault_path.to_string_lossy().replace('/', "-")).join("memory");

    let vault = Vault::new(&vault_path);
    index::seed(&vault, &inputs[0].date);
    index::regenerate_indexes(&vault).map_err(|e| e.to_string())?;
    let mem_cmd = install_mem(&std::env::current_exe().map_err(|e| e.to_string())?
        .parent().unwrap_or(Path::new(".")).join("mem"));

    let stem = out_path.file_stem().unwrap_or_default().to_string_lossy().to_string();
    let sibling = |suffix: &str| out_dir.join(format!("{stem}{suffix}"));
    let toollog = sibling("-tools.jsonl");
    let memlog = sibling("-mem.jsonl");
    // one line per input: which session id ran it, and what came back
    let sessionlog = sibling("-sessions.jsonl");
    let results_path = sibling("-results.jsonl");
    // a bare repo outside the vault, so a snapshot per session leaves nothing
    // in the vault itself and the vault's own .git stays empty
    let snaps = sibling("-snaps.git");
    for p in [&toollog, &memlog, &sessionlog, &results_path] {
        let _ = std::fs::remove_file(p);
    }
    let _ = std::fs::remove_dir_all(&snaps);
    std::process::Command::new("git").args(["init", "-q", "--bare"]).arg(&snaps)
        .status().map_err(|e| e.to_string())?;

    let snap = |tag: &str| -> String {
        let git = |a: &[&str]| {
            std::process::Command::new("git").args(a).current_dir(&vault_path)
                .env("GIT_DIR", &snaps).env("GIT_WORK_TREE", &vault_path)
                .env("GIT_AUTHOR_NAME", "lab").env("GIT_AUTHOR_EMAIL", "lab@localhost")
                .env("GIT_COMMITTER_NAME", "lab").env("GIT_COMMITTER_EMAIL", "lab@localhost")
                .output()
        };
        let _ = git(&["add", "-A", "--", ":!.index.db", ":!.claude", ":!.obsidian"]);
        let _ = git(&["commit", "-q", "--allow-empty", "-m", tag]);
        git(&["rev-parse", "HEAD"]).map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_default()
    };
    write_session_config(&vault_path);

    let mut report = vec!["# Recall report (session-driven)".to_string(), String::new()];
    let cli = std::process::Command::new("claude").arg("--version").output();
    let cli = cli.ok().filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).split_whitespace().next()
             .unwrap_or("unknown").to_string())
        .unwrap_or_else(|| "unknown".into());
    // the run's own logs and the binaries sit beside the vault a session
    // works in, so reading them takes an `ls ..`. That is not forbidden and not a
    // defect — it is a thing to know, because what a session recalled after reading
    // the tooling came from somewhere other than its memory. The session id is here
    // so the transcript can be read and the question asked: did it help, and does
    // what it found belong in the instructions instead.
    let lab_dir = std::env::current_exe().ok()
        .and_then(|p| p.parent().map(|d| d.display().to_string()))
        .unwrap_or_default();
    let instrument: Vec<String> = [stem.clone(), lab_dir, "lab/".into()]
        .into_iter().filter(|n| !n.is_empty()).collect();
    let mut peeks: Vec<String> = Vec::new();

    let (mut n_inputs, mut n_sessions, mut n_errors, mut n_turns, mut n_placeholder) =
        (0i64, 0i64, 0i64, 0i64, 0i64);
    let mut cost_total = 0.0f64;
    // what wanda has said so far in each scene, so a later reading can resolve
    // a reference back to something she said earlier
    let mut prior: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    // a thread's messages so far, wanda's replies included, and who is in it:
    // everyone who posts to it at any point, since a thread is read by
    // everyone in the channel whether or not they have spoken yet
    let mut threads: BTreeMap<String, Vec<(String, String)>> = BTreeMap::new();
    let mut members: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for i in &inputs {
        if !i.thread().is_empty() {
            let m = members.entry(i.thread().to_string()).or_default();
            if !m.contains(&i.speaker) {
                m.push(i.speaker.clone());
            }
        }
    }
    for m in members.values_mut() {
        m.sort();
    }
    let t0 = std::time::Instant::now();
    let total = inputs.len();

    for (n, inp) in inputs.iter().enumerate() {
        let i = n + 1;
        // the position and nothing else. The run's own directory is mounted
        // into the container a session works in, so anything written here is
        // readable by every later session: an arrival's text, who said it and
        // which scene it belongs to are the history, and the history is what
        // a session must not be able to read. What the corpus supplied is
        // joined back on the host, by this position.
        let key = format!("{i:03}");
        // what the session's own environment tags its log lines with. Opaque,
        // and only ever compared for equality, so nothing in the environment
        // describes the arrival or where it falls in the sequence.
        let trace_key = uuid::Uuid::new_v4().simple().to_string();
        // Claude Code's own memory would otherwise carry notes from session to
        // session, running a second memory system alongside the vault
        let _ = std::fs::remove_dir_all(&mem_dir);
        let before = snap(&format!("before {key}"));

        let empty = json!({"recalled": [], "answer": "", "recorded": []});
        let (out, meta, sid, session_error) = match run_session(
            &vault_path, inp, &mem_cmd, args.timeout, &trace_key, &memlog,
            members.get(inp.thread()).map(|v| v.as_slice()).unwrap_or(&[]),
            threads.get(inp.thread()).map(|v| v.as_slice()).unwrap_or(&[]),
        ) {
            Ok((out, meta, sid)) => {
                write_trace(&toollog, &trace_key, &sid, &vault_path);
                for c in read_trace(&toollog, &trace_key) {
                    let arg = c.get("arg").and_then(|v| v.as_str()).unwrap_or("");
                    if instrument.iter().any(|n| arg.contains(n.as_str())) {
                        peeks.push(format!("  session {} {:6} {}",
                            memory::text::take_chars(&sid, 8),
                            c.get("tool").and_then(|v| v.as_str()).unwrap_or(""),
                            memory::text::take_chars(arg, 110)));
                    }
                }
                n_sessions += 1;
                if let Some(t) = meta.split("turns=").nth(1).and_then(|s| s.split_whitespace().next()) {
                    n_turns += t.parse().unwrap_or(0);
                }
                if let Some(c) = meta.split("cost=").nth(1) {
                    cost_total += c.trim().parse().unwrap_or(0.0);
                }
                (out, meta, sid, String::new())
            }
            Err(e) => {
                n_errors += 1;
                eprintln!("  session error {}: {e}", inp.date);
                (empty, format!("ERROR {e}"), String::new(), e)
            }
        };
        let bad = placeholder_fields(&out);
        if !bad.is_empty() {
            // recorded, not retried: what it wrote to the vault stands, and a
            // scaffolded report is not evidence of a recall failure either way
            n_placeholder += 1;
            eprintln!("  placeholder output {}: {bad:?}", inp.date);
        }
        let answer = out.get("answer").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let recalled = out.get("recalled").cloned().unwrap_or(json!([]));

        append(&sessionlog, &json!({
            "key": key, "input_id": inp.id, "trace_key": trace_key,
            "session": sid,
            "answer": answer, "recalled": recalled, "error": session_error,
        }));

        if inp.is_checkpoint {
            let items = recalled.as_array().cloned().unwrap_or_default();
            report.push(format!("## {}", inp.scene));
            report.push(String::new());
            report.push(format!("`{} | {} | {}`", inp.date, inp.speaker, inp.text));
            report.push(String::new());
            report.push(format!("recalled {} \u{b7} {meta} \u{b7} session {}",
                                items.len(), memory::text::take_chars(&sid, 8)));
            report.push(String::new());
            for (r, item) in items.iter().enumerate() {
                report.push(format!("  {:2}. {}", r + 1,
                    item.as_str().map(|s| s.to_string()).unwrap_or_else(|| item.to_string())));
            }
            // never truncated: this is read by a person and read back later,
            // and a clipped answer cannot be told from a short one
            report.push(String::new());
            report.push(format!("  answer: {answer}"));
            report.push(String::new());
            let trace = read_trace(&toollog, &trace_key);
            let mem = read_trace(&memlog, &trace_key);
            append(&results_path, &json!({
                "input_id": inp.id, "recalled": recalled,
                "answer": answer, "recorded": out.get("recorded").cloned().unwrap_or(json!([])),
                "trace": summarise_trace(&trace, &mem), "placeholder": bad,
                "error": session_error, "session": sid,
                "prior": prior.get(&inp.scene).cloned().unwrap_or_default(),
                "snapshot": before, "anchor": anchor, "history": history_digest,
            }));
            report.push(format!("  trace: {}", summarise_trace(&trace, &mem)));
            for t in &trace {
                report.push(format!("      {:6} {}",
                    t.get("tool").and_then(|v| v.as_str()).unwrap_or(""),
                    memory::text::take_chars(t.get("arg").and_then(|v| v.as_str()).unwrap_or(""), 110)));
            }
            report.push(String::new());
            report.push(String::new());
        }

        // what wanda said earlier in this scene, by position. The arrival it
        // answered is the corpus's and is joined back with the rest.
        prior.entry(inp.scene.clone()).or_default().push(json!({
            "input_id": inp.id, "answer": answer,
        }));
        if !inp.thread().is_empty() {
            let t = threads.entry(inp.thread().to_string()).or_default();
            t.push((inp.speaker.clone(), inp.text.clone()));
            t.push(("wanda".into(), answer.clone()));
        }
        n_inputs += 1;
        eprintln!("[{i}/{total}] {} {:6} {} {} | {}",
            inp.date, memory::text::take_chars(&inp.speaker, 6),
            if inp.is_checkpoint { "CHECK" } else { "     " },
            memory::text::take_chars(&meta, 34), memory::text::take_chars(&inp.text, 44));
    }

    // if a session wrote to Claude Code's own memory, the vault is no longer
    // the only place state lives, and that has to be visible rather than silent
    let mut leaked: Vec<String> = std::fs::read_dir(&mem_dir).map(|rd| rd.flatten()
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| n.ends_with(".md")).collect()).unwrap_or_default();
    leaked.sort();
    let _ = std::fs::remove_dir_all(&mem_dir);
    if !leaked.is_empty() {
        eprintln!("  auto-memory written by the last session: {leaked:?}");
    }

    // so the store opens in Obsidian coloured, without a second command
    index::write_graph_config(&vault_path);
    let (counts, edges) = index::counts(&vault).map_err(|e| e.to_string())?;
    let elapsed = (t0.elapsed().as_secs_f64() * 10.0).round() / 10.0;
    let stats: Vec<(String, Value)> = vec![
        ("lab".into(), json!(head)), ("cli".into(), json!(cli)),
        ("inputs".into(), json!(n_inputs)), ("sessions".into(), json!(n_sessions)),
        ("errors".into(), json!(n_errors)), ("turns".into(), json!(n_turns)),
        ("placeholder".into(), json!(n_placeholder)),
        ("peeked".into(), json!(peeks.len())),
        ("elapsed_s".into(), json!(elapsed)),
        ("transcripts".into(), json!(transcript::project_dir(&vault_path)
            .file_name().unwrap_or_default().to_string_lossy())),
    ];
    let counts_shown = py_dict(&counts.iter()
        .map(|(k, v)| (k.clone(), json!(v))).collect::<Vec<_>>());
    if !peeks.is_empty() {
        report.extend(["## Looked at the instrument".to_string(), String::new()]);
        report.extend(peeks.iter().cloned());
        report.push(String::new());
    }
    report.extend(["## Store".to_string(), String::new(),
                   format!("nodes by kind: {counts_shown}"),
                   format!("edges: {edges}"),
                   format!("run: {}", py_dict(&stats)), String::new()]);
    std::fs::write(&args.out, report.join("\n") + "\n").map_err(|e| e.to_string())?;
    let stats_json: serde_json::Map<String, Value> = stats.into_iter().collect();
    eprintln!("{}", memory::text::py_json(&Value::Object(stats_json)));
    // the cost of the run, for the ledger to read afterwards off this line
    eprintln!("{}", memory::text::py_json(&json!({"cost": (cost_total * 10000.0).round() / 10000.0})));
    Ok(0)
}
