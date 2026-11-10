# Complete continuation upgrade implementation plan

**Goal:** Finish finite monitoring work without arbitrary duration, recovery-count or object-count termination, while retaining results and never replaying unknown requests.

**Architecture:** Keep the existing leased SQLite worker, request journal and per-page publication. Separate transport failure detection from elapsed research time; recover the same durable work, isolate dependent failures and publish independently verified results. Context sizes remain batching boundaries, not evidence quotas.

**Tech Stack:** Python, SQLite, urllib, pytest, TypeScript/Node, installed Windows runtime.

## Boundaries

- Preserve unrelated changes, all existing results, request/fee receipts, configuration and pause states.
- Never change a request payload to bypass a historical unknown. No automatic product restart during active tasks.
- No new product features, redesign, public push or release. One candidate after verification.

## 1. Waiting and recovery

Files: `studio/http.py`, `llm.py`, `execution.py`, `runtime_health.py`, `store.py` under `src/project_watch/`; tests `test_studio_runtime_health.py`, `test_automatic_recovery.py`, `test_studio_llm_timeout.py`.

1. Add isolated tests for work beyond the former 300/360-second deadlines, fourth recovery, stale-owner fencing, disconnect and unknown zero resend.
2. Separate connection setup from model response waiting; do not infer a failed request merely from its age. Keep explicit network failure classification and receipt capture.
3. Remove recovery-count termination, retaining ownership checks and backoff. Unknown requests remain isolated at their dependency boundary.
4. Run focused tests, including completed-response replay and pause/consent checks.

## 2. Coverage and context

Files: `studio/monitoring_plan.py`, `relation_delivery.py`, `review_source_traversal.py`; tests `test_studio_relation_monitoring.py`, `test_evidence_delivery.py`, `test_analysis_selection_coverage.py`.

1. Test the 17th object and fifth direction item, preservation of parents/history and duplicate/cycle rejection.
2. Remove arbitrary storage admission quotas. Keep evidence/identity requirements and scheduling batches.
3. Split oversized review payloads without dropping assertions or source text; preserve cross-source contradictions and source coordinates. Freeze legacy request semantics when receipts already exist.
4. Test long sources/assertions, later contradictions and interruption/resume with no duplicated call.

## 3. Independent delivery

Files: `studio/analyze_job.py`, `section_analysis.py`, `page_summaries.py`, `runtime_health.py`, relevant task handlers.

1. Audit collection → analysis → follow-through → page publication dependency edges.
2. Persist step outcomes; continue independent work after an unknown, retain dependent work without resending it.
3. Correct page-summary partial projection. Test one failure with successful sibling publication and no false overall completion.

## 4. Acceptance and deployment

1. Run focused offline replays of existing Beiqi and SiliconFlow records in isolated copies; no production writes.
2. Run pytest, Node/frontend checks and lint. Record pass/fail/skip and fix relevant regressions.
3. Obtain one independent review using the strongest available reviewer; verify and fix actionable findings.
4. Freeze source, build once, verify exact wheel/install correspondence, fresh-install synthetic journey and Defender scan.
5. Run isolated real validation without resending old unknowns; report provider failures honestly, never equate partial with complete.
6. Wait for live jobs to finish, back up and verify data/configuration/pause/unknown receipts, upgrade once, reopen and verify.

Completion means all applicable gates have evidence and the installed candidate preserves existing state. External provider failures or dependent unknown steps must be reported as remaining gaps, not concealed or bypassed.
