# Local rc3 candidate input corrections

This records build inputs, not publication approval or live-service acceptance.
The exact source commit, artifact hashes and completed checks belong to the
candidate's external receipts. Earlier rc3 diagnostic outputs remain retained.

- Python `pypdf` now requires 6.21.2 or later within major 6; the parser container
  lock also selects 6.21.2 with both official PyPI artifact hashes. This addresses
  CVE-2026-115572, CVE-2026-115585 and CVE-2026-115580. Its packaged license text is
  byte-identical to 6.21.1. [Upstream release](https://github.com/py-pdf/pypdf/releases/tag/6.21.2).
- DSH remains 0.2.1-rc.2. The npm overrides select `undici` 8.12.2 and `zod` 4.6.7.
  The old lock contained fifty nested Zod 4.4.3 installations and one root 4.6.7;
  all declared requirements use major 4. The corrected lock deduplicates these
  installations to the existing 4.6.7 version. No DSH package version changes.
  The actual prepared runtime must pass the current lock and full file inventory
  verification again; a previous runtime manifest is not interchangeable.
- Node stays at the hash-pinned 24.22.1. Its built-in Undici reports 7.30.1,
  which is a patched version in the [upstream TLS options advisory](https://github.com/nodejs/undici/security/advisories/GHSA-2m5v-pch6-74hx).
- Source inventory admits only six exact existing font/brand paths with fixed
  binary hashes and corresponding complete license text hashes. New or modified
  binary files remain rejected. Text hash comparison normalizes Git line endings.

The old host-only regression fixtures explicitly admit their legacy protocol;
their original cadence, failure, source and request-count assertions remain.
Fresh default material roles continue to be exercised separately through actual
Pipeline/worker entries and native DSH contracts. An installed-wheel synthetic
worker journey uses a fake DSH transport driver; it is not installed native DSH
acceptance. Native desktop and installer receipts are recorded separately.

Legacy database upgrade correction: event revisions introduced a new dependency
on revision/dependency tables during the pre-events migration. Those schemas now
exist before conversion; an explicit transaction covers the new column marker,
event membership and revision heads. A failure rolls them back together so reopen
can retry. Existing report states, active job leases/attempts and unknown call
receipts are preserved. Current-schema ungrouped candidates are not promoted.
No new business columns or schema format are introduced by this ordering fix.
The installed-wheel smoke now opens a synthetic pre-events database and verifies
four preserved report states, four immutable heads and idempotent reopening.
The earlier R2 failure remains diagnostic evidence; rebuilt artifacts need their
own identities and affected installation checks. Rollback uses a matching backup
restored to a new directory, never old code on a migrated active database.

The legacy optional insight generator also keeps its early unavailable return:
scheduling does not load settings or build provider tools when that component is
absent. The DSH branch retains its own readiness checks and durable dispatch.
