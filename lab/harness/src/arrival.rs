//! What a session is handed: the arrival, and the prompt around it.
//!
//! `memory::transcript::parse_prompt` reads this back out of a session's
//! transcript to show a later session what was said, so the two shapes are
//! one thing written twice. `check_prompt_shape` holds them together.

/// One arrival, as it is handed in. `id` is its position in the run, and is
/// what a result is keyed on: a scene name is shared by several arrivals and a
/// date moves with the anchor, so neither identifies one.
#[derive(Clone, Debug, Default, serde::Deserialize, serde::Serialize)]
pub struct Input {
    pub id: i64,
    pub date: String,
    pub channel: String,
    pub speaker: String,
    pub text: String,
    pub scene: String,
    #[serde(default)]
    pub is_checkpoint: bool,
}

impl Input {
    pub fn thread(&self) -> &str {
        self.channel.strip_prefix("thread:").unwrap_or("")
    }
}

// The product's sessions are told who they are at the top of their first
// message (wanda/main.py); this says it in the same place.
pub const PROMPT: &str = "You are wanda.\n\nToday is {date}.\n\n{arrival}\n\nDo two things, in this order.\n\n\
First, work out what you already know that bears on this. Read the indexes,\n\
navigate to what looks relevant, and use `mem recall` on the two or three\n\
things this is actually about. Put what you found in `recalled`, most relevant\n\
first, and what you would say back in `answer`.\n\n\
Second, record what should be remembered from it, using `mem`.\n\n\
Third, before you finish, invoke the `enrich` skill: link what you wrote to\n\
what was already here. Then list what you wrote, edges included, in `recorded`.\n\n\
Run mem as: {mem}\n";

const DM: &str = "{speaker} says to you, in a direct message:\n\n    {text}";
const EMAIL: &str = "An email has arrived in the mailbox you look after:\n\n    From: {speaker}\n    {text}";
// a Slack thread: everyone in it reads what she says, and she sees what was
// said before — their messages and her own replies, which is what a thread is.
// Only the new message is this session's input.
const THREAD: &str = "In a Slack thread that {members} read, wanda included. Everyone in it sees \
what you say there.\n\n{history}{speaker} {now}says:\n\n    {text}";

/// The arrival as the session sees it. For a thread, `members` is who is in it
/// and `history` is (speaker, text) for every message so far, wanda's included
/// where she answered.
pub fn arrival_text(inp: &Input, members: &[String], history: &[(String, String)]) -> String {
    if inp.thread().is_empty() {
        let t = match inp.channel.as_str() {
            "email" => EMAIL,
            "thread" => THREAD,
            _ => DM,
        };
        return t.replace("{speaker}", &inp.speaker).replace("{text}", &inp.text);
    }
    let who: Vec<&String> = members.iter().filter(|m| *m != "wanda").collect();
    let fallback = vec![&inp.speaker];
    let who = if who.is_empty() { fallback } else { who };
    let names = if who.len() <= 2 {
        who.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(" and ")
    } else {
        let (last, rest) = who.split_last().unwrap();
        format!("{} and {last}", rest.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "))
    };
    let lines: String = history.iter()
        .filter(|(_, tx)| !memory::text::py_strip(tx).is_empty())
        .map(|(sp, tx)| format!("    {sp}: {tx}\n"))
        .collect();
    let (history_block, now) = if lines.is_empty() {
        (String::new(), "")
    } else {
        (format!("The thread so far:\n\n{lines}\n"), "now ")
    };
    THREAD.replace("{members}", &names)
        .replace("{history}", &history_block)
        .replace("{speaker}", &inp.speaker)
        .replace("{now}", now)
        .replace("{text}", &inp.text)
}

pub fn prompt_for(date: &str, arrival: &str, mem: &str) -> String {
    PROMPT.replace("{date}", date).replace("{arrival}", arrival).replace("{mem}", mem)
}

/// The projection reads this prompt back out of a session's transcript. The
/// two are checked against each other at startup, so that editing the prompt
/// without the parser cannot quietly turn every exchange into someone saying
/// the whole prompt. The prompt without its opening line is checked too: that
/// is the shape every transcript from before round 20 holds.
pub fn check_prompt_shape() -> Result<(), String> {
    for chan in ["dm", "email", "thread"] {
        let inp = Input {
            id: 0,
            date: "2026-01-01".into(),
            channel: if chan == "thread" { "thread:t".into() } else { chan.into() },
            speaker: "probe".into(),
            text: "one line\n    and a second".into(),
            scene: "probe scene".into(),
            is_checkpoint: false,
        };
        let members = vec!["probe".to_string(), "other".to_string()];
        for history in [vec![], vec![("probe".to_string(), "earlier".to_string()),
                                     ("wanda".to_string(), "reply".to_string())]] {
            let probe = prompt_for("2026-01-01", &arrival_text(&inp, &members, &history), "mem");
            // cut at the date line rather than at the opener's words, so that
            // rewording the opener cannot turn this into a second copy of the
            // new shape
            let old = &probe[probe.find("Today is ")
                .ok_or_else(|| "the prompt has no date line".to_string())?..];
            let want = ("2026-01-01".to_string(), chan.to_string(), "probe".to_string(),
                        "one line\nand a second".to_string());
            for (shape, p) in [("as written", probe.as_str()),
                               ("without its opening line, as before round 20", old)] {
                let got = memory::transcript::parse_prompt(p);
                if got != want {
                    return Err(format!(
                        "the prompt and the parser disagree for {chan}, {shape}: {got:?}"));
                }
            }
        }
    }
    Ok(())
}
