# News navigation, importance and date integrity

Scope: isolate three defects without changing the active desktop instance or adding company-specific rules.

- Give external navigation and discovery panels distinct, project-scoped React keys. Exercise refreshes, project changes and tab transitions on the actual Project page.
- Extract optional editorial importance and its evidence-based rationale in the existing DSH material/record call. Review the rating separately in the existing admission batches. Unsupported ratings remain pending without discarding accepted factual news; model-call budgets, source bindings, checkpoints and unknown protection remain in force. Added fields can increase the existing review batch count, never the frozen budget.
- Preserve unknown occurrence dates. A reviewed unambiguous occurrence date may project to the event; collection timestamps never enter recent-event counts. Future/invalid dates do not count. Source publication metadata remains available separately.
- Expose pending status for identifiable legacy DSH defaults at read time. Preserve legacy rated results and explicit human feedback; do not backfill or run model calls on historical records. Existing events are not automatically re-rated by this change.

Validation uses fictional fixtures, blocked network calls, real components and an isolated wheel install. No production installation, data migration, retry of unknown requests or release is included. Offline fixtures verify contracts and rendering, not real-model rating accuracy.

## Isolated verification

- 108 relevant Python cases passed across the projection/review/bootstrap/material/extraction/membership suites. The three native DSH cases were run explicitly against the pinned runtime after the first offline run skipped them.
- 311 frontend and 87 core JavaScript tests passed, together with TypeScript, scoped Ruff and the production frontend build.
- Actual Project page: 20 external/internal tab cycles and 25 refreshes left exactly one category navigation, with no duplicate-key errors. Mixed and unknown-date fixtures displayed the expected totals; the 390px viewport had no horizontal overflow. A reusable Playwright callback is in `frontend/tests/news-navigation.browser.js`.
- The final wheel was installed in an independent venv, reusing existing verified dependency packages. Nine new regression cases and the fresh-user synthetic journey passed from that installed package, with no live provider calls. All 315 wheel product files matched source bytes.
- This is targeted isolated validation, not a full repository CI run or a fresh-machine installer certification. Historical re-rating and real-model semantic quality are not claimed.
