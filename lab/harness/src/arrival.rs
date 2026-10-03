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

// The steps are imperatives with no pronoun: in her first person a step reads
// as a habit, and an order beside "I" reads as someone else's.
pub const PROMPT: &str = "I am wanda.\n\nToday is {date}.\n\n{arrival}\n\nDo three things, in this order.\n\n\
First, work out what is already known that bears on this. Read the indexes,\n\
navigate to what looks relevant, and use `mem recall` on the two or three\n\
things this is actually about. Put what was found in `recalled`, most relevant\n\
first, and what would be said back in `answer`.\n\n\
Second, record what should be remembered from it, using `mem`.\n\n\
Third, before finishing, invoke the `enrich` skill: link what this session wrote to\n\
what was already here. Then list what this session wrote, edges included, in `recorded`.\n\n\
Run mem as: {mem}\n";

const DM: &str = "{speaker} says to me, in a direct message:\n\n    {text}";
const EMAIL: &str = "An email has arrived in the mailbox I look after:\n\n    From: {speaker}\n    {text}";
// a Slack thread: everyone in it reads what she says, and she sees what was
// said before — their messages and her own replies, which is what a thread is.
// Only the new message is this session's input.
const THREAD: &str = "In a Slack thread that {members} read. Everyone in it sees what I say \
there.\n\n{history}{speaker} {now}says:\n\n    {text}";

/// How the thread so far labels her own replies.
pub const ME: &str = "me";

// a session no message started. Nobody is speaking, so the frame names who
// hears what she says rather than who said something; the indented lines are
// what woke her, and a `clock` arrival's text is the time of day it ran at.
pub const CLOCK: &str = "No message started this session. What I say now reaches {speaker} alone, in a \
direct message.\n\n    {text}";
pub const MORNING: &str = "It is {weekday}, {time}, and this is my look at the day ahead for {speaker}.";

/// A morning look, and below it the list `memory::due::for_look` gives for
/// that person, when it gives one.
pub fn clock_text(inp: &Input, listed: &[String]) -> String {
    let mut woke = MORNING.replace("{weekday}", weekday(&inp.date))
        .replace("{time}", &inp.text).replace("{speaker}", &inp.speaker);
    if !listed.is_empty() {
        woke += &format!("\n\n    {}", listed.join("\n    "));
    }
    CLOCK.replace("{speaker}", &inp.speaker).replace("{text}", &woke)
}

/// The date of this person's last look before `at`: their latest earlier
/// `clock` arrival, or the day before when they have had none, so a first look
/// is handed only what is due that day.
pub fn last_look(inputs: &[Input], at: &Input) -> String {
    inputs.iter()
        .filter(|i| i.id < at.id && i.channel == "clock" && i.speaker == at.speaker)
        .map(|i| i.date.clone())
        .max()
        .unwrap_or_else(|| memory::due::days_from_civil(&at.date)
            .map(|d| memory::due::civil_from_days(d - 1)).unwrap_or_default())
}

/// The day of the week a date falls on.
pub fn weekday(date: &str) -> &'static str {
    const DAYS: [&str; 7] = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                             "Saturday"];
    // the month's offset in Sakamoto's method, January first
    const OFFSET: [i64; 12] = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];
    let num = |r: std::ops::Range<usize>| date.get(r).and_then(|x| x.parse::<i64>().ok());
    let (Some(y), Some(m), Some(d)) = (num(0..4), num(5..7), num(8..10)) else { return "" };
    if !(1..=12).contains(&m) {
        return "";
    }
    let y = if m < 3 { y - 1 } else { y };
    DAYS[(y + y / 4 - y / 100 + y / 400 + OFFSET[(m - 1) as usize] + d).rem_euclid(7) as usize]
}

