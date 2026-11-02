# Material output limit

A provider can settle a request with `finish_reason=length` while returning only
reasoning, or an incomplete tool call. This is a paid, known response, not an
unknown request and not a rejected evidence claim.

Fresh material requests use the configured output allowance up to 8000 tokens;
lower settings remain binding. Exact completed legacy 4000-token requests retain
their original wire identity and cache lookup. Request counts, time windows,
input limits and unknown guards are unchanged.

The gateway journals the original response, records `material_output_limit` with
request/response hashes, and prevents truncated tool execution. The material
stays pending. Further extraction in that batch stops, including after restart;
existing candidates can still pass the normal semantic admission. With no
candidates the result is explicitly partial, with zero published records.

This change does not repair historical rejected rows, reset budgets, retry a
settled request, or extract facts from reasoning text. Continuing an old failed
batch needs a separately reviewed, finite same-ledger path that preserves all
previous receipts. A larger output allowance is not proof of successful research.
