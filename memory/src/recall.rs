//! The walk: from what a session has named, out to what bears on it.
//!
//! Traversal and ranking are graph arithmetic and there is nothing for
//! judgement to add. What to recall from is the session's decision; this only
//! says what is near it, and how near.

use crate::index;
use crate::text::{line_for, marks};
use indexmap::{IndexMap, IndexSet};
use rusqlite::Connection;
use std::collections::{BTreeMap, BTreeSet, HashMap};

pub const DECAY: f64 = 0.45;
pub const HOPS: i64 = 3;
pub const LIMIT: i64 = 14;

pub struct Row {
    pub score: f64,
    pub id: String,
    pub name: String,
    pub summary: String,
    pub seeds: usize,
    pub hop: i64,
    pub status: String,
}

impl Row {
    /// The line a session reads. The score is shown to two places, which is
    /// what makes a difference in the ranking visible at all.
    pub fn line(&self) -> String {
        format!("{:6.2}  seeds={} hop={}  `{}`{}  {}",
            self.score, self.seeds, self.hop, self.id, marks(&self.status),
            line_for(&self.name, &self.summary))
    }
}

/// CPython's `round(x, 9)`, which is the decimal value correctly rounded and
/// then taken back to the nearest double — the same as `float("%.9f" % x)`.
/// `(x * 1e9).round() / 1e9` is not the same rule: it rounds half away from
/// zero, and on a tie at the ninth place the two part company.
fn round9(x: f64) -> f64 {
    format!("{:.9}", x).parse().unwrap_or(x)
}

