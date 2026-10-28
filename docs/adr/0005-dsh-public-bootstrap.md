# ADR-0005: DSH owns public bootstrap agent execution

## Status

Accepted for isolated implementation by the maintainer on 2026-10-20.
Production rollout and full monitoring migration are not approved by this ADR.

### 2026-10-20 extension: isolated public monitor loop

The maintainer also approved migrating continuous public exploration, comprehensive
judgments, scope revision and evidence-driven wake-up in isolation. The second
opt-in flag `PROJECT_WATCH_DSH_MONITOR=1` enables this route. DSH owns the
tool-selection/reasoning loop; the existing durable exploration state machine,
human ownership, evidence routing, depth/capacity/cadence limits and journal remain
authoritative. This is not permission to deploy, read internal/commercial material,
or retry an ambiguous paid call.

The public monitor uses the existing injected judgment normalizer without invoking
its legacy model loop. First bootstrap queues a DSH follow-up; later external
rounds include judgment and revision, so the old all-source automatic insight
fingerprint must not enqueue a duplicate model run. Explicit insight jobs use
the public evidence-only DSH route in this opt-in mode. Normal/default mode and
internal-document processing remain unchanged.

Trade-offs: this is still a bounded synchronous DSH subprocess per durable job,
not a persistent session scheduler. Long-running fairness/session restoration and
desktop runtime distribution require separate validation. Public-only judgments
are not replacements for authorized internal/commercial cross-source judgments.

## Context

The Studio public-information pipeline currently orchestrates model calls and
collection in Python. The old optional DSH excerpt executor is not the Studio
agent foundation. The maintainer clarified that DSH should be that foundation.
Existing durable jobs, evidence, pause state and ambiguous-payment protection
must survive migration.

## Decision

Use an official pinned DSH runtime to own the model/tool loop for the first
public bootstrap. ProjectWatch exposes a narrow company/job-bound tool surface
and a journaled model gateway. Agent outputs are validated and persisted through
existing product contracts. Keep other workflows and the current live instance
unchanged; migration is opt-in and failures do not trigger a hidden fallback.

## Consequences

Positive: real adaptive tool use, less duplicated generic agent execution,
existing UI/storage and auditability retained.

Negative: an additional Node runtime and plugin protocol to package and test;
pre-release upstream compatibility; model behavior and research quality still
require evaluation. DSH does not guarantee factual accuracy or exactly-once billing.

Neutral: durable scheduling and business validation remain ProjectWatch
responsibilities. Existing Codex/WorkBuddy and independent API paths remain.

## Alternatives considered

- Retain only custom orchestration: does not satisfy the approved direction.
- Move the whole product into DSH: unnecessary data/operational migration.
- Add a DSH wrapper around the old pipeline: does not migrate decision-making.
- Ship several default harnesses now: increases scope and validation burden.

## References

- https://deepseek-harness.github.io/deepseek-harness/en/reference/
- https://github.com/deepseek-ai/deepseek-harness
- ../developer-architecture.md (current execution boundary; historical private plans are not exported)
