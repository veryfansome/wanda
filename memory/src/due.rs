//! What has come due: open trajectories whose date is today or gone by, read
//! from the node files. The daemon asks `mem due` for it on the clock, and the
//! lab's `run` hands a clock arrival the same list, so both are rendered here.

use crate::fm::label;
use crate::text::one_line;
use crate::transcript;
use crate::vault::Vault;
use std::collections::HashMap;

/// A day count for a date, so two dates can be subtracted. None for a date not
/// on the calendar.
pub fn days_from_civil(s: &str) -> Option<i64> {
    let y: i64 = s.get(0..4)?.parse().ok()?;
    let m: i64 = s.get(5..7)?.parse().ok()?;
    let d: i64 = s.get(8..10)?.parse().ok()?;
    let y2 = if m <= 2 { y - 1 } else { y };
    let era = y2.div_euclid(400);
    let yoe = y2 - era * 400;
    let doy = (153 * ((m + 9) % 12) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let z = era * 146_097 + doe - 719_468;
    (civil_from_days(z) == s.get(..10)?).then_some(z)
}

pub fn civil_from_days(z: i64) -> String {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!("{y:04}-{m:02}-{d:02}")
}

/// What a morning look is handed above the list.
pub const LOOK_HEAD: &str = "Come due for {name} after {after}:";
/// The labels her own node goes by, which only her own undertakings link to;
/// wanda/clock.py holds the same two for the wakes.
pub const SELF: [&str; 2] = ["me", "wanda"];

/// What a look's list says under an undertaking of hers timed later that day
/// for one person who asked: whom the clock gives it to, who need not be the
/// person reading, and when. The clock finds it only while it is open with
/// that time, so a look that closes or re-dates it takes the reminder away.
pub const STILL_TO_COME: &str =
    "still to come: the clock gives it to {asker} at {time}, but only while it is open and timed so";

/// Whether this is a time of day a clock shows, `HH:MM`.
pub fn is_clock_time(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 5 && b[2] == b':' && b.iter().enumerate().all(|(i, c)| i == 2 || c.is_ascii_digit())
        && &s[..2] <= "23" && &s[3..] <= "59"
}

/// One open trajectory that has come due.
pub struct Item {
    pub id: String,
    /// `expect_by` as written: a date, or a date and a time of day
    pub by: String,
    pub days_ago: i64,
    pub summary: String,
    pub involves: Vec<String>,
    pub constrained_by: Vec<String>,
    /// whose message the thread was made in; empty when the transcript is gone
    /// or nobody wrote, as with an email or the clock
    pub asked_by: String,
}

impl Item {
    pub fn involves_name(&self, name: &str) -> bool {
        let name = name.to_lowercase();
        self.involves.iter().any(|l| l.to_lowercase() == name)
    }

    /// Whether the clock wakes her for this at its time on its day: an
    /// undertaking of hers, at a time of day a clock shows, written in the one
    /// shape `mem` writes, which wanda/clock.py reads too, that one person
    /// asked for.
    pub fn woken_at_its_time(&self) -> bool {
        self.by.len() == 16 && self.by.as_bytes()[10] == b'T'
            && self.by.get(11..).is_some_and(is_clock_time)
            && self.involves.iter().any(|l| SELF.contains(&l.to_lowercase().as_str()))
            && !self.asked_by.is_empty()
    }

    /// What it is and when, then who and what it involves, any rule it is held
    /// to, and who asked.
    pub fn lines(&self) -> Vec<String> {
        let when = match self.days_ago {
            0 => "today".to_string(),
            1 => "1 day ago".to_string(),
            d => format!("{d} days ago"),
        };
        let mut out = vec![format!("`{}`  {}, {when}  {}", self.id, self.by, self.summary)];
        for (rel, to) in [("involves", &self.involves), ("constrained_by", &self.constrained_by)] {
            if !to.is_empty() {
                out.push(format!("    {rel}: {}", to.join("; ")));
            }
        }
        if !self.asked_by.is_empty() {
            out.push(format!("    asked by: {}", self.asked_by));
        }
        out
    }
}

fn asked_by(v: &Vault, made: &str) -> String {
    if made.is_empty() {
        return String::new();
    }
    let Some(p) = transcript::find(&v.root, made) else { return String::new() };
    let ex = transcript::load(&p);
    // an email's sender asked her nothing, and on the clock nobody wrote. A
    // turn that took several people's messages reads back as "{speaker}
    // (after {others})" (transcript::parse_prompt): no one person asked
    if ex.channel == "email" || ex.channel == "clock" || ex.speaker.contains(" (after ") {
        return String::new();
    }
    ex.speaker
}

/// Every open trajectory dated on or before `today`, and after `after` when
/// one is given, newest first. None when either date is not one.
pub fn items(v: &Vault, today: &str, after: Option<&str>) -> Option<Vec<Item>> {
    let now = days_from_civil(today)?;
    let from = match after {
        Some(a) => days_from_civil(a)?,
        None => i64::MIN,
    };
    let nodes = v.nodes();
    let labels: HashMap<&str, String> = nodes.iter()
        .map(|n| (n.id.as_str(), label(&n.meta))).collect();
    let mut found: Vec<(&str, i64, &crate::vault::Node)> = nodes.iter()
        .filter(|n| n.kind() == "trajectory" && n.meta.get("status") == "open")
        .filter_map(|n| {
            let by = n.meta.get("expect_by");
            let Some(day) = days_from_civil(by) else {
                // stored before `mem` refused a day no calendar has, or by
                // hand: it would never come due, and nothing else says so
                if !by.is_empty() {
                    eprintln!("mem due: {} has a date no calendar has ({by}); it never comes due",
                              n.id);
                }
                return None;
            };
            (from < day && day <= now).then_some((by, day, n))
        })
        .collect();
    // newest first, as every index is
    found.sort_by(|a, b| b.0.cmp(a.0));
    let to = |n: &crate::vault::Node, rel: &str| -> Vec<String> {
        n.meta.edges.iter().filter(|e| e.rel == rel)
            .map(|e| labels.get(e.to.as_str()).cloned().unwrap_or_else(|| e.to.clone()))
            .collect()
    };
    Some(found.into_iter().map(|(by, day, n)| Item {
        id: n.id.clone(),
        by: by.to_string(),
        days_ago: now - day,
        summary: one_line(n.meta.get("summary")),
        involves: to(n, "involves"),
        constrained_by: to(n, "constrained_by"),
        asked_by: asked_by(v, n.meta.get("made")),
    }).collect())
}

/// What a morning look for `name` at `at` (`HH:MM`, or empty) is handed: each
/// open trajectory involving them whose date fell after `after`, their last
/// look, and on or before today, under a line saying so. An undertaking of
/// hers timed later today for someone who asked is marked as still to come:
/// the clock wakes her for it at that time, and a look that closed or
/// re-dated it would take that away. Nothing at all when nothing came due, so
/// the look is framed as it would be without a list. None when either date
/// is not one.
pub fn for_look(v: &Vault, today: &str, after: &str, name: &str, at: &str) -> Option<Vec<String>> {
    let items = items(v, today, Some(after))?;
    let ahead = |i: &Item| !at.is_empty() && i.days_ago == 0 && i.woken_at_its_time()
        && i.by.get(11..).is_some_and(|t| t >= at);
    let mut out = Vec::new();
    for item in items.iter().filter(|i| i.involves_name(name)) {
        if out.is_empty() {
            out.push(LOOK_HEAD.replace("{name}", name).replace("{after}", after));
        }
        out.extend(item.lines());
        if ahead(item) {
            out.push(format!("    {}", STILL_TO_COME.replace("{asker}", &item.asked_by)
                .replace("{time}", &item.by[11..])));
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fm::Edge;

    /// A vault in a fresh directory with its transcripts beside it, removed
    /// when the test ends.
    struct Store(Vault, std::path::PathBuf);

    impl Drop for Store {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.1);
        }
    }

    fn edge(rel: &str, to: &str) -> Edge {
        Edge { rel: rel.into(), to: to.into() }
    }

    /// One exchange, as Claude Code writes the opening of a transcript.
    fn transcript(dir: &std::path::Path, sid: &str, arrival: &str) {
        let prompt = format!("I am wanda.\n\nToday is 2031-01-10.\n\n{arrival}\n\n\
                              Do three things, in this order.\n");
        let line = serde_json::json!({"type": "user", "timestamp": "2031-01-10T09:00:00Z",
                                      "message": {"content": prompt}});
        std::fs::write(dir.join(format!("{sid}.jsonl")), format!("{line}\n")).unwrap();
    }

    fn store(tag: &str) -> Store {
        let base = std::env::temp_dir().join(format!("mem-due-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let root = base.join("vault");
        let tx = base.join("transcripts");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&tx).unwrap();
        let mut v = Vault::new(root);
        transcript(&tx, "s-fan", "fan says to me, in a direct message:\n\n    remind me at 5");
        transcript(&tx, "s-mail", "An email has arrived in the mailbox I look after:\n\n    \
                                   From: shop\n    your order");
        // the product's frame for a turn that took fan's message and then mei's
        transcript(&tx, "s-both", "In a group direct message that fan, mei and I read. Everyone \
                                   in it sees what I say there.\n\nThe conversation so far:\n\n    \
                                   08:58 fan: remind me at 9\n\nmei now says, after fan:\n\n    \
                                   I'm out then anyway");
        v.upsert("person:1a2b3c", "person", "fan", "", "", &[], &[], "2031-01-10");
        v.upsert("person:2b3c4d", "person", "mei", "", "", &[], &[], "2031-01-10");
        v.upsert("person:3c4d5e", "person", "me", "", "", &[], &[], "2031-01-10");
        v.upsert("preference:4d5e6f", "preference", "keep it from mei", "keep it from mei", "",
                 &[], &[], "2031-01-10");
        let mut thread = |id: &str, made: &str, summary: &str, by: &str, status: &str,
                          edges: &[Edge]| {
            v.session = made.into();
            v.upsert(id, "trajectory", summary, summary, "",
                     &[("expect".into(), "it happens".into()), ("expect_by".into(), by.into()),
                       ("status".into(), status.into())],
                     edges, "2031-01-10");
        };
        let fan = edge("involves", "person:1a2b3c");
        let mei = edge("involves", "person:2b3c4d");
        let me = edge("involves", "person:3c4d5e");
        thread("trajectory:aaaaaa", "s-fan", "remind fan at 5", "2031-01-12T17:00", "open",
               &[me.clone(), fan.clone(), mei.clone(), edge("constrained_by", "preference:4d5e6f")]);
        thread("trajectory:bbbbbb", "s-mail", "the parcel", "2031-01-12", "open", &[mei.clone()]);
        thread("trajectory:cccccc", "", "gone by", "2031-01-09", "open", &[fan.clone()]);
        thread("trajectory:dddddd", "", "not yet", "2031-01-13", "open", &[fan.clone()]);
        thread("trajectory:eeeeee", "", "done with", "2031-01-11", "closed", &[fan]);
        thread("trajectory:ffffff", "", "no date", "", "open", &[mei]);
        thread("trajectory:gggggg", "s-both", "remind fan at 9", "2031-01-12T09:00", "open",
               &[me, edge("involves", "person:1a2b3c")]);
        // written before `mem` refused a day no calendar has
        thread("trajectory:hhhhhh", "", "the 30th of February", "2031-02-30", "open",
               &[edge("involves", "person:1a2b3c")]);
        v.session = String::new();
        std::env::set_var("MEM_TRANSCRIPTS", &tx);
        Store(v, base)
    }

    #[test]
    fn what_has_come_due_is_open_and_dated_and_says_who_asked() {
        let mut s = store("items");
        let v = &s.0;
        let _ = std::fs::remove_file(v.root.join(".index.db"));
        let all = items(v, "2031-01-12", None).unwrap();
        let lines: Vec<String> = all.iter().flat_map(|i| i.lines()).collect();
        // the turn of two speakers names no one who asked; the 30th of
        // February is never due
        assert_eq!(lines, vec![
            "`trajectory:aaaaaa`  2031-01-12T17:00, today  remind fan at 5",
            "    involves: me; fan; mei",
            "    constrained_by: keep it from mei",
            "    asked by: fan",
            "`trajectory:gggggg`  2031-01-12T09:00, today  remind fan at 9",
            "    involves: me; fan",
            "`trajectory:bbbbbb`  2031-01-12, today  the parcel",
            "    involves: mei",
            "`trajectory:cccccc`  2031-01-09, 3 days ago  gone by",
            "    involves: fan",
        ]);
        assert!(!v.root.join(".index.db").exists(), "read from the files, not the index");
        assert!(items(v, "2031-01-08", None).unwrap().is_empty());
        assert!(items(v, "", None).is_none());
        assert!(items(v, "2031-01-12", Some("no date")).is_none());

        // a look is handed what came due for that person since the last one
        assert_eq!(for_look(v, "2031-01-12", "2031-01-11", "mei", "17:30").unwrap(), vec![
            "Come due for mei after 2031-01-11:",
            "`trajectory:aaaaaa`  2031-01-12T17:00, today  remind fan at 5",
            "    involves: me; fan; mei",
            "    constrained_by: keep it from mei",
            "    asked by: fan",
            "`trajectory:bbbbbb`  2031-01-12, today  the parcel",
            "    involves: mei",
        ]);
        assert_eq!(for_look(v, "2031-01-12", "2031-01-11", "mei", "").unwrap().len(), 7);
        // her own undertaking timed later today for someone who asked is
        // marked as still to come, with whom the clock gives it to at its
        // time: fan, who asked, though this is mei's list
        assert_eq!(for_look(v, "2031-01-12", "2031-01-11", "mei", "08:00").unwrap(), vec![
            "Come due for mei after 2031-01-11:",
            "`trajectory:aaaaaa`  2031-01-12T17:00, today  remind fan at 5",
            "    involves: me; fan; mei",
            "    constrained_by: keep it from mei",
            "    asked by: fan",
            "    still to come: the clock gives it to fan at 17:00, but only while it is open and timed so",
            "`trajectory:bbbbbb`  2031-01-12, today  the parcel",
            "    involves: mei",
        ]);
        // at its own minute too, which is when the clock wakes for it
        assert_eq!(for_look(v, "2031-01-12", "2031-01-11", "mei", "17:00").unwrap().len(), 8);
        // one no one person asked for is not marked, since the clock does not
        // wake for it; nor is a time gone by
        let fan = for_look(v, "2031-01-12", "2031-01-08", "fan", "08:00").unwrap();
        assert_eq!(fan, vec![
            "Come due for fan after 2031-01-08:",
            "`trajectory:aaaaaa`  2031-01-12T17:00, today  remind fan at 5",
            "    involves: me; fan; mei",
            "    constrained_by: keep it from mei",
            "    asked by: fan",
            "    still to come: the clock gives it to fan at 17:00, but only while it is open and timed so",
            "`trajectory:gggggg`  2031-01-12T09:00, today  remind fan at 9",
            "    involves: me; fan",
            "`trajectory:cccccc`  2031-01-09, 3 days ago  gone by",
            "    involves: fan",
        ]);
        assert!(for_look(v, "2031-01-12", "2031-01-12", "fan", "08:00").unwrap().is_empty());
        assert!(is_clock_time("23:59") && is_clock_time("00:00"));
        assert!(!is_clock_time("24:00") && !is_clock_time("12:60") && !is_clock_time("8:00"));

        // a time written by hand in a shape `mem` refuses is no time the clock
        // wakes for, so nothing marks it as still to come
        s.0.session = "s-fan".into();
        for (id, by) in [("trajectory:iiiiii", "2031-01-12T9:00"), ("trajectory:jjjjjj", "2031-01-12 17:00")] {
            s.0.upsert(id, "trajectory", "by hand", "by hand", "",
                       &[("expect".into(), "it happens".into()), ("expect_by".into(), by.into()),
                         ("status".into(), "open".into())],
                       &[edge("involves", "person:3c4d5e"), edge("involves", "person:1a2b3c")], "2031-01-10");
        }
        let fan = for_look(&s.0, "2031-01-12", "2031-01-11", "fan", "08:00").unwrap();
        assert!(fan.contains(&"`trajectory:iiiiii`  2031-01-12T9:00, today  by hand".to_string())
                && fan.contains(&"`trajectory:jjjjjj`  2031-01-12 17:00, today  by hand".to_string()), "{fan:?}");
        assert_eq!(fan.iter().filter(|l| l.contains("still to come")).count(), 1, "{fan:?}");
        std::env::remove_var("MEM_TRANSCRIPTS");
    }
}
