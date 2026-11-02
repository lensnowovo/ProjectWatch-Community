# Evidence-bound professional retrieval

Use the existing bounded collection role and ordinary first/incremental update
entry, not a parallel research engine. Existing frozen collection inputs keep
their original prompt/tools; untouched collection roles freeze a new contract.

1. Read already collected public disclosures. A specialist query can carry a
   proposed same-entity, former-name, brand, subsidiary, product or technical
   relationship, the named subject and an exact public evidence version/span.
   Both subject and query must occur in that span. This is a query hypothesis,
   never an automatic company-ownership assertion. Withdrawn or changed scope
   is rejected before dispatch and downstream evidence use.
2. The ordinary entry's base plan sends the owner-confirmed subject names (the
   subject object's name, current public aliases, then public aliases whose
   validity has ended) to the institution and applicant indexes, at most three
   per adapter, as the legacy collector did. OpenAlex and Europe PMC (papers and
   patents) try Latin-script names first; the web patent search keeps the
   owner's order. A name change during the round stops it before further
   dispatch. Internal and not-yet-valid aliases are not sent. Model-discovered
   names still need the evidence-bound query below. A successful base query of
   a confirmed name counts as the institution receipt for a product fallback
   when the cited excerpt names that confirmed name, the subject and the product.
   Try institution/applicant names before product topics. A product fallback
   requires a current public excerpt naming subject, institution and product,
   plus this project's current job's successful query of that institution. Zero
   results or candidates whose original affiliations do not contain that name
   allow a fallback; any literal affiliation match conservatively keeps it shut.
   A candidate count is not a relevance verdict. Missing receipt material cannot
   unlock it. At most one product dimension per frozen collection role.
   All existing action, source, model, material and analysis budgets still apply.
3. Expose Europe PMC tools in the new native DSH contract. Patent discovery uses
   the configured search provider; an exact publication number from saved public
   evidence enables one Google Patents document GET. Google is a third-party
   source, not an official legal register. Require consistent document identity
   and explicit original assignee fields, retain current owner separately, and
   never infer grant/legal validity from a suffix. HTML/identity/parser failures
   stay failed source receipts; no fabricated empty success or retry.
   Dates accept bibliographic `time datetime` and text fields. Fields are the
   microdata properties of the page's first top-level item (the result
   article), wherever its layout puts them: the live page has the title as a
   bare span and owners/dates as `dd` rows outside any `dl`. Properties of nested
   items (other versions, events, family, citation rows, claims) never supply
   or override this document's fields. Each field retains the HTML slice,
   character offsets and attribute, with decoded and raw response SHA-256 hashes.
   The layout was checked offline against saved public US grant, WO application
   and TW application pages; committed tests use synthetic values in that
   layout. The live pages carry no family ID. The page's own "Other versions"
   list (same application, e.g. a pre-grant A1 beside its B2) is kept as
   publication numbers with field provenance; nothing is fetched from it. This is not a live-provider
   compatibility certification or a product network run.
4. Preserve publication dates, application dates, discovered-at and source
   versions separately. Family grouping is a display/count view only: retained
   documents and amendments are not deleted or merged by similar title. Without a
   provider family ID, collected documents linked by one document's listed other
   versions are grouped (`basis: listed_versions`); never by title or applicant.
   Unknown families remain independent documents. Independent semantic review still
   precedes events, analysis, today's view and weekly reports.
   The UI exposes loaded / total collected family documents across pagination;
   that total is not the worldwide family size.

Alternative considered: a new registry service would add configuration and
unverified coverage; a list of known company IDs would overfit acceptance cases.
Both are excluded. This batch reuses the current journal, immutable evidence,
scope guards, material admission and projections without a new budget platform.

Verification: synthetic unrelated companies, same-name/technical-only hits,
query echo without affiliation, withdrawn scope, application versus grant,
original versus current assignee, family versions, parser failure and legacy
wire preservation. Actual provider HTML and real journeys require separately
reviewed finite source/model scope; offline parser tests cannot prove live access.