/// The arrival as the session sees it. For a thread, `members` is who is in it
/// and `history` is (speaker, text) for every message so far, her own replies
/// under `ME`.
pub fn arrival_text(inp: &Input, members: &[String], history: &[(String, String)]) -> String {
    if inp.thread().is_empty() {
        let t = match inp.channel.as_str() {
            "email" => EMAIL,
            "thread" => THREAD,
            "clock" => return clock_text(inp, &[]),
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

/// What sessions were handed before this prompt and these frames: each earlier
/// prompt with the frames it was handed with, which their transcripts still hold
/// and the projection still has to read. Written out whole, not built from the
/// prompt and frames above: built from those, a change to either would change
/// the probe with it and the check would pass against a shape no transcript
/// holds.
const EARLIER: [(&str, &[(&str, &str)]); 3] = [
    ("Today is {date}.\n\n{arrival}\n\nDo two things, in this order.\n\n\
First, work out what you already know that bears on this. Read the indexes,\n\
navigate to what looks relevant, and use `mem recall` on the two or three\n\
things this is actually about. Put what you found in `recalled`, most relevant\n\
first, and what you would say back in `answer`.\n\n\
Second, record what should be remembered from it, using `mem`.\n\n\
Third, before you finish, invoke the `enrich` skill: link what you wrote to\n\
what was already here. Then list what you wrote, edges included, in `recorded`.\n\n\
Run mem as: {mem}\n", &ARRIVALS_TO_YOU),
    ("You are wanda.\n\nToday is {date}.\n\n{arrival}\n\nDo three things, in this order.\n\n\
First, work out what you already know that bears on this. Read the indexes,\n\
navigate to what looks relevant, and use `mem recall` on the two or three\n\
things this is actually about. Put what you found in `recalled`, most relevant\n\
first, and what you would say back in `answer`.\n\n\
Second, record what should be remembered from it, using `mem`.\n\n\
Third, before you finish, invoke the `enrich` skill: link what you wrote to\n\
what was already here. Then list what you wrote, edges included, in `recorded`.\n\n\
Run mem as: {mem}\n", &ARRIVALS_TO_YOU),
    ("I am wanda.\n\nToday is {date}.\n\n{arrival}\n\nI do three things, in this order.\n\n\
First, I work out what I already know that bears on this. I read the indexes,\n\
navigate to what looks relevant, and use `mem recall` on the two or three\n\
things this is actually about. I put what I found in `recalled`, most relevant\n\
first, and what I would say back in `answer`.\n\n\
Second, I record what should be remembered from it, using `mem`.\n\n\
Third, before I finish, I invoke the `enrich` skill: I link what I wrote to\n\
what was already here. Then I list what I wrote, edges included, in `recorded`.\n\n\
I run mem as: {mem}\n", &ARRIVALS_TO_ME),
];

/// The probe below, as the frames that addressed her as "you" put it: a direct
/// message, an email, and a thread without and with messages before it.
const ARRIVALS_TO_YOU: [(&str, &str); 4] = [
    ("dm", "probe says to you, in a direct message:\n\n    one line\n    and a second"),
    ("email", "An email has arrived in the mailbox you look after:\n\n    From: probe\n    \
one line\n    and a second"),
    ("thread", "In a Slack thread that probe and other read, wanda included. Everyone in it \
sees what you say there.\n\nprobe says:\n\n    one line\n    and a second"),
    ("thread", "In a Slack thread that probe and other read, wanda included. Everyone in it \
sees what you say there.\n\nThe thread so far:\n\n    probe: earlier\n    wanda: reply\n\n\
probe now says:\n\n    one line\n    and a second"),
];

/// The same probe, as the frames in her first person put it.
const ARRIVALS_TO_ME: [(&str, &str); 4] = [
    ("dm", "probe says to me, in a direct message:\n\n    one line\n    and a second"),
    ("email", "An email has arrived in the mailbox I look after:\n\n    From: probe\n    \
one line\n    and a second"),
    ("thread", "In a Slack thread that probe, other and I read. Everyone in it sees what I say \
there.\n\nprobe says:\n\n    one line\n    and a second"),
    ("thread", "In a Slack thread that probe, other and I read. Everyone in it sees what I say \
there.\n\nThe thread so far:\n\n    probe: earlier\n    me: reply\n\n\
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
    let morning = Input { channel: "clock".into(), text: "08:00".into(), ..Input::default() };
    let morning = Input { speaker: "probe".into(), date: "2026-01-01".into(), ..morning };
    let look = "It is Thursday, 08:00, and this is my look at the day ahead for probe.";
    let listed = ["Come due for probe after 2025-12-31:".to_string(),
                  "`trajectory:aaaaaa`  2026-01-01, today  one line".to_string(),
                  "    involves: probe".to_string()];
    for (arrival, text) in [
        (arrival_text(&morning, &[], &[]), look.to_string()),
        (clock_text(&morning, &listed), format!("{look}\n\n{}", listed.join("\n"))),
    ] {
        let got = read(PROMPT, &arrival);
        let woke = ("2026-01-01".to_string(), "clock".to_string(), "probe".to_string(), text);
        if got != woke {
            return Err(format!("the prompt and the parser disagree for clock: {got:?}"));
        }
    }
    for (i, (prompt, arrivals)) in EARLIER.iter().enumerate() {
        for &(chan, arrival) in arrivals.iter() {
            let got = read(prompt, arrival);
            if got != want(chan) {
                return Err(format!("the parser no longer reads earlier prompt {i}, \
                                    for {chan}: {got:?}"));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_parser_reads_back_every_prompt() {
        check_prompt_shape().unwrap();
    }

    #[test]
    fn weekdays_fall_where_the_calendar_puts_them() {
        assert_eq!(weekday("2026-01-01"), "Thursday");
        assert_eq!(weekday("2026-10-01"), "Thursday");
        assert_eq!(weekday("2024-02-29"), "Thursday");
        assert_eq!(weekday("2026-03-01"), "Sunday");
        assert_eq!(weekday("2000-01-01"), "Saturday");
        assert_eq!(weekday("2026-13-01"), "");
    }

    #[test]
    fn a_look_is_handed_what_came_due_since_that_persons_last_one() {
        let at = |id: i64, date: &str, channel: &str, speaker: &str| Input {
            id, date: date.into(), channel: channel.into(), speaker: speaker.into(),
            text: "08:00".into(), ..Input::default()
        };
        let inputs = [at(1, "2026-07-18", "clock", "mei"), at(2, "2026-07-20", "clock", "fan"),
                      at(3, "2026-07-21", "dm", "mei"), at(4, "2026-07-23", "clock", "mei")];
        assert_eq!(last_look(&inputs, &inputs[3]), "2026-07-18");
        assert_eq!(last_look(&inputs, &inputs[0]), "2026-07-17");
        assert_eq!(last_look(&inputs, &inputs[1]), "2026-07-19");
        let text = clock_text(&inputs[3], &["Come due for mei after 2026-07-18:".into(),
                                            "`trajectory:aaaaaa`  2026-07-23, today  x".into()]);
        assert_eq!(text, "No message started this session. What I say now reaches mei alone, in \
a direct message.\n\n    It is Thursday, 08:00, and this is my look at the day ahead for mei.\n\n    \
Come due for mei after 2026-07-18:\n    `trajectory:aaaaaa`  2026-07-23, today  x");
    }
}
