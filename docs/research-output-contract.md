# Research output and recovery contract

The decoupled implementation must retain the current product's content, not
replace it with a short generic summary. This is a compatibility specification;
the new update owner and UI retry action are not yet enabled.

| Reader | Existing content retained | Evidence / time semantics |
| --- | --- | --- |
| `CompanyDossierView` / `companyDossier` | `profile.one_liner`, sections and points, highlights, risks, questions; public business/product/person/financing facts; milestones; customer/team/capital/registry groups | Preserve each point's refs and date; internal and public records remain distinct. Publication date is not milestone date. |
| Finance / operating views | Key numbers, original periods and units, cash/runway method, underlying documents, missing/ambiguous metrics | No inferred figures from absent cells; actual/forecast and periods remain separate. |
| Competitive position | `position.verdict/confidence/summary/peers/for/against/change_if` | Comparison scope, counterevidence and conditions survive. Competitor facts never become company facts. |
| Technology | `technology.route/maturity/consensus/confidence/summary/academic/industry/risks` | Academic evidence, industry adoption and risks are separate axes. |
| Company operations | `operations.verdict/confidence/summary/for/against/change_if` | Company operations must not be replaced by industry growth. |
| Supply chain | `chain.verdict/confidence/summary/for/against/change_if`; scoped relationships | Query matches are candidates, not confirmed supplier/customer transactions. |
| Macro environment | `industry.verdict/confidence/summary/drivers/headwinds`; industry/policy/technology pages | Separate policy facts from technology progress; preserve category and original source. |
| Professional sources | Paper, patent and applicable trial source metadata, original IDs, attribution and exact evidence version | Candidate count is not confirmed company attribution. Papers need affiliation/product grounds; patents retain applicants, publication numbers and date basis. |
| Tracking questions | Question, object scope, criteria, latest assessment, supporting and opposing evidence, lifecycle | Preserve human scope and pause; unresolved questions do not authorize unlimited new collection. |
| News / today's changes | Event identity, category, importance, sources, event date | No fixed importance or array-position statistics. Collection today does not make an old event today's news. |
| Weekly digest | Period, project events, major/notable totals, document changes, issues, incomplete monitoring, export | Derived from persisted event/analysis versions; local export must not initiate model calls. |

Each published section has its own input snapshot, version, generation time and
status: current result, retained previous result, or explicit gap. A successful
collection is never relabelled failed because analysis failed. A generated draft
is not a reviewed publication. Prior results and their dates remain visible.

## First boundary implementation

`material_snapshot` reuses `studio_insight_versions` under a distinct artifact
key, without advancing insight heads. It stores immutable evidence references,
hashes, baseline judgment heads, public object versions and collection-ledger
fingerprints. All original evidence parts are resolved, without first-N clipping.
The snapshot is historical input, not permission: current disclosure, withdrawal
and exclusion are rechecked when resolving it.

`analyze_saved` is an internal draft boundary, not yet the user-facing retry
route. It has no collection or publication implementation. The injected role
must use the existing leased request journal and cumulative cost ledger. Reusing
a completed attempt returns the saved draft; a failed or interrupted attempt is
not silently run again. A distinct settled retry retains the same snapshot.
Unknown protection remains conservative until the independent-work proof in T5
is implemented. This does not claim that the normal update path is switched.

## Transition and retirement

One new coordinator will own untouched updates after integration tests pass.
Already-started jobs retain their frozen protocol. Retire the new-update use of
`event_insight_work.inputs` as a prerequisite for analysis and the requirement
to finish event publication before reading originals. Retire the bespoke
publication/analysis handoff chain for new work; retain old decoders read-only.
Do not enable two owners or reuse the private acceptance runners as product
entry points. The exact removal list will accompany T7, not a claim of removal
in this first boundary implementation.

## Storage compatibility and rollback

No table migration is introduced in this batch. Snapshot rows use the existing
immutable insight-version table with `material-snapshot-v1:<job>` keys, which
the existing judgment readers exclude. No judgment head or `studio_state`
current-analysis pointer is advanced. Attempt receipts use a dedicated job
checkpoint prefix. Older code ignores these rows; rollback leaves originals,
snapshots, failed attempts and request journals intact, rather than deleting
them. Internal document analysis remains on its existing path: this initial
public-material boundary does not authorize external disclosure of internal
documents.
