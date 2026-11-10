# Page-specific research summaries Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Each research page opens with a concise, evidence-linked account of substantive developments, rather than material counts or instructions to the reader.

**Architecture:** Reuse saved evidence versions, request journals and immutable insight versions. A separate optional summary job reads public saved materials only; collection and existing analysis do not wait for it. Publish reviewed pages independently, retaining previous versions on failure. Never replay an old unknown request. Internal material needs its existing disclosure boundary, not inclusion in the public summary job.

**Tech Stack:** Python/SQLite, existing model journal and evidence reviewer, React/TypeScript.

1. Define explicit content contracts for every page. Documents and monitoring controls remain operational pages. Keep the five Current Judgment tabs.
2. Implement public summary drafting over the complete selected snapshot in checkpointed batches; validate page identity, concrete statements and evidence. Review assertions against originals before versioned publication. No source fetching or automatic repair loop.
3. Connect an independent summary job after report production. GET requests only read saved publications. Preserve old publications and dates when a page is missing or rejected; hide withdrawn evidence.
4. Remove frontend count/title/other-page fallbacks. Display only page-specific publications or explicitly reviewed local copy; evidence opens on click. Cover cross-page contamination, source withdrawal, retained results and no-data behavior with focused tests.
5. Validate content using isolated copies of saved materials before installation. Record actual generated/rejected/missing pages. Do not present offline fixtures as real content acceptance. Internal summaries require a separate permitted-material path before claiming full completion.

Acceptance: facts name their subject, change/result and company relationship; dates and attribution survive; clinical/paper/patent text cannot be substituted across pages; no new research or replay of unknown calls; collected records remain usable without a summary. No public release in this work.
