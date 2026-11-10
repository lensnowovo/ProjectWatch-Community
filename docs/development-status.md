# Development snapshot — 2026-11-10

This branch consolidates the current local development state. It is not a release
or a claim that full regression has passed. Tests were stopped at the maintainer's
request; do not automatically resume tests, deployment, or a release from this snapshot.

## Included work

- Incremental page summaries and independently delivered judgment sections.
- Saved progress, isolated failures, and preserved unknown-request receipts.
- Monitoring, source attribution, streaming diagnostics, and interface refinements
  accumulated in the development checkout.
- The latest fix keeps a judgment-mode migration failure inside the section boundary,
  so independent follow-through and partial reporting can finish. Explicit owner redo
  retains the existing dispatch rules; no historical unknown request was retried.

## Validation at handoff

- The focused judgment-delivery and historical-500 journey checks passed: 18 tests,
  also run against the installed wheel. Independent review found no concrete defects
  in the latest boundary fix.
- The corresponding isolated installed-wheel synthetic journey and security scan passed.
- The final full regression did **not** finish. It reported a 90-second subprocess
  timeout in `test_core_without_insight.py::test_bp_incremental_history_permissions_and_restart_without_insight`.
  That timeout remains unresolved; the suite must not be described as passing.
- The real desktop instance was not upgraded with the latest candidate.

Keep private logs, model configuration, databases, installers, and historical
development snapshots outside Git. Continue work from the canonical checkout and
record any later validation separately from these historical results.

## 2026-11-10 — judgment display, competitor reading, chain collection

Design notes: `docs/plans/2026-11-10-judgment-page-semantics.md` and
`docs/plans/2026-11-10-competitor-research.md`.

**Judgment page.** An unsupported caveat no longer voids a whole section. The
generator rewrites the offending clause in place, records it in `annotations`,
and the section keeps its verdict with `state = judged_annotated`. A caveat that
leads a summary, or sits outside a summary, still demotes the section to
`uncertain` — the claim is load-bearing there. Every section now carries
`verdict_source`, `reason_codes`, `annotations`, `evidence_counts`, and `state`
(`judged` / `judged_annotated` / `undetermined` / `stale` / `absent`), so the
interface can distinguish "never published" from "no conclusion" instead of
showing one blank sentence for both. `无法判断` became `未定`: no-judgment is a
state, not a sixth verdict value.

**Competitors.** `peers` stays a name list. `position` gained
`competition_window`, `competition_signals` (at most three, each requiring a
peer, an event, an impact, and a citation) and `competition_status`. Review cost
moved from 12 to 19 fields and from one to at most two batches; `peer` was added
to `STRUCTURAL` so a missing name no longer drops the record that carries the
comparison.

**Chain collection.** Five places used to erase the material; all five are
closed: one already-triaged duplicate no longer rolls back the rest of a query
(the refusal is counted in `skipped`), `chain` keeps its own category instead of
being rewritten to `company`, `AFFECTS['chain']` invalidates only the chain page,
`SOURCE_KINDS` records chain and technology failures, and ordinary rounds rotate
up to two chain relations instead of reserving them for first/refresh.

Data note: historical rows written before this change remain under
`category = 'company'` because the old mapping erased their origin. They are not
backfilled — the original category cannot be recovered. New chain rounds write
`category = 'chain'`, so counts per category are comparable only within the same
period. `technology` still maps to `industry` because it feeds that page.

