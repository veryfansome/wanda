//! The text rules a node's fields are held to.
//!
//! These decide what a summary says, what an index line shows and what a body
//! keeps, so they are the functions a store's bytes are most sensitive to.
//! Two of them follow CPython rather than Rust where the two disagree, and
//! the disagreement is silent in both directions — see `split_whitespace` and
//! `split_lines` below.

/// CPython's `str.split()` separators: the Unicode White_Space property, plus
/// the four file/group/record/unit separators. `char::is_whitespace` follows
/// White_Space alone, so a summary containing U+001C would keep it here and
/// lose it there, and the two stores would differ by one byte with nothing
/// pointing at why.
pub fn is_py_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{1f}')
}

/// CPython's `str.splitlines()` boundaries. `str::lines` breaks on `\n` and
/// `\r\n` only; this also breaks on vertical tab, form feed, the separators,
/// NEL and the Unicode line and paragraph separators. It decides which body
/// lines are struck, so a body carrying one of them would keep a retracted
/// claim alive under `lines`.
fn is_py_linebreak(c: char) -> bool {
    matches!(c, '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}'
                | '\u{85}' | '\u{2028}' | '\u{2029}')
}

/// As `text.splitlines()`: no trailing empty element for a final break, and
/// `\r\n` is one boundary.
pub fn split_lines(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0usize;
    let mut it = text.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if !is_py_linebreak(c) {
            continue;
        }
        out.push(&text[start..i]);
        let mut end = i + c.len_utf8();
        if c == '\r' {
            if let Some(&(_, '\n')) = it.peek() {
                it.next();
                end += 1;
            }
        }
        start = end;
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// `" ".join(text.split())` — every run of whitespace becomes one space, and
/// leading and trailing whitespace goes.
pub fn one_line(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for word in text.split(is_py_space).filter(|w| !w.is_empty()) {
        if !out.is_empty() {
            out.push(' ');
        }
        out.push_str(word);
    }
    out
}

/// One line within the cap, cut at a word and marked as cut. For text nobody
/// is around to rewrite — a migration; a session gets a refusal instead.
///
/// The cap counts characters, not bytes: a summary of 82 characters can be 85
/// bytes, and cutting at byte 80 would both cut in the wrong place and split a
/// character in half.
pub fn clip(text: &str, cap: usize) -> String {
    let t = one_line(text);
    if t.chars().count() <= cap {
        return t;
    }
    let head: String = t.chars().take(cap - 1).collect();
    let cut = match head.rfind(' ') {
        Some(i) => &head[..i],
        None => &head[..],
    };
    let mut s = cut.trim_end_matches([',', ';', ':', '-', '\u{2014}', ' ']).to_string();
    s.push('\u{2026}');
    s
}

/// The id without its kind — what a directory index shows, since the
/// directory says the kind.
pub fn local_id(nid: &str) -> &str {
    match nid.split_once(':') {
        Some((_, rest)) => rest,
        None => nid,
    }
}

/// A retracted line stays in the file, and out of everything derived from it.
/// History is kept; the claim stops being true.
pub fn live_body(body: &str) -> String {
    split_lines(body)
        .into_iter()
        .filter(|l| !l.starts_with("~~"))
        .collect::<Vec<_>>()
        .join("\n")
}

pub fn marks(status: &str) -> &'static str {
    if status == "open" { " [open]" } else { "" }
}

/// Name, and the summary when it says something the name does not. A summary
/// that is the name cut short, or the name is the summary cut short, says
/// nothing twice.
pub fn line_for(name: &str, summary: &str) -> String {
    let name = one_line(name);
    let summary = one_line(summary);
    let a = name.to_lowercase();
    let b = summary.to_lowercase();

    // a cut is a prefix ending at a word boundary, so `Tony's` is a cut of
    // `Tony's — the pizza place` and `Tonya` is not a cut of `Tony`
    fn cut_of(longer: &str, shorter: &str) -> bool {
        longer.starts_with(shorter)
            && (longer.len() == shorter.len()
                || matches!(longer[shorter.len()..].chars().next(),
                            Some(' ') | Some(',') | Some(';') | Some(':') | Some('.')
                            | Some('\u{2014}') | Some('-') | Some('\u{2026}')))
    }
    if summary.is_empty() || cut_of(&a, &b) || cut_of(&b, &a) {
        return if name.chars().count() >= summary.chars().count() { name } else { summary };
    }
    format!("{name} \u{2014} {summary}")
}

/// Python's `repr()` of a string.
///
/// Two messages a session reads are formatted with it — the candidates behind
/// an ambiguous name, and the refusal for an id that names no node — so the
/// quoting rules are part of what the experiment measures. They are not
/// obvious: single quotes unless the text has one and no double quote, and a
/// character is escaped when Python calls it unprintable, which includes the
/// separators and the non-breaking space.
pub fn py_repr(s: &str) -> String {
    use unicode_general_category::{get_general_category, GeneralCategory as G};
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => { out.push('\\'); out.push(c); }
            ' ' => out.push(' '),
            c => {
                let printable = !matches!(get_general_category(c),
                    G::Control | G::Format | G::Surrogate | G::PrivateUse
                    | G::Unassigned | G::LineSeparator | G::ParagraphSeparator
                    | G::SpaceSeparator);
                if printable {
                    out.push(c);
                } else {
                    let n = c as u32;
                    if n < 0x100 {
                        out.push_str(&format!("\\x{n:02x}"));
                    } else if n < 0x10000 {
                        out.push_str(&format!("\\u{n:04x}"));
                    } else {
                        out.push_str(&format!("\\U{n:08x}"));
                    }
                }
            }
        }
    }
    out.push(quote);
    out
}

