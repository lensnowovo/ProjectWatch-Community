"""Local original-material lookup, not an impact model or identity resolution.

Only explicitly selected public terms already present in a story are used. No
private context becomes a query, no model/network calls, no formal fact writes.
"""
import re

RULE_VERSION = 'external-material-context-v1'
MAX_MATERIALS = 3


def _pattern(term):
    # Short/broad words would link almost every document, not help understanding.
    if sum(c.isalnum() for c in term) < 4 or term.casefold() in {'公司信息', '行业动态', '人工智能'}:
        return None
    left = r'(?<![a-zA-Z0-9_])' if term[:1].isascii() else ''
    right = r'(?![a-zA-Z0-9_])' if term[-1:].isascii() else ''
    return re.compile(left + re.escape(term) + right, re.IGNORECASE)


def attach_internal_context(repo, project_id, groups):
    """Attach bounded, freshly located original quotes to existing public stories."""
    stories = [item for group in groups for item in group['items']]
    patterns = {item['match_term']: _pattern(item['match_term']) for item in stories}
    matches = {term: [] for term, pattern in patterns.items() if pattern}
    if matches:
        sources = sorted((s for s in repo.list_sources(project_id) if s.project_id == project_id),
                         key=lambda s: (s.created_at, s.id), reverse=True)
        seen_hashes = set()
        for source in sources:
            if not re.fullmatch(r'[0-9a-fA-F]{64}', source.content_sha256) or source.content_sha256 in seen_hashes:
                continue
            seen_hashes.add(source.content_sha256)
            fragments = [f for f in repo.list_fragments(source.id)
                         if f.project_id == project_id and f.source_id == source.id and f.locator.strip()]
            for term in matches:
                for fragment in fragments:
                    match = patterns[term].search(fragment.text)
                    if not match:
                        continue
                    # Preserve the exact original slice (including negations), with
                    # an explicit clipping flag rather than silently rewriting it.
                    start, end = max(0, match.start()-120), min(len(fragment.text), match.end()+220)
                    matches[term].append({
                        'project_id': project_id, 'source_id': source.id,
                        'reference': source.reference, 'version': source.content_sha256,
                        'fragment_id': fragment.id, 'ordinal': fragment.ordinal, 'locator': fragment.locator,
                        'quote': fragment.text[start:end], 'excerpt_clipped': start > 0 or end < len(fragment.text),
                        'received_at': source.created_at.isoformat(), 'material_period': None,
                    })
                    break  # At most one excerpt per distinct original document.
    for item in stories:
        term = item['match_term']
        pattern = patterns[term]
        # Recheck the actual title/summary, not a stale or overly broad matcher result.
        refs = matches.get(term, []) if pattern and pattern.search(item['title']+' '+(item.get('summary') or '')) else []
        boundary = ('两侧仅共同提到这个关注词；尚未核实是否为同一主体或存在业务关系。'
                    '材料接收时间不是业务发生时间，不能据此判断影响或因果。')
        if item['category'] == 'policy':
            boundary += '政策适用地区、对象及生效条件仍需核实。'
        published = item.get('published_at')
        item['internal_context'] = {
            'project_id': project_id, 'rule_version': RULE_VERSION,
            'status': 'shared_term_only' if refs else 'no_located_match',
            'term': term, 'relationship_verified': False, 'impact': None,
            'external_published_at': published.isoformat() if published else None,
            'materials': refs[:MAX_MATERIALS], 'total_materials': len(refs), 'boundary': boundary,
        }
