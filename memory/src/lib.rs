//! wanda's memory: the store, and the rules a node is held to.
//!
//! The vault is the truth and everything else is derived from it — the
//! index, the search table, the generated CLAUDE.md surfaces. What lives here
//! is what decides a node's bytes; `mem` is the CLI that drives it.

pub mod fm;
pub mod index;
// the store as a Python module, for the tools that read a finished run
#[cfg(feature = "python")]
mod python;
pub mod recall;
pub mod text;
pub mod transcript;
pub mod vault;

/// The index line's cap, in characters. Over it a write is refused rather
/// than cut: the session that has the context rewrites it.
pub const SUMMARY_MAX: usize = 140;