/// Rank what the seeds reach.
///
/// Every set is walked in sorted order. Left to a hash set, the order terms are
/// added in — and so the last bits of every score — would depend on the run,
/// and the rounding at the sort would hide it from the ranking while the scores
/// a session reads still moved.
pub fn walk(con: &Connection, seeds: &BTreeSet<String>, hops: i64) -> rusqlite::Result<Vec<Row>> {
    let mut adj: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    {
        let mut stmt = con.prepare("SELECT src, dst FROM edges")?;
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            let (src, dst): (String, String) = (r.get(0)?, r.get(1)?);
            adj.entry(src.clone()).or_default().insert(dst.clone());
            adj.entry(dst).or_default().insert(src);
        }
    }

    // Convergence, weighted. A node the seed's neighbourhood reaches by several
    // paths outranks one it reaches by a single path. Credit flows forward
    // only — a node already reached at a shallower hop takes nothing from a
    // deeper one — so a well-connected node does not collect credit from
    // everything it touches.
    let mut contrib: IndexMap<String, f64> = IndexMap::new();
    let mut reached: IndexMap<String, IndexSet<String>> = IndexMap::new();
    let mut best_hop: HashMap<String, i64> = HashMap::new();
    let empty: BTreeSet<String> = BTreeSet::new();

    // One walk per seed, each with its own `hop_of`. Shared, it blocked a
    // second seed from a node the first had already reached, so convergence was
    // undercounted and the answer depended on the order the refs were given.
    for seed in seeds {
        let w = 1.0f64;
        *contrib.entry(seed.clone()).or_insert(0.0) += w;
        reached.entry(seed.clone()).or_default().insert(seed.clone());
        let mut hop_of: HashMap<String, i64> = HashMap::from([(seed.clone(), 0)]);
        let mut frontier: BTreeSet<String> = BTreeSet::from([seed.clone()]);
        let mut seen: BTreeSet<String> = BTreeSet::from([seed.clone()]);
        for hop in 1..=hops {
            if frontier.is_empty() {
                break;
            }
            let mut nxt: BTreeSet<String> = BTreeSet::new();
            for nid in &frontier {
                for nb in adj.get(nid).unwrap_or(&empty) {
                    if hop_of.get(nb).is_some_and(|h| *h < hop) {
                        continue;
                    }
                    // libm's pow, as CPython's `**` is. Repeated multiplication
                    // gives a different double from the fourth hop on.
                    *contrib.entry(nb.clone()).or_insert(0.0) += w * DECAY.powf(hop as f64);
                    reached.entry(nb.clone()).or_default().insert(seed.clone());
                    hop_of.insert(nb.clone(), hop);
                    nxt.insert(nb.clone());
                }
            }
            frontier = nxt.difference(&seen).cloned().collect();
            seen.extend(nxt);
        }
        // and within a hop. From a seed with far more neighbours than the
        // limit, every hop-1 node arrives by one path and they all tie, so
        // which of them show is arbitrary. What separates them is how much of
        // the rest of that neighbourhood each is tied to. Half weight, so a
        // direct path still beats a lateral one — and hop one only: at hop two
        // the path count already separates nodes.
        for (nid, h) in &hop_of {
            if *h != 1 {
                continue;
            }
            let lateral = adj.get(nid).unwrap_or(&empty).iter()
                .filter(|nb| *nb != nid && hop_of.get(*nb) == Some(&1))
                .count();
            if lateral > 0 {
                *contrib.entry(nid.clone()).or_insert(0.0) +=
                    w * DECAY.powf(*h as f64) * 0.5 * lateral as f64;
            }
        }
        // the nearest any seed got, for the hop column and the sort
        for (nid, h) in &hop_of {
            let e = best_hop.entry(nid.clone()).or_insert(*h);
            *e = (*e).min(*h);
        }
    }

    let mut meta: HashMap<String, (String, String, String)> = HashMap::new();
    {
        let mut stmt = con.prepare("SELECT id, name, summary, status FROM nodes")?;
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            meta.insert(r.get(0)?, (r.get(1)?, r.get(2)?, r.get(3)?));
        }
    }

    let mut rows: Vec<Row> = Vec::new();
    for (nid, base) in &contrib {
        // a dangling edge's target is a vertex in the walk and has no node, so
        // it takes credit and is never shown
        let Some((name, summary, status)) = meta.get(nid) else { continue };
        let n = reached.get(nid).map(|s| s.len()).unwrap_or(0);
        let mut score = base * (1.0 + 0.6 * (n as f64 - 1.0));
        if status == "open" {
            score *= 1.4;
        }
        rows.push(Row {
            score, id: nid.clone(), name: name.clone(), summary: summary.clone(),
            seeds: n, hop: *best_hop.get(nid).unwrap_or(&0), status: status.clone(),
        });
    }
    // Nearer first, then more converged: distance is the primary signal, and
    // how many paths arrive orders what distance leaves tied.
    //
    // The score is rounded for the sort, because two nodes the formula gives
    // the same value can differ in the last bit from the order their terms were
    // added — 0.9 + 0.225 and 0.45 + 0.675 are one real number and two floats.
    // Without the rounding that noise separates them and the id never decides
    // anything. Real differences here are thousandths, not 1e-16.
    //
    // Then the id, so the order is total: two nodes at the same distance with
    // the same score are equally relevant by the ranking's own lights, and
    // something has to choose. Arbitrary and the same every time beats a draw.
    rows.sort_by(|a, b| {
        a.hop.cmp(&b.hop)
            .then_with(|| round9(b.score).total_cmp(&round9(a.score)))
            .then_with(|| a.id.cmp(&b.id))
    });
    Ok(rows)
}

/// A Python slice: a negative limit drops that many from the end.
pub fn take_limit(rows: &[Row], limit: i64) -> &[Row] {
    if limit >= 0 {
        &rows[..(limit as usize).min(rows.len())]
    } else {
        let keep = rows.len().saturating_sub((-limit) as usize);
        &rows[..keep]
    }
}

/// The warning a read prints when the graph points at nodes that are not there.
pub fn dangling_warning(con: &Connection) -> Option<String> {
    let d = index::dangling_edges(con).ok()?;
    let (src, rel, dst) = d.first()?;
    Some(format!("warning: {} edges point at ids no node has, e.g. {src} --{rel}--> {dst}\n",
                 d.len()))
}
