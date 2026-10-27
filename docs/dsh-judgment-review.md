# DSH judgment evidence review

The host reviews a normalized public judgment before saving it. The review uses a
separate model context containing the exact candidate and all exposed public
evidence, including evidence the candidate did not cite. It has no tools and no
generation transcript. The configured model is reused; this is not independent
provider corroboration or a guarantee of truth.

Selected-topic answers use the same gate with their own text/status/gaps schema
and selected question. They do not become global company judgments. A prior job
checkpoint without the review version cannot resume as an accepted judgment.

The reviewer covers every nonempty semantic field (including headline, summaries,
evidence points, verdicts, qualifications and gaps). Python verifies exact field
coverage, source IDs and contiguous quoted excerpts. Semantic agreement remains
a model decision and requires live evaluation. Search summaries remain summaries.
No company names, industry terms or phrase-specific rules are added.

Only a passing review allows saving. Contradicted or insufficient statements are
returned to DSH for correction, without rewriting source records. At most two
distinct normalized drafts are reviewed per job; existing model budgets still
apply. Exhaustion, malformed reviews and unknown requests never imply approval.
Unknown results retain the normal recovery gate and are not automatically retried.
Exact successful review receipts may be reused; changed inputs or review policy
invalidate reuse. Old judgment proofs are invalidated by the new review policy.

This change does not introduce a claim ledger, new sources, model configuration,
UI changes or a new analysis platform. Installation and public release require
their existing gates.

## Record admission and exact quotes

Record admission v2 isolates a review's noncontiguous or altered quotation to its
own record. That record is rejected; other records may proceed only after their
own review and the existing consistency checks pass. The original evidence is
retained, and no quote is normalized or repaired to make it pass.

This isolation requires complete, unique field coverage, valid review fields and
known source identities. Missing fields, malformed witnesses, unknown sources or
unknown requests still stop the batch. No automatic retry is introduced. Final
judgment validation remains strict; this record-level policy does not relax it.
Receipts from the older record-admission policy cannot authorize new projections.

## Bounded review requests

Review v2 assigns at most 12 fields and 6000 field-text characters per request.
Each request retains the complete candidate and all original exposed evidence;
counterevidence is never filtered by the candidate's citations. Oversized fields
or plans exceeding 24 requests are rejected before dispatch, not truncated.
Multi-batch candidates also receive a final cross-section consistency check.
Each validated batch has a durable receipt bound to the full candidate, context
and policy. Partial coverage cannot admit a judgment. Identical completed batches
can be reused only after the normal lease, pause and unknown guards pass.

Review output is capped at 8192 tokens (or the user's smaller limit). The gateway
reuses the normal structured-model provider options, including low reasoning for
GLM-5.3. Individual upstream requests retain their 300-second deadline and no-retry
policy. The local judgment tool waits for the host's bounded review plan; the host
still owns the overall round deadline and drains in-flight requests on exit.
Whitelisted transport phase and failure codes are persisted without raw errors.


## Capacity during evidence projection

The existing monitoring-plan limits remain unchanged. A typed capacity rejection
can defer a valid candidate without discarding research or failing finalization.
Deferred candidates retain their record keys and a capacity reason in the round
receipt; the round is partial, including incremental monitoring. Admission keeps
record order, existing human choices, retired relations and the write-time limit.
Other validation and lease errors still stop the write. This does not promote
trial relations, increase collection scope, or claim deferred targets are monitored.
