//! Put one arrival to a store many times, from a snapshot of how it stood.
//!
//! A single pass confounds two things. The store an arrival meets is the
//! product of everything before it, and the session that meets it is one draw.
//! Two passes over one configuration differ, and nothing in either says which
//! half that came from.
//!
//! The run commits the vault to a bare repo before every session, so the store
//! as it stood at any point can be materialised again. Putting the same
//! arrival N times to one snapshot varies only the session; putting it once to
//! each of several snapshots varies only the store.

use harness::arrival::{Input, ME};
use harness::session::{
    install_mem, materialise, placeholder_fields, read_trace, resolve, run_session,
    summarise_trace, write_trace,
};
use memory::text::{py_json, take_chars};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};

struct Args {
    results: Vec<PathBuf>,
    inputs: PathBuf,
    scene: String,
    reps: usize,
    out: PathBuf,
    timeout: u64,
    work: PathBuf,
    regenerate: bool,
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        results: Vec::new(),
        inputs: PathBuf::new(),
        scene: String::new(),
        reps: 3,
        out: PathBuf::new(),
        timeout: 420,
        work: PathBuf::from("/tmp/lab-work"),
        regenerate: false,
    };
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < argv.len() {
        match argv[i].as_str() {
            // one or more, each naming its own snapshot repo
            "--results" => {
                while i + 1 < argv.len() && !argv[i + 1].starts_with("--") {
                    a.results.push(PathBuf::from(&argv[i + 1]));
                    i += 1;
                }
            }
            // a run records the position and what came back, not the history,
            // so the arrival itself is joined back from here
            "--inputs" => { a.inputs = PathBuf::from(argv.get(i + 1).cloned().unwrap_or_default()); i += 1 }
            "--scene" => { a.scene = argv.get(i + 1).cloned().unwrap_or_default(); i += 1 }
            "--reps" => { a.reps = argv.get(i + 1).and_then(|v| v.parse().ok()).unwrap_or(3); i += 1 }
            "--out" => { a.out = PathBuf::from(argv.get(i + 1).cloned().unwrap_or_default()); i += 1 }
            "--timeout" => { a.timeout = argv.get(i + 1).and_then(|v| v.parse().ok()).unwrap_or(420); i += 1 }
            "--work" => { a.work = PathBuf::from(argv.get(i + 1).cloned().unwrap_or_default()); i += 1 }
            "--regenerate" => a.regenerate = true,
            other => return Err(format!("unknown argument {other}")),
        }
        i += 1;
    }
    if a.results.is_empty() {
        return Err("--results is required".into());
    }
    if a.out.as_os_str().is_empty() {
        return Err("--out is required".into());
    }
    Ok(a)
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}

/// A list field read leniently: absent, null or anything not a list all come
/// back empty.
fn list(v: &Value, k: &str) -> Vec<Value> {
    v.get(k).and_then(|x| x.as_array()).cloned().unwrap_or_default()
}

/// A record and the arrival it came from, as one.
///
/// A run writes its records into its own directory and that directory is
/// mounted into the container its sessions work in, so the history is not
/// written there; `--inputs` is where it comes back from. A record written
/// before the split carries its own, and keeps it.
fn filled(rec: &Value, by_id: &BTreeMap<i64, Value>) -> Value {
    let mut out = by_id.get(&rec.get("input_id").and_then(|v| v.as_i64()).unwrap_or(-1))
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();
    if let Some(m) = rec.as_object() {
        for (k, v) in m {
            if !matches!(v, Value::Null) && v.as_str() != Some("") {
                out.insert(k.clone(), v.clone());
            }
        }
    }
    Value::Object(out)
}

/// The arrival this record came from.
fn arrival(rec: &Value) -> Input {
    let mut chan = s(rec, "channel");
    if chan.is_empty() {
        // results from before the channel was recorded. The priors are the
        // same scene and usually name one, but the arrival's own channel is
        // not always theirs — and a thread put as a direct message is a
        // different exchange — so any reconstruction says so.
        chan = list(rec, "prior").iter().map(|pr| s(pr, "channel"))
            .find(|c| !c.is_empty()).unwrap_or_default();
        if chan.is_empty() {
            chan = "dm".into();
        }
        eprintln!("no channel recorded for {}; reading it as {chan}",
                  take_chars(&s(rec, "scene"), 40));
    }
    Input {
        id: rec.get("input_id").and_then(|v| v.as_i64()).unwrap_or(0),
        date: s(rec, "date"),
        channel: chan,
        speaker: s(rec, "speaker"),
        text: s(rec, "text"),
        scene: s(rec, "scene"),
        is_checkpoint: true,
    }
}

