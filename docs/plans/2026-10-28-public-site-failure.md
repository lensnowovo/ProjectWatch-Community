# Typed public connector failures

The guarded website connector wraps connection and TLS failures in `FeedError`.
The public-read journal previously recognized raw socket exceptions and tagged
`HttpError` only, so these wrapped failures could still stop the collection round
as an unknown outcome before research began.

Treat `FeedError` as a failed read only when the host has explicitly declared the
operation public and read-only. Preserve the failure receipt and completed
sources, continue other sources, and never resend the failed request during
replay. Do not relax TLS, URL/IP checks, leases, budgets, paid/write protections,
or historical unknown receipts. Unclassified programming/I/O failures still hold.

Regression uses the actual pinned connector with synthetic socket/TLS failures,
including certificate rejection. It verifies progress to DSH, partial coverage,
no resend, and continued protection for paid/write/undeclared calls.
