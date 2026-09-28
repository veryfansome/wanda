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
/// The cap counts characters, not bytes: a summary with an em dash in it is
/// longer in bytes than in characters, and cutting at the cap's byte count
/// would both cut in the wrong place and split a character in half.
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

fn strip_dot(r: &str) -> &str {
    r.strip_prefix("./").unwrap_or(r)
}

/// `<spelling>:<rest>` or `<spelling>/<rest>`: the kind ("" for `entity`) and
/// the rest, which may be empty.
pub fn spelled(r: &str) -> Option<(&'static str, &str)> {
    let t = strip_dot(r.trim());
    let i = t.find([':', '/'])?;
    let k = crate::fm::spelled_kind(&t[..i])?;
    Some((k, &t[i + 1..]))
}

/// A spelling with nothing after it but `:`, `/` or `*`: the kind, and the
/// spelling as written.
pub fn kind_alone(r: &str) -> Option<(&'static str, String)> {
    let t = strip_dot(r.trim()).trim_end_matches('*');
    let t = t.strip_suffix(':').or_else(|| t.strip_suffix('/')).unwrap_or(t);
    crate::fm::spelled_kind(t).map(|k| (k, t.to_string()))
}

/// `[[` and `]]` off a value they wrap, one pair with none inside, and off its
/// first word: edges are stored as `[[<id>]]`, and `show` prints them so.
pub fn unbracket(r: &str) -> String {
    let t = r.trim();
    if let Some(x) = t.strip_prefix("[[").and_then(|x| x.strip_suffix("]]")) {
        if !x.contains('[') && !x.contains(']') {
            return x.trim().to_string();
        }
    }
    let end = t.find(char::is_whitespace).unwrap_or(t.len());
    if let Some(x) = t[..end].strip_prefix("[[").and_then(|x| x.strip_suffix("]]")) {
        if !x.is_empty() {
            return format!("{x}{}", &t[end..]);
        }
    }
    t.to_string()
}

/// A word read as an id: the kind written (None for none, or `entity`) and the
/// local part, lower case, without `./`, `.md` or brackets.
pub fn id_word(word: &str) -> Option<(Option<&'static str>, String)> {
    let w = word.to_lowercase();
    let w = w.strip_prefix("[[").and_then(|x| x.strip_suffix("]]")).unwrap_or(&w);
    let w = strip_dot(w);
    let (kind, local) = match w.find([':', '/']) {
        Some(i) => {
            let k = crate::fm::spelled_kind(&w[..i])?;
            ((!k.is_empty()).then_some(k), &w[i + 1..])
        }
        None => (None, w),
    };
    let local = local.strip_suffix(".md").unwrap_or(local);
    is_hash_id(local).then(|| (kind, local.to_string()))
}

/// A kind and a local part that is not a hash, as ids once were: `person:alpha`.
pub fn legacy_word(word: &str) -> Option<String> {
    let w = word.to_lowercase();
    let (k, local) = spelled(&w)?;
    let local = local.strip_suffix(".md").unwrap_or(local);
    if k.is_empty() || is_hash_id(local) || !is_local_id(local) {
        return None;
    }
    Some(format!("{k}:{local}"))
}

/// A hash id with `.md` after it.
pub fn hash_md(r: &str) -> bool {
    r.to_lowercase().strip_suffix(".md").is_some_and(is_hash_id)
}

static DATE_START: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d{4}-\d{2}-\d{2}").unwrap());

/// The rest after a spelling reads as a mistyped id rather than a name: its
/// first word starts with a date, holds a glob, path, kind, variable,
/// placeholder or list character (`* / : $ _ < > { } ,`), or is hex and dashes
/// with a digit.
pub fn id_like(rest: &str) -> bool {
    let w = rest.split_whitespace().next().unwrap_or("").to_lowercase();
    DATE_START.is_match(&w)
        || w.contains(['*', ':', '/', '_', '$', '<', '>', '{', '}', ','])
        || (!w.is_empty() && w.chars().all(|c| c.is_ascii_hexdigit() || c == '-')
            && w.chars().any(|c| c.is_ascii_digit()))
}

/// Items of a list, split at the commas outside `(…)` and `[[…]]`, so a note
/// with a comma in it stays with its item. A closer with no opener is text; an
/// opener never closed keeps the rest in one item.
pub fn list_outside(text: &str) -> Vec<String> {
    let cs: Vec<char> = text.chars().collect();
    let (mut paren, mut square) = (0usize, 0usize);
    let (mut items, mut cur) = (Vec::new(), String::new());
    let mut i = 0;
    while i < cs.len() {
        let pair = |a: char| i + 1 < cs.len() && cs[i] == a && cs[i + 1] == a;
        if pair('[') {
            square += 1;
            cur.push_str("[[");
            i += 2;
            continue;
        }
        if pair(']') {
            square = square.saturating_sub(1);
            cur.push_str("]]");
            i += 2;
            continue;
        }
        match cs[i] {
            '(' => paren += 1,
            ')' => paren = paren.saturating_sub(1),
            ',' if paren == 0 && square == 0 => {
                items.push(std::mem::take(&mut cur));
                i += 1;
                continue;
            }
            _ => {}
        }
        cur.push(cs[i]);
        i += 1;
    }
    items.push(cur);
    items.iter().map(|x| py_strip(x).to_string()).filter(|x| !x.is_empty()).collect()
}

static DATED_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\d{4}-\d{2}-\d{2}-([0-9a-f]{6})(?:$|[^0-9a-z])").unwrap());