use regex::Regex;
use std::sync::LazyLock;

/// `person:7f3a2c - the neighbour` and `event:x (2026-01-01)` are an id with a
/// gloss written for a human reader. Cut the gloss.
///
/// Only ever applied to something already shaped like an id: a bare name may
/// legitimately contain a dash or a bracket, and `Acme - east branch` is not
/// `Acme`.
static GLOSS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\s+(?:[\u{2014}\u{2013}(\[]|-\s)").unwrap());

pub fn bare_ref(r: &str) -> String {
    let t = r.trim();
    // the Python pattern ends `.*$`, and `.` does not cross a newline: a gloss
    // with a line break after it is not a gloss, and the reference is returned
    // whole. Only a marker whose tail stays on one line cuts.
    for m in GLOSS.find_iter(t) {
        let tail = &t[m.start()..];
        if !tail.trim_end_matches('\n').contains('\n') {
            return t[..m.start()].trim().to_string();
        }
    }
    t.to_string()
}

static HASH_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:\d{4}-\d{2}-\d{2}-)?([0-9a-f]{6})$").unwrap());
static LOCAL_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9-]*$").unwrap());

/// Python's `$` also matches immediately before a trailing newline. Rust's
/// matches at the end of the haystack only, so one trailing newline is taken
/// off before either pattern is applied.
fn before_final_newline(s: &str) -> &str {
    s.strip_suffix('\n').unwrap_or(s)
}

/// An opaque id: six hex characters, with the date in front for an event.
///
/// The Python pattern asserts the digit with a lookahead, which the regex
/// crate does not have. With `$` immediately after the six characters the
/// lookahead can only be satisfied inside them, so the two are the same rule:
/// at least one of the six is a digit. `abcdef` is a word, not an id.
pub fn is_hash_id(s: &str) -> bool {
    match HASH_RE.captures(before_final_newline(s)) {
        Some(c) => c[1].chars().any(|ch| ch.is_ascii_digit()),
        None => false,
    }
}

pub fn is_local_id(s: &str) -> bool {
    LOCAL_RE.is_match(before_final_newline(s))
}

/// `str.strip()`: CPython's whitespace set, which is wider than Rust's — see
/// `is_py_space`. Used wherever the Python strips, so a value carrying one of
/// the four separators comes out the same length either side.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(is_py_space)
}

/// The first `n` characters, as a Python slice takes them.
pub fn take_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Something a session copied out of an index rather than a name: an id with
/// its kind, a bare hash, either with a gloss after it, in any case.
pub fn id_shaped(r: &str) -> bool {
    let head = py_strip(r).split_whitespace().next().unwrap_or("").to_lowercase();
    let (kind, local) = match head.rsplit_once(':') {
        Some((k, l)) => (k, l),
        None => ("", head.as_str()),
    };
    is_hash_id(local)
        || (crate::fm::KIND_DIR.iter().any(|(k, _)| *k == kind) && is_local_id(local))
}

/// `json.dumps(v)`: the same bytes, including the space after each `,` and `:`
/// that CPython writes by default and serde_json does not, and the `\uXXXX`
/// escape CPython puts on every character above ASCII.
pub fn py_json(v: &serde_json::Value) -> String {
    dumps(v, true)
}

/// `json.dumps(v, ensure_ascii=False)`: the same, with text above ASCII left
/// as itself.
pub fn py_json_utf8(v: &serde_json::Value) -> String {
    dumps(v, false)
}

/// One JSON string, quoted and escaped as `json.dumps(s, ensure_ascii=False)`
/// writes it.
pub fn json_str(s: &str) -> String {
    esc(s, false)
}

fn dumps(v: &serde_json::Value, ascii: bool) -> String {
    match v {
        serde_json::Value::Object(m) => {
            let inner: Vec<String> = m.iter()
                .map(|(k, x)| format!("{}: {}", esc(k, ascii), dumps(x, ascii)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
        serde_json::Value::Array(a) => {
            format!("[{}]", a.iter().map(|x| dumps(x, ascii)).collect::<Vec<_>>().join(", "))
        }
        serde_json::Value::String(s) => esc(s, ascii),
        serde_json::Value::Bool(b) => if *b { "true".into() } else { "false".into() },
        serde_json::Value::Null => "null".into(),
        serde_json::Value::Number(n) => n.to_string(),
    }
}

/// CPython escapes the same five characters by name, everything else below a
/// space as `\u00xx`, and — unless `ensure_ascii` is off — everything from
/// DEL upwards as `\uxxxx`, an astral character as the two halves of its
/// surrogate pair. `/` is left alone and the hex is lower case.
fn esc(s: &str, ascii: bool) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if ascii && (c as u32) >= 0x7f => {
                let n = c as u32;
                if n > 0xffff {
                    let n = n - 0x10000;
                    out.push_str(&format!("\\u{:04x}\\u{:04x}",
                                          0xd800 + (n >> 10), 0xdc00 + (n & 0x3ff)));
                } else {
                    out.push_str(&format!("\\u{n:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}
