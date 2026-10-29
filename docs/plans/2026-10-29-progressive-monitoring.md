# Progressive monitoring implementation

Keep the current SQLite jobs/leases, request journal and unknown protection,
CollectionLedger/Ingest, immutable evidence and dependency invalidation. This
program separates source visibility, event processing, analysis/review and scope
maintenance without introducing another scheduler or distributed infrastructure.

## P1: source visibility

CollectionIngest already commits source items, versions and observations per
successful unit. The normal feed deliberately excludes untriaged items. Add a
separate read-only project source view using those saved rows. Do not promote
unreviewed results to confirmed news, change category counts, or call providers.

Alternatives rejected: removing the normal feed's triage filter would confuse
discovery with accepted findings; creating another source store duplicates the
transactional ingest already present. The independent projection preserves both.

Expose source title/link, publisher, supplied publication date, discovery time,
saved evidence quality and processing status. Missing dates stay unknown. Hide
dismissed and reviewed irrelevant items; enforce project and public evidence
boundaries. Pagination is deterministic and bounded. Refresh reads saved state;
downstream unknown or failure does not remove existing source visibility.

Exit checks: visibility while collection is incomplete, after failure and after
reopening storage; replay does not duplicate; private/other-project/dismissed and
irrelevant sources do not leak; read API performs no writes or calls. Frontend
must show pending sources separately from accepted events and never expand on
hover.

The discovery snapshot ID remains stable on the response. A current own-body
pointer supplies quality only when its read status and public/project boundary
are valid. Donor text does not upgrade the source's own reading quality. A
withdrawn snapshot or body hides the source; no older snapshot is resurrected.

Professional-source baseline: current host collection runs before DSH. Existing
source receipts distinguish pending, unavailable, no matches and saved counts;
clinical registry records and papers are not inferred from news headlines. The
older installed build lacked this path. Synthetic host/report regressions cover
the current path; they do not establish actual paper/clinical recall for a company.

## P2 foundation (not the complete event-processing stage)

Successful source ingest now records one material task per public evidence
version and policy, atomically with its source/observation. Tasks retain their
original parent job. Replay does not create a fresh task or fresh budget.
The local processor uses that parent's existing lease and JobExecution; it has
no scheduler or transport. A provider adapter must use that same journal.

Model proposals select stable part IDs; the host restores exact immutable text
and positions. These are candidate source assertions, not approved facts or
news. Invalid proposal structure/reference affects its own material; unknown,
budget and lease failures stop dispatch without erasing saved results. Input
dependencies use the existing stale/rebuild mechanism. Withdrawal excludes input
before extraction and is rechecked inside the output commit transaction.

Still required for P2 completion: wire the fixed DSH extraction adapter into
normal execution under a single pipeline owner; bounded scheduling/backpressure;
explicit recovery of old-owner tasks without bypassing unknown; event revision,
merge/split and correction projection. The foundation does not yet replace the
current DSH batch, publish event observations or invoke extra model requests.

The extraction adapter now has a pinned-runtime synthetic integration check:
fresh session per material, one allowed structured submission tool, typed tool
receipt, same parent's journal and budget, separate runtime/result checkpoint
names. At most two model turns are admitted by a role instance; exhaustion
persists an invalid candidate for per-material isolation. A received candidate
replays locally. Existing project holds still prevent new role dispatch.
These checks prove isolation and receipts, not extraction quality or complete
pipeline migration; normal monitoring has not been switched to this adapter.

## Remaining stages

P0: verify pinned DSH composition and the existing offline invariants.
P2: per-version event tasks, stable evidence references and reversible revisions.
P3: fresh analysis/review roles, shared budget and existing semantic gates.
P4: independent base listening, event response and low-frequency scope upkeep.
P5: consolidated regressions, fault injection, wheel/fresh-install verification
and explicitly bounded quality evaluation. Synthetic checks are not evidence of
live discovery speed or semantic accuracy. No stage is complete merely because
this plan or P1 has been committed.
