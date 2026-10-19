# ProjectWatch development

Read README.md and docs/developer-architecture.md before changing the product. This repository
is the source of truth for the independent PE/VC project monitoring product.

- Personal self-hosted monitoring: internal document updates and external public
  monitoring, not a general-purpose project-management suite.
- Preserve unrelated edits. Work on feature branches; the initial verified import
  is the bootstrap exception. Never force-push shared branches.
- Private data, originals, credentials, model settings, logs and databases stay
  outside Git. Only synthetic test fixtures may be committed. Never connect to
  Greenpine production or copy its credentials.
- Honor project/provider-bound consent, source versions and disclosure rules.
  AI proposals and public signals are not confirmed private investment facts.
- Existing localhost/WSL/Docker services are not disposable test fixtures. Use
  separate temporary databases and free ports; do not silently downgrade isolation.
- Run pytest, Node tests, lint, wheel build and fresh-install smoke after risky
  changes. PostgreSQL tests require a dedicated empty `_test` database. Report
  skips and model/network limitations honestly.
- Local/SQLite and PostgreSQL/team alpha do not have identical capabilities.
- UI is plain-language, neutral and information-first. No single-side decorative
  card bars; prioritize readable tables, chart evidence and whitespace.
- Developer documentation and code determine current boundaries. Do not copy
  machine-specific paths or private development records into the distribution.
- Releases require explicit maintainer authorization and licensing/security review.
  Never import private development history, credentials or operational records into this public repository.
