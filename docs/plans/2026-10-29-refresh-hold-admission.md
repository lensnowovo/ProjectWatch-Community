# Manual refresh and independent request recovery

Manual external refresh checks the same effective request holds used by recovery
before creating a job. A blocked refresh returns HTTP 409 with `refresh_blocked`
and `dispatch: not_started`. This describes the refresh action, not a new
`not_sent` provider receipt. Existing queued/running external work is coalesced.

Failed historical jobs with zero request receipts can be shown as blocked by
earlier effective requests. A job that actually sent a request retains its own
uncertainty state. The task view opens the project-wide receipt lookup, including
history omitted from the visible task list. Historical unknown rows that already
have consumed recovery records remain in the audit but are not active holds.

The legacy `business` and `details` extractors use frozen input manifests and
ordinary journaled model HTTP, not DSH continuation. When such a failed job has
exactly one pending model request and no pending query, the owner may explicitly
approve that request despite another job's pending receipt. The same original
job, input binding, lease, cumulative budget, request hash and provider remain
required. Journal admission only permits the unconsumed, authorized successor
key; a different payload/provider, extra batch or new task cannot cross the
remaining hold. Completed calls are replayed locally. Another failure requires
new consent. No old receipt is deleted or relabeled as known success.

This does not weaken DSH's project-wide guard or `model_continuation_slot`.
Viewing a recovery dialog or installing the fix never authorizes dispatch.
If frozen inputs/configuration have changed, recovery remains unavailable.

The portfolio/feed `major_recent` KPI now uses the same validated event/legacy
Agent publication date and UTC bounds as dated news/report reading. Undated,
invalid, future and out-of-window events remain stored but are not counted as
recent. Internal research lookback and the existing unread-attention definition
are unchanged; the latter is explained separately on the feed.

Validation uses synthetic receipts and transports, including two simultaneous
independent holds, exact sequential consent, payload/provider/kind/version/
lease/budget/input-binding rejection, failed retry, idempotency, consumed
historical recovery and reader-date edge cases. No real recovery is exercised.

After an explicitly recovered legacy request succeeds, a later batch may still
be blocked by another task's pending receipt. This is a queued wait, not a new
unknown request or an entirely unexecuted task. The worker parks the same job,
and queue admission skips it until all project holds settle through their own
existing recovery consent. It then continues saved proposal checkpoints under
a new lease, retaining input binding, cumulative budget and immutable call
receipts. Project pause and changed-input guards still apply. No separate job,
budget reset, or implicit authorization for another unknown is introduced.

The bounded regression exercises the real three-batch splitter, LLM error
propagation, worker and journal: saved batch, unknown batch explicitly recovered,
next batch held without sending, independent second request explicitly settled,
and same-job completion. Budget exhaustion and changed inputs after settlement
remain blocked. No real provider is contacted.