/// A value written to point at a node, which a slot that mints refuses rather
/// than mints when it names nothing: a node minted from it would be named after
/// the pointer, not the thing.
pub fn reference_shaped(r: &str) -> bool {
    let t = py_strip(r);
    let lower = t.to_lowercase();
    let head = lower.split_whitespace().next().unwrap_or("");
    let head = head.strip_prefix("[[").and_then(|x| x.strip_suffix("]]")).unwrap_or(head);
    let (kind, local) = match head.rfind([':', '/']) {
        Some(i) => (&head[..i], &head[i + 1..]),
        None => ("", head),
    };
    let local = local.strip_suffix(".md").unwrap_or(local);
    let first_word_id = is_hash_id(local)
        || (crate::fm::spelled_kind(kind).is_some_and(|k| !k.is_empty()) && is_local_id(local));
    t.starts_with('/')
        || t.contains("[[")
        || t.contains("]]")
        || kind_alone(t).is_some()
        || spelled(t).is_some()
        || first_word_id
        || DATED_ID.captures_iter(&lower).any(|c| c[1].chars().any(|ch| ch.is_ascii_digit()))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kinds_are_spelled_many_ways() {
        assert_eq!(spelled("pref:1a2b3c"), Some(("preference", "1a2b3c")));
        assert_eq!(spelled("people/1a2b3c"), Some(("person", "1a2b3c")));
        assert_eq!(spelled("./topics/1a2b3c.md"), Some(("topic", "1a2b3c.md")));
        assert_eq!(spelled("Entity:Alpha"), Some(("", "Alpha")));
        assert_eq!(spelled("Re: invoice"), None);
        assert_eq!(spelled("garden/shed plans"), None);
    }

    #[test]
    fn a_kind_alone() {
        assert_eq!(kind_alone("people"), Some(("person", "people".to_string())));
        assert_eq!(kind_alone("pref:*"), Some(("preference", "pref".to_string())));
        assert_eq!(kind_alone("topics/"), Some(("topic", "topics".to_string())));
        assert_eq!(kind_alone("topic:kettle"), None);
        assert_eq!(kind_alone("*"), None);
    }

    #[test]
    fn a_word_as_an_id() {
        assert_eq!(id_word("pref:1a2b3c"), Some((Some("preference"), "1a2b3c".into())));
        assert_eq!(id_word("./events/2031-01-02-7a8b9c.md"),
                   Some((Some("event"), "2031-01-02-7a8b9c".into())));
        assert_eq!(id_word("[[1a2b3c]]"), Some((None, "1a2b3c".into())));
        assert_eq!(id_word("entity:1a2b3c"), Some((None, "1a2b3c".into())));
        assert_eq!(id_word("abcdef"), None, "six letters are a word");
        assert_eq!(id_word("person:alpha"), None);
        assert_eq!(legacy_word("person:alpha"), Some("person:alpha".into()));
        assert_eq!(legacy_word("person:1a2b3c"), None);
    }

    #[test]
    fn brackets_come_off_a_wrapped_value_and_a_first_word() {
        assert_eq!(unbracket("[[1a2b3c]]"), "1a2b3c");
        assert_eq!(unbracket("[[Alpha Beta]]"), "Alpha Beta");
        assert_eq!(unbracket("[[1a2b3c]] (a note)"), "1a2b3c (a note)");
        assert_eq!(unbracket("[[a]] and [[b]]"), "a and [[b]]");
        assert_eq!(unbracket("Alpha [[x]]"), "Alpha [[x]]");
    }

    #[test]
    fn what_after_a_kind_reads_as_a_mistyped_id() {
        for rest in ["2031-04-26-", "2031-04-15-0d1e2f3", "2031-09-20-*x*", "_last",
                     "topic:3c4d5e", "0d1e2f", "x,Alpha", "$HOME", "<id>"] {
            assert!(id_like(rest), "{rest}");
        }
        for rest in ["kettle", "garden plans", "Alpha Beta", "Filing"] {
            assert!(!id_like(rest), "{rest}");
        }
    }

    #[test]
    fn a_list_splits_outside_brackets() {
        assert_eq!(list_outside("a, b"), ["a", "b"]);
        assert_eq!(list_outside("1a2b3c (a, b)"), ["1a2b3c (a, b)"]);
        assert_eq!(list_outside("1a2b3c (a, b), 4d5e6f"), ["1a2b3c (a, b)", "4d5e6f"]);
        assert_eq!(list_outside("1a2b3c (a (b, c), d), 4d5e6f"), ["1a2b3c (a (b, c), d)", "4d5e6f"]);
        assert_eq!(list_outside("1a2b3c (a, b"), ["1a2b3c (a, b"]);
        assert_eq!(list_outside("a), b"), ["a)", "b"]);
        assert_eq!(list_outside("[[x, y]], z"), ["[[x, y]]", "z"]);
        assert!(list_outside(" , ").is_empty());
    }

    #[test]
    fn what_is_never_minted() {
        for r in ["events/2031-03-25-0e1f2a", "event:2031-09-20-*x*", "event-2031-09-07-1f2a3b",
                  "topic:_last", "topic:", "event:", "/dev/fd/63", "[[1a2b3c]]", "[[Alpha]]",
                  "Alpha [[x]]", "1a2b3c beta", "people", "entity:Alpha", "topics/kettle",
                  "[[1a2b3c]] (a note)", "[[Alpha]] (a note)"] {
            assert!(reference_shaped(r), "{r}");
        }
        for r in ["Alpha", "Acme - east branch", "flight BA2490", "Re: invoice",
                  "garden/shed plans", "Event planning"] {
            assert!(!reference_shaped(r), "{r}");
        }
    }
}
