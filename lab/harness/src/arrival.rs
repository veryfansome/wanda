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

pub const PROMPT: &str = "I am wanda.\n\nToday is {date}.\n\n{arrival}\n\nI do three things, in this order.\n\n\
First, I work out what I already know that bears on this. I read the indexes,\n\
navigate to what looks relevant, and use `mem recall` on the two or three\n\
things this is actually about. I put what I found in `recalled`, most relevant\n\
first, and what I would say back in `answer`.\n\n\
Second, I record what should be remembered from it, using `mem`.\n\n\
Third, before I finish, I invoke the `enrich` skill: I link what I wrote to\n\
what was already here. Then I list what I wrote, edges included, in `recorded`.\n\n\
I run mem as: {mem}\n";

const DM: &str = "{speaker} says to me, in a direct message:\n\n    {text}";
const EMAIL: &str = "An email has arrived in the mailbox I look after:\n\n    From: {speaker}\n    {text}";
// a Slack thread: everyone in it reads what she says, and she sees what was
// said before — their messages and her own replies, which is what a thread is.
// Only the new message is this session's input.
const THREAD: &str = "In a Slack thread that {members} read. Everyone in it sees what I say \
there.\n\n{history}{speaker} {now}says:\n\n    {text}";

/// How the thread so far labels her own replies.
pub const ME: &str = "me";

/// The arrival as the session sees it. For a thread, `members` is who is in it
/// and `history` is (speaker, text) for every message so far, her own replies
/// under `ME`.
pub fn arrival_text(inp: &Input, members: &[String], history: &[(String, String)]) -> String {
    if inp.thread().is_empty() {
        let t = match inp.channel.as_str() {
            "email" => EMAIL,
            "thread" => THREAD,
            _ => DM,
        };
        return t.replace("{speaker}", &inp.speaker).replace("{text}", &inp.text);
    }
    let who: Vec<&str> = members.iter().map(String::as_str)
        .filter(|m| *m != "wanda" && *m != ME).collect();
    let who = if who.is_empty() { vec![inp.speaker.as_str()] } else { who };
    let names = format!("{} and I", who.join(", "));
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

fn fill(prompt: &str, date: &str, arrival: &str, mem: &str) -> String {
    prompt.replace("{date}", date).replace("{arrival}", arrival).replace("{mem}", mem)
}

pub fn prompt_for(date: &str, arrival: &str, mem: &str) -> String {
    fill(PROMPT, date, arrival, mem)
}

/// Prompts sessions were handed before this one, which their transcripts still
/// hold and the projection still has to read. Written out whole, not built from
/// the frames above: built from those, a change to a frame would change the
/// probe with it and the check would pass against a shape no transcript holds.
const EARLIER: [&str; 2] = [
    "Today is {date}.\n\n{arrival}\n\nDo two things, in this order.\n\n\
First, work out what you already know that bears on this. Read the indexes,\n\
navigate to what looks relevant, and use `mem recall` on the two or three\n\
things this is actually about. Put what you found in `recalled`, most relevant\n\
first, and what you would say back in `answer`.\n\n\
Second, record what should be remembered from it, using `mem`.\n\n\
Third, before you finish, invoke the `enrich` skill: link what you wrote to\n\
what was already here. Then list what you wrote, edges included, in `recorded`.\n\n\
Run mem as: {mem}\n",
    "You are wanda.\n\nToday is {date}.\n\n{arrival}\n\nDo three things, in this order.\n\n\
First, work out what you already know that bears on this. Read the indexes,\n\
navigate to what looks relevant, and use `mem recall` on the two or three\n\
things this is actually about. Put what you found in `recalled`, most relevant\n\
first, and what you would say back in `answer`.\n\n\
Second, record what should be remembered from it, using `mem`.\n\n\
Third, before you finish, invoke the `enrich` skill: link what you wrote to\n\
what was already here. Then list what you wrote, edges included, in `recorded`.\n\n\
Run mem as: {mem}\n",
];

/// The probe below, as those prompts framed it: a direct message, an email, and
/// a thread without and with messages before it.
const EARLIER_ARRIVALS: [(&str, &str); 4] = [
    ("dm", "probe says to you, in a direct message:\n\n    one line\n    and a second"),
    ("email", "An email has arrived in the mailbox you look after:\n\n    From: probe\n    \
one line\n    and a second"),
    ("thread", "In a Slack thread that probe and other read, wanda included. Everyone in it \
sees what you say there.\n\nprobe says:\n\n    one line\n    and a second"),
    ("thread", "In a Slack thread that probe and other read, wanda included. Everyone in it \
sees what you say there.\n\nThe thread so far:\n\n    probe: earlier\n    wanda: reply\n\n\
probe now says:\n\n    one line\n    and a second"),
];

/// The projection reads this prompt back out of a session's transcript, and
/// every earlier one. They are checked against the parser at startup, so that
/// editing the prompt without the parser, or the parser without the prompts
/// transcripts still hold, cannot quietly turn an exchange into someone saying
/// the whole prompt.
pub fn check_prompt_shape() -> Result<(), String> {
    let want = |chan: &str| ("2026-01-01".to_string(), chan.to_string(), "probe".to_string(),
                             "one line\nand a second".to_string());
    let read = |prompt: &str, arrival: &str|
        memory::transcript::parse_prompt(&fill(prompt, "2026-01-01", arrival, "mem"));
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
                                     (ME.to_string(), "reply".to_string())]] {
            let got = read(PROMPT, &arrival_text(&inp, &members, &history));
            if got != want(chan) {
                return Err(format!("the prompt and the parser disagree for {chan}: {got:?}"));
            }
        }
    }
    for prompt in EARLIER {
        for (chan, arrival) in EARLIER_ARRIVALS {
            let got = read(prompt, arrival);
            if got != want(chan) {
                return Err(format!("the parser no longer reads the prompt that opened {:?}, \
                                    for {chan}: {got:?}", prompt.lines().next().unwrap_or("")));
            }
        }
    }
    Ok(())
}
