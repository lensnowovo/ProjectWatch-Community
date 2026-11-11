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

Three review findings were fixed on top of that: an unsupported `basis` now drops
the window (number included) instead of rejecting the section; `months` survives
only when `basis` carries two dated ends, since an integer never reaches review;
and a `peer` must appear in its own cited material — membership in `peers` is
model output too, so it cannot ground a name. An invented citation code now
costs the item that named it rather than the whole section, via `_sanitize()`
before review.

**Chain collection.** Five places used to erase the material; all five are
closed: one already-triaged duplicate no longer rolls back the rest of a query
(the refusal is counted in `skipped`), `chain` keeps its own category instead of
being rewritten to `company`, `AFFECTS['chain']` invalidates only the chain page,
`SOURCE_KINDS` records chain and technology failures, and ordinary rounds include
every due chain relation instead of reserving them for first/refresh.

An interim two-per-round rotation was removed on 2026-11-11 because it woke
relations that were not yet due. The lifecycle scheduler owns cadence: incremental
rounds take every due relation, first and manual refresh rounds cover all of them,
and event-driven relations still wait for a trigger. `cadence` and `last_run_at`
remain in `WATCH_FIELDS`. `chain` is labelled 产业链 in the interface so the
material is not shown under a raw English category name.

Data note: historical rows written before this change remain under
`category = 'company'` because the old mapping erased their origin. They are not
backfilled — the original category cannot be recovered. New chain rounds write
`category = 'chain'`, so counts per category are comparable only within the same
period. `technology` still maps to `industry` because it feeds that page.

**Post-implementation review.** The source drawer now lets only its newest request
publish, so a slow reply cannot pair a new title with an older source's text.
Every occurrence of a neutralised clause is annotated, competition citations
count toward the section's source total, and a failed peer-directory read says
`名单读取失败` instead of inviting the model to invent competitors. The unwired
official-site collector and its runtime registration were removed.

Validation: the frontend passed 345 tests and TypeScript checking. The wheel
built successfully and imported from a clean Python 3.12 environment. Backend
full-regression results are recorded separately below; a focused subset is not
a claim that the whole suite passed.

**2026-11-11 — scope follow-ups.** At the maintainer's request, new analyses no longer
draft the 公司本体 (`operations`) judgment, which the interface stopped showing. This
supersedes the earlier note in `docs/plans/2026-11-11-company-progress.md` that task
scheduling would not change. The section list is frozen per job; analyses that started
before the change, and redos of a batch whose analysis used the five-section list, keep
it. Published `operations` versions are retained. Other fixes in the same pass: a failed
collect round now stamps its attempt time and cannot loop; a blocked news feed fails only
its source; an offline round waits in the same job (bounded to 24 hours) instead of failing
for a full cadence; a complete model reply that arrives after the lease lapsed (sleep) is
saved and replayed rather than left unknown. Public monitoring controls moved from the
hidden 资料与版本 page to 监控范围. The project header folds a settled round to one line,
and 当前判断 shows the period's key changes under 公司进展 instead of a separate block.

New public page-summary jobs also stop drafting the unused `operations` page; a job
that already saved or sent its page list keeps it.

Validation: full backend regression 4843 passed, 18 skipped, 1 failed. The failure is
`test_existing_agent_mode.py::test_bridge_mcp_schema_has_scope_contract`, which needs the
optional `mcp` extra that the test environment lacks; neither it nor the bridge changed.
The earlier 90-second timeout in `test_core_without_insight.py` did not recur. Node suites
passed (78 root, 359 frontend) with TypeScript and Ruff checks. A wheel built from this
tree installed into a fresh venv and passed `tests/installed_wheel_smoke.py --full-journey`
(installed bytes match the wheel, 0 live calls); the smoke needed the dev extra's `httpx`
because current Starlette's test client is not a runtime dependency. No real-model run.
