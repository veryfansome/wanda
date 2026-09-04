# wanda memory — open items

Everything with a decision has shipped. What's left is a few small defects,
some residuals that need a decision, and work deferred until there's a reason
to do it. Items name files and symbols, never line numbers, which drift.

There is no deployment yet — no vault, no `wanda.db`, no ledger history — so
nothing here carries a migration constraint; each item can take the simplest
correct shape.

## Likely defects (small, no product decision needed)

- **`retire` commits with `git add -A`.** `passes.retire` ends in
  `_git_commit_all`, which stages the whole vault, so an owner edit in flight
  is swept into a `curated:` commit and the next pass's `_absorb_owner_changes`
  never pins it. The path set is in hand at the call site — commit named paths
  instead, as `MemoryService.apply_now` already does.
- **A rule targeting a retired prefs stub is never applied.** `_prefs_note`
  raises `Deferred` when the target `prefs/*.md` is a retire stub, so the rule
  is dropped — the owner gets a digest line after five passes and nothing else.
  Decide: follow the stub's `superseded_by` to the live successor, re-mint the
  prefs note, or refuse the rule outright.
- **Unbounded pref selector.** `_prepare_writespecs` selects prefs
  `ORDER BY score DESC` with no `LIMIT`, so a large prefs set can build an
  oversized write-spec rewrite prompt. Bound it.

## Security residuals (disclosed; each needs a decision)

- **Assisted forged `attest`.** An `attest` confers owner tier only on the
  exact claim its ref names, which shuts the unassisted case. A session that
  forges the `attest` line **and** rewrites the quoted claim text can still
  reach owner tier, because the verifier recomputes the claim from the vault
  the session just edited. Closing it means binding the attest to content the
  session cannot rewrite — e.g. a content fingerprint held in `Authority` at
  attest time, the way an owner command's authority already works.
  (`index._index_claim` attest branch, `passes.make_owner_verifier`.)
- **Canonical-prefs ranking.** A hand-made `prefs/aaa-copy.md` can out-rank the
  real prefs note for the line rendered in the always-loaded projection;
  ordering by insertion id only makes the choice deterministic, not canonical.
  Prefer the canonical note using the facet→slug map in `_prefs_note`.

## Retention (deferred — a decision per table)

The ledger (`belt/ledger/`) and the wanda.db tables `memory_meta`,
`memory_shas`, `memory_offers`, `memory_owner_checks`, `memory_run_windows`
and `memory_digest` all grow unbounded, and **every candidate is a trust
input**: pruning `memory_run_windows` promotes old email-tier lines to session
tier; pruning `memory_owner_checks` / `checked:` demotes owner lines; pruning
`memory_shas` makes every claim read as owner-edited. Each table needs a
retention rule stated with its accepted tier/drift consequence. Separately, the
ledger scans in `_verify_owner_lines`, `_load_ledger`, `_pending_ops` and
`apply_now` are each O(total history) — this wants a hot-window watermark or an
obs cache keyed on file mtime before the ledger is large.

## Architecture (deferred — design questions)

- **Per-pass rollback.** A pass that raises mid-way leaves the vault partially
  written; recovery today is git, the retire journal, the staging dir, and each
  step re-deriving its own applied-ness. Decide whether a partially-applied
  pass is acceptable and, if not, which mechanism (a staging root + atomic
  swap, or git-reset-on-failure — which can discard an owner edit that landed
  during the pass).
- **Pass registry / locked connection.** A registry, or a
  `Services.locked_conn()` context manager, would collapse the repeated
  `memory_lock` + `open_conn` + `close()` sites. Worth doing once it's known
  whether a third pass is coming and what a pass may assume.

## Cleanup (deferred)

- **Unread index columns.** `claims.sha`, `docs.mtime` / `sha` / `created`,
  `edges.value`, and the whole `writespecs` table are written and never read.
  Decide whether they are intentional debugging surface; if not, drop them with
  a schema version / drop-and-recreate step. (`meta.rebuilt_at` now has a
  reader.)
- **Smaller one-liners.** `fsck` run inside a pass and its digest-noise budget;
  a `NEAR_TRIGRAM_STRICT` retune (a wrongly-merged person vs a duplicated one);
  an un-quarantine verb (who may run it, does it re-verify); a second trust
  path into `tier_for_obs` from lines a pass authored itself; an incremental
  rebuild (~390 ms at 1,000 notes, first-run only — no evidence of need); the
  `memory_writespec_owner_only` default; packaging `PROMPTS_DIR` and
  `sync_workspace` together; and the write-spec size chain, where the prompt
  asks for 1,200 B, `WRITESPEC_MODEL_CAP_B` accepts 1,500, and the walk budget
  in `recall` (`WALK_CAP_B`) allows 1,650.

## Test gap

- **`MemoryService.run_nightly` is never exercised.** It is the path production
  takes, but the tests stub it out and drive `passes.nightly` directly, so its
  `BudgetReached` branch never runs under test. Add a test that drives the real
  `run_nightly`.
