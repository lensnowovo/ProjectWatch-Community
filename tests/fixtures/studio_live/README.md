# Studio public live-response replays

Captured on 2026-10-07 by `scripts/studio_live_probe.py`, querying public company
names 宁德时代 / Contemporary Amperex Technology and Moderna / ModernaTX.
These are trimmed **real public responses**, not synthetic company materials.

Each JSON retains four records with capture time, fixed source endpoint and
operation. RSS keeps title/link/date/source; JSON keeps only the metadata used by
the collector and public author affiliation strings. Article bodies, descriptions,
summaries, contacts, request/response headers, cookies and credentials are omitted.
Credential-bearing URL arguments and Bing's per-request tracking identifiers are
removed; its embedded article URL is retained to test wrapper decoding.

There are no model-completion snapshots. Replay tests never access the network.
Bocha snapshots were captured after quota became available. Brave/Tavily have only
documented-shape synthetic tests, not live snapshots. CATL's empty clinical-trial
response has no fixture because it contains fewer than three records.

Source dates and titles are not independently verified claims: OpenAlex's broad
`Moderna` keyword query includes unrelated Portuguese research and future-dated
metadata. ClinicalTrials.gov's sponsor query also matches collaborators; the lead
sponsor and collaborators are preserved separately. See the dated acceptance
report for what the real run demonstrates and what remains unverified.

The release audit permits only this directory's bounded public provenance and
the probe's metadata projection. Credential scanning still applies, and these
fixtures are not included in the distributable Python wheel.
