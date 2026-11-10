# ProjectWatch development

Read README.md and docs/developer-architecture.md before changing the product. This repository
is the source of truth for the independent PE/VC project monitoring product.

- Use one canonical ProjectWatch checkout for ongoing development. Historical research
  checkouts and verification snapshots are archives, not alternate sources of truth.
  Read docs/development-status.md before resuming validation or deployment.

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
  changes. Report
  skips and model/network limitations honestly.
- Only the personal local/SQLite edition is supported; the PostgreSQL team alpha was removed.
- UI is plain-language, neutral and information-first. No single-side decorative
  card bars; prioritize readable tables, chart evidence and whitespace.
- When revising a screen, remove redundant labels and helper copy first. Omit slogans
  and tutorial paragraphs by default. Use layout and specific action labels to make
  the next step and its effect clear on first use.
  Keep form labels, accessible names and visible consequences for destructive actions,
  payments and permanent changes. Prefer an info button with a tooltip for optional
  context. Make the explanation available on focus and tap as well as hover.
- Check visual and motion changes in an isolated preview, including narrow screens,
  keyboard focus and reduced motion. Inspect screenshots for layout and transitions
  in the browser for smoothness. Iterate when a concrete UX issue remains.
- Before delivering substantial code changes, use the strongest available reviewer
  model for one independent review. The primary agent verifies each finding, fixes
  confirmed issues and tests the fix. Review again only when a fix warrants it.
- Developer documentation and code determine current boundaries. Do not copy
  machine-specific paths or private development records into the distribution.
- Releases require explicit maintainer authorization and licensing/security review.
  Never import private development history, credentials or operational records into this public repository.
