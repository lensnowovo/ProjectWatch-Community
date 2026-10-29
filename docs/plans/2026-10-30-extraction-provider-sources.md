# Bounded extraction and visible professional sources

Fresh native DSH extraction requests reuse the existing provider capability
mapping for reasoning effort / thinking mode only. Tool arguments keep their
existing schema; text JSON response mode is not attached to a tool request.
The output allowance remains bounded by the configured value and 8,000 tokens.

Before applying options, both legacy 4,000 and 8,000 token wire identities are
checked, including inherited material calls. Completed requests replay their
exact wire. Started/unknown requests block; other recorded/attempted identities
retain the old journal path. A settled length response keeps material pending
and stops the batch. This change does not authorize another request or reset a
consumed material/grant, and does not guarantee provider completion.

The professional pages already use `research-sources`, independent of the
admitted event feed. Source counts and pagination must be checked through that
API and the actual page, not through legacy `list_items` alone. Pending sources
now show a short relevance status; collection time and supplied DOI/patent ID
remain in click-to-open source details. No relevance/triage flags, event
admission rules, source exclusions, or project settings are changed.

Verification is bounded to provider routing, exact legacy receipts, unresolved
request protection, native tool exchange, output-limit stopping, professional
source API/page pagination and fresh installed product checks. Saved real
responses may be replayed offline. Live semantic acceptance remains separate;
no real model/source request is part of this implementation batch.

Provider reference: [official GLM-5.3 model card](https://huggingface.co/zai-org/GLM-5.3/blob/main/README.md)
documents `reasoning_effort` low/high/max and default max. Only the already
supported official endpoint/model combination receives this option.
