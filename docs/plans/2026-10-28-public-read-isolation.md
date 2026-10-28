# Public read failure isolation

Scope: isolate transport failure of host-declared, unauthenticated public reads.
Do not expand collection, retry requests, change model budgets or repair historical receipts.

- The host declares the read contract. GET alone, an unrecognized API, or a model
  assertion is insufficient. Paid model/search/fetch and writes retain unknown protection.
- A transport timeout/error of a declared public read is recorded as `failed_read`
  with `public_read_failed`, transport phase and sanitized failure category.
  This describes local failure to obtain usable evidence, not server completion.
- Same-job replay returns the recorded failure without transport. Historical
  `started`/`unknown` records remain unresolved; no migration clears them.
- The collection unit fails visibly and other units proceed. Completed pages
  remain ingestible with partial coverage. Failure never means zero results.
- Pause, lease loss, malformed adapter contracts and programming errors are not
  converted into recoverable transport failures. Request budgets remain enforced.

Acceptance: synthetic transport failures across public sources; paid and undeclared
requests still hold; replay/restart sends nothing; partial pages survive; host
collection reaches DSH with explicit partial coverage. Run related regressions,
lint, Node tests, wheel and fresh-install smoke. No repeated live timeout needed.

This refines the earlier blanket unknown-stop rule for future classified public
reads only. Existing unknown receipts and their protections remain unchanged.