fn main() {
    std::process::exit(match run() {
        Ok(rc) => rc,
        Err(e) => {
            eprintln!("{e}");
            2
        }
    });
}

fn run() -> Result<i32, String> {
    let args = parse_args()?;
    let out_path = resolve(&args.out)?;
    // its own directory, beside the one it draws from and never inside it:
    // that one collects more after its pass ends, and is given whole to
    // whatever runs against it next
    let out_dir = out_path.parent().unwrap_or(Path::new(".")).to_path_buf();
    std::fs::create_dir_all(&out_dir).map_err(|e| e.to_string())?;
    let stem = out_path.file_stem().unwrap_or_default().to_string_lossy().to_string();
    let toollog = out_dir.join(format!("{stem}-tools.jsonl"));
    let memlog = out_dir.join(format!("{stem}-mem.jsonl"));
    for p in [&out_path, &toollog, &memlog] {
        let _ = std::fs::remove_file(p);
    }
    let work = resolve(&args.work)?;
    let mem_cmd = install_mem(&std::env::current_exe().map_err(|e| e.to_string())?
        .parent().unwrap_or(Path::new(".")).join("mem"));
    let t0 = std::time::Instant::now();
    let mut n = 0;

    for rp in &args.results {
        let rp = resolve(rp)?;
        let name = rp.file_name().unwrap_or_default().to_string_lossy().to_string();
        let snaps = rp.with_file_name(name.replace("-results.jsonl", "-snaps.git"));
        if !snaps.exists() {
            eprintln!("no snapshots beside {name}; run it on a run made after \
                       snapshotting landed");
            return Ok(2);
        }
        let rp_stem = rp.file_stem().unwrap_or_default().to_string_lossy().to_string();
        let snaps_name = snaps.file_name().unwrap_or_default().to_string_lossy().to_string();
        let text = std::fs::read_to_string(&rp).map_err(|e| e.to_string())?;
        let mut payload = Value::Null;
        let by_id: BTreeMap<i64, Value> = if args.inputs.as_os_str().is_empty() {
            BTreeMap::new()
        } else {
            let raw = std::fs::read_to_string(&args.inputs).map_err(|e| e.to_string())?;
            payload = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
            list(&payload, "inputs").into_iter()
                .filter_map(|i| i.get("id").and_then(|v| v.as_i64()).map(|n| (n, i)))
                .collect()
        };
        for line in memory::text::split_lines(&text) {
            let Ok(rec) = serde_json::from_str::<Value>(line) else { continue };
            // the arrivals have to be the ones this run was made from: a date
            // moves with the anchor, so a payload built on another day rebuilds
            // every arrival on the wrong one and nothing says so
            for field in ["anchor", "history"] {
                let want = s(&rec, field);
                if !payload.is_null() && !want.is_empty() && s(&payload, field) != want {
                    eprintln!("--inputs has {field} {} and these results have {want}; \
                               rebuild the arrivals with --anchor {}",
                              s(&payload, field), s(&rec, "anchor"));
                    return Ok(2);
                }
            }
            let whole = filled(&rec, &by_id);
            let scene = s(&whole, "scene");
            if !args.scene.is_empty() && !scene.contains(&args.scene) {
                continue;
            }
            let sha = s(&rec, "snapshot");
            if sha.is_empty() {
                continue;
            }
            if s(&whole, "text").is_empty() {
                eprintln!("no arrival for position {}; pass --inputs for a run that \
                           records the position and not the history",
                          rec.get("input_id").map(|v| v.to_string()).unwrap_or_default());
                return Ok(2);
            }
            let inp = arrival(&whole);
            let prior: Vec<Value> = list(&rec, "prior").iter()
                .map(|pr| filled(pr, &by_id)).collect();
            // a thread arrival needs the thread so far, which the original run
            // kept in `prior`; older results have no channel on their prior
            // entries and no thread arrivals either
            let mut hist: Vec<(String, String)> = Vec::new();
            for pr in prior.iter().filter(|pr| s(pr, "channel") == inp.channel) {
                hist.push((s(pr, "speaker"), s(pr, "text")));
                hist.push((ME.into(), s(pr, "answer")));
            }
            let mut who: Vec<String> = prior.iter()
                .filter(|pr| s(pr, "channel") == inp.channel)
                .map(|pr| s(pr, "speaker"))
                .chain(std::iter::once(inp.speaker.clone()))
                .collect();
            who.sort();
            who.dedup();
            let in_thread = !inp.thread().is_empty();
            let (members, history): (&[String], &[(String, String)]) =
                if in_thread { (&who, &hist) } else { (&[], &[]) };

            for k in 1..=args.reps {
                // keyed on the arrival's position, like everything else: a
                // scene name is shared, so neither the directory nor the log
                // key would otherwise be stable and unique
                let ident = match rec.get("input_id") {
                    Some(Value::Number(v)) if v.as_i64() != Some(0) => v.to_string(),
                    _ => take_chars(&scene, 20).to_string(),
                };
                let dest = work.join(format!("{rp_stem}-{ident}-{k}"));
                materialise(&snaps, &sha, &dest, args.regenerate, true)
                    .map_err(|e| e.to_string())?;
                let key = format!("{rp_stem}|{ident}|{k}");
                let (body, meta) = match run_session(&dest, &inp, &mem_cmd, args.timeout,
                                                     &key, &memlog, members, history) {
                    Ok((body, meta, sid)) => {
                        write_trace(&toollog, &key, &sid, &dest);
                        (body, meta)
                    }
                    Err(e) => (json!({"recalled": [], "answer": "", "recorded": []}),
                               format!("ERROR {e}")),
                };
                let recalled = body.get("recalled").cloned().unwrap_or(json!([]));
                let answer = match body.get("answer") {
                    Some(Value::String(a)) if !a.is_empty() => Value::String(a.clone()),
                    _ => json!(""),
                };
                let mut out = Map::new();
                // the position and what came back, as a run writes them: the
                // arrival is the history and is not written into a directory a
                // session can read. The position is what joins this record to
                // its expectations, and to the arrival itself, the way a run's
                // own records join.
                out.insert("input_id".into(),
                           rec.get("input_id").cloned().unwrap_or(Value::Null));
                out.insert("anchor".into(), rec.get("anchor").cloned().unwrap_or(Value::Null));
                out.insert("history".into(), rec.get("history").cloned().unwrap_or(Value::Null));
                // a record from before the split carries its own expectations
                // and has no position to join on; pass them through so it
                // still scores
                for f in ["should", "should_not", "budget"] {
                    if let Some(v) = rec.get(f) {
                        out.insert(f.into(), v.clone());
                    }
                }
                out.insert("recalled".into(), recalled.clone());
                out.insert("answer".into(), answer);
                out.insert("recorded".into(), json!(list(&body, "recorded")));
                out.insert("placeholder".into(), json!(placeholder_fields(&body)));
                out.insert("store".into(), json!(rp_stem));
                out.insert("rep".into(), json!(k));
                out.insert("snapshot".into(), json!(sha));
                // the repo this snapshot came from, by name: a record is
                // written in the container and read on the host
                out.insert("snaps".into(), json!(snaps_name));
                // what the original run's sessions said earlier in the scene,
                // by position; this session faces the same store
                out.insert("prior".into(), json!(list(&rec, "prior").iter()
                    .map(|pr| json!({"input_id": pr.get("input_id").cloned()
                                                   .unwrap_or(Value::Null),
                                     "answer": s(pr, "answer")}))
                    .collect::<Vec<_>>()));
                out.insert("trace".into(), json!(summarise_trace(
                    &read_trace(&toollog, &key), &read_trace(&memlog, &key))));
                if let Ok(mut fh) = std::fs::OpenOptions::new().create(true).append(true)
                    .open(&out_path)
                {
                    let _ = writeln!(fh, "{}", py_json(&Value::Object(out)));
                }
                let _ = std::fs::remove_dir_all(&dest);
                n += 1;
                eprintln!("[{n}] {rp_stem} {:34} rep {k} {} recalled={}",
                          take_chars(&scene, 34), take_chars(&meta, 30),
                          recalled.as_array().map(|a| a.len()).unwrap_or(0));
            }
        }
    }

    let elapsed = (t0.elapsed().as_secs_f64() * 10.0).round() / 10.0;
    println!("{}", py_json(&json!({"runs": n, "elapsed_s": elapsed})));
    Ok(0)
}
