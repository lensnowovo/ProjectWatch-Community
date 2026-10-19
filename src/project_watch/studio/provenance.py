"""Conservative provenance/family labels; source type is not investment credibility."""

import hashlib
import re
from urllib.parse import urlsplit

REGULATOR_HOSTS = {"cninfo.com.cn", "sse.com.cn", "szse.cn", "nmpa.gov.cn", "cde.org.cn",
                   "clinicaltrials.gov", "sec.gov"}
COMPETITION_BODY_LIMIT = 19000


def effective_family(evidence: dict) -> dict:
    """Reproject known automatic snapshots, leaving immutable history intact.

    Only the captured body span participates; a publisher's headline is not a
    second source. This is duplicate-text detection, not proof of independence.
    Manual/document/multipart evidence retains its explicitly recorded labels.
    """
    recorded = {k: evidence.get(k, '') for k in ('propagation_family', 'family_basis')}
    parts = evidence.get('parts', [])
    if (evidence.get('extraction_version') not in ('competition-source-v1', 'competition-source-v2')
            or evidence.get('source_type') != 'public_page' or evidence.get('read_scope') != 'partial'
            or len(parts) != 1 or parts[0].get('locator_kind') != 'stored_text'):
        return recorded
    url, text = parts[0].get('locator', ''), parts[0].get('text', '')
    if (not url.startswith(('https://', 'http://')) or len(url) >= 500
            or evidence.get('source_key') != 'competition:partial:' + hashlib.sha256(url.encode()).hexdigest()
            or '\n' not in text):
        return recorded
    body = text.split('\n', 1)[1][:COMPETITION_BODY_LIMIT]
    labels = provenance(url, body, read_scope='partial')
    return {k: labels[k] for k in recorded}


def provenance(url: str, text: str, *, read_scope: str = "partial", company_website: str = "",
               declared_origin: str = "unknown") -> dict:
    try:
        host = (urlsplit(url).hostname or "").casefold()
        company = (urlsplit(company_website).hostname or "").casefold()
    except ValueError:
        host = company = ""
    origin = declared_origin if declared_origin in ("company_self", "regulator", "partner", "media") else "unknown"
    if host and any(host == domain or host.endswith("." + domain) for domain in REGULATOR_HOSTS):
        origin = "regulator"
    elif host and company and (host == company or host.endswith("." + company)):
        origin = "company_self"
    normalized = re.sub(r"\s+", "", text)
    # Never group all reports from the same outlet, or treat short summaries as
    # independently read original sources. Long identical text is one family.
    exact = read_scope != "summary" and len(normalized) >= 500
    basis = "exact_text" if exact else "source_only"
    key = normalized if exact else url
    return {"origin": origin, "propagation_family": hashlib.sha256((basis + ":" + key).encode()).hexdigest(),
            "family_basis": basis}
