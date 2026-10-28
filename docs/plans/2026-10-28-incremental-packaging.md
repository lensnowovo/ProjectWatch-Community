# Windows incremental packaging implementation plan

**Goal:** Cache unchanged build components and produce a manually applied, exact-base file update without shipping unchanged Python/DSH/Node bytes.

**Architecture:** Keep the existing full installer and manifest-addressed version directories. A verified build cache holds dependency-only Python runtimes and shared runtime trees; the application wheel is installed separately. An update contains the target manifest, changed/new files, and an offline maintenance runner. Under the existing program/home locks, verify the exact base and local modifications, reconstruct a new payload, back up data and atomically activate through existing maintenance code. Never patch the active files in place.

**Tech stack:** Python standard library, existing pip/wheel locking, NSIS full installer, Windows command wrapper. No network updater, binary diff, automatic downloads, new business features, or production access.

## 1. Verified build cache

- Add `scripts/windows/build_cache.py`, integrate optional `--cache-dir` in `scripts/build_windows_bundle.py`.
- Cache key covers component inputs, target ABI/platform and build-tool versions/source. Python dependency key excludes the product wheel. Product is overlaid from a separate staging install to avoid pip replacing dependency console scripts.
- New entries are staged and renamed; hits verify inventory and hashes. Corrupt, linked and partial entries fail closed. Copies must not share writable hard links with cache.
- Test key invalidation, hit/no-builder behavior, corruption, added files, unsafe paths and independent copies in `tests/test_windows_build_cache.py`.

## 2. Exact-base manual updates

- Add `scripts/build_windows_update.py`, `scripts/windows/apply_update.py`, and a manual command wrapper; reuse locked install transaction from `scripts/windows/maintain.py`.
- Package only changed/new application files and changed shared runtimes; removed files are omitted from the new version, never deleted from the old version.
- Refuse wrong base, changed/missing/extra installed files, modified launcher/runtime, changed bootstrap, incompatible Python, downgrade, bad paths/hash, running application and failed backup/staging. Preserve old pointer and original data on failure.
- Lock before inspecting/staging; never run workers or migrations. Explicit confirmation is required at the manual entrypoint. Local changes require maintainer/user reconciliation, never silent overwrite.
- Test successful app/dependency update, removal, runtime reuse/update, local edits, invalid base/payload, interrupted staging, held locks and backup failure in `tests/test_windows_update.py`.

## 3. Acceptance and documentation

- Run targeted installer/cache/update tests, repository lint and relevant Node tests; build wheel and verify fresh installed smoke if required by changed scope. No paid calls.
- Use the already verified R11b and PDF1 bundles in a new private directory to measure actual update size and apply to an isolated install with synthetic data. Verify exact target files, retained config/pause/unknown and unchanged real instance.
- Exercise cache cold/hit with real inputs; report timings and remaining copy/hash work honestly. No claim of zero-cost full installers.
- Add user/build documentation with full-installer fallback, explicit checksum verification, no automatic install, local modification protection and rollback/data-schema limits.
- Stop when these checks pass; do not redesign the monitoring product, migrate real installations or publish without a separate release action.

## Acceptance boundary

Full installers remain for first install/offline recovery. Small updates require the exact declared base. SHA-256 detects corruption and binds the selected artifact; it is not a substitute for a trusted release channel or code signing. Old versions/backups remain; successful-version downgrade after schema changes requires a verified matching data restore, not a blind pointer flip.
