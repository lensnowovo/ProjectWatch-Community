# Research records in monitoring

Structured papers and patents can enter the existing material pipeline as typed
`RecordCandidate` values. These are research signals, not company profile facts.
The pipeline retains the provider identity, immutable evidence version, original
quote location, authors/affiliations or applicants, and the claimed relationship.

Relationships are explicit: `author_affiliation`, `applicant`,
`technical_relevance`, or `unverified`. A query string is not affiliation evidence;
an inventor is not an applicant; an application/publication is not a grant.
Unverified relationships remain saved candidates and cannot publish an event.
Technical relevance does not assert ownership or product validation.

An untouched paper with a bounded bibliography, authors and a literal saved
company-name match in affiliation may skip model extraction. This only proposes
a bibliographic fact. Independent admission review, verbatim citations, source
version checks, and the existing remaining-budget gate still apply. A candidate
with no importance assessment remains unrated. Every old attempted, deferred,
settled or unknown request keeps its original contract; this feature does not
reset tasks or authorize their replay. New native DSH extraction schemas are
opted into by a frozen `bibliography-v1` task contract; old schemas remain intact.

An explicit `bibliography_once` maintenance entry can review one original paper
whose only deferral is an unattempted `publication_reserve`. It has no scheduler
or HTTP route. A separately reviewed finite grant and exact original-job plan
are required. It retains the same material task, original inputs, failed/unknown
requests and deferral history; extraction and source calls are prohibited. One
independent admission call must fit the original material budget. Failure or an
unknown result consumes this single operation without retry or choosing another
paper. The ordinary path does not silently clear such deferrals.

After admission, the normal analysis child inherits all original calls and the
original round cap. An existing tail must be terminal and have no spent analysis
model balance or unresolved calls. Its exact budget pointer is archived in the
new operation's START plan before the normal shared-budget tail advances;
original calls, extraction checkpoints and terminal job contents are preserved.
This does not reset a round or authorize additional real testing. A successful
admission alone is not proof that analysis or date-filtered brief pages finished.

Admitted records become events and feed the existing independently reviewed
analysis. Today/week windows use the reviewed event date, not collection time.
Weekly digests still require a reviewed major/notable assessment. Undated and
old records do not become current news merely because they were imported today.
Distinct paper/patent source items are not merged on similar titles; replays use
the retained source item's event identity. Cross-provider DOI deduplication and
patent-family consolidation are not introduced by this change.

## Source coverage limits

OpenAlex's affiliation text query is not a verified institution-ID or corporate
ownership lookup. Europe PMC affiliation queries search its biomedical index;
its patent search is not an exhaustive corporate patent registry. Returned hits
must be checked against author institutions, applicants and the relevant entity.
The existing web patent collector retains search excerpts as leads. Search terms,
snippet text and index dates do not establish applicant identity or legal status.

`patent_registry.normalize_page` is a **default-off offline adapter boundary**,
not a configured provider. Enabling a provider requires a separately reviewed
mapping of original publication numbers, applicant and inventor roles, explicit
status/date basis and verbatim evidence. It also requires documented access and
redistribution rights, jurisdictions, credentials if applicable, bounded paging,
error handling and the normal call journal. No scheduler, URL or credentials are
registered here. Structural validation is followed by independent semantic
review; even a schema-valid value can misstate what the source says.

Saved aliases are not automatically proven aliases. Expanded company, subsidiary,
product or applicant searches require retained public evidence linking each name
to the target, with exact quotations and source versions. Missing results mean
the bounded query did not find a usable record, not that no such research exists.
