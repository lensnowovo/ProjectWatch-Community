"""Saved professional sources, not admitted events or verified company assets.

This projection never runs research. Collection snapshots can be read before
triage; legacy public-source items remain available. Exclusions and current
evidence visibility/lifecycle apply before paging or counting.
"""
import hashlib
import json
from contextlib import closing
from urllib.parse import urlsplit

CATEGORIES = ('paper', 'patent', 'trial')
TEXT_FIELDS = ('openalex_id', 'doi', 'nct_id', 'phase', 'status', 'sponsor',
               'europepmc_id', 'pmcid', 'license',
               'patent_number', 'patent_kind', 'patent_country', 'provider_affiliation',
               'legal_status', 'family_status', 'source_first_date', 'date_basis',
               'assignee_query', 'applicant_quote', 'applicant_basis', 'family_id', 'application_number',
               'filing_date', 'priority_date', 'provider_kind', 'research_relation')
LIST_FIELDS = ('authors', 'institutions', 'affiliations', 'collaborators', 'conditions', 'applicants', 'current_assignees', 'inventors',
               'other_versions')


class SourceRevisionChanged(ValueError):
    pass


def _extra(raw):
    try:
        value = json.loads(raw or '{}')
    except (ValueError, TypeError):
        value = {}
    if not isinstance(value, dict):
        return {}
    # Never return arbitrary source_record, extraction diagnostics or model fields.
    out = {k: value[k][:1000] for k in TEXT_FIELDS if isinstance(value.get(k), str)}
    out.update({k: [v[:500] for v in value[k][:100] if isinstance(v, str)]
                for k in LIST_FIELDS if isinstance(value.get(k), list)})
    if type(value.get('cited_by')) is int and value['cited_by'] >= 0:
        out['cited_by'] = value['cited_by']
    search = value.get('paper_search')
    if isinstance(search, dict):
        out['paper_search'] = {k: search[k][:200] for k in ('query', 'mode') if isinstance(search.get(k), str)}
    context = value.get('query_context')
    if isinstance(context, dict):
        out['query_relationship'] = {k: context[k][:300] for k in ('query', 'subject', 'relationship', 'basis')
                                     if isinstance(context.get(k), str)}
    return out


def _url(value):
    try:
        url = urlsplit(value)
        return value if url.scheme in ('https', 'http') and url.hostname and not url.username and not url.password else ''
    except ValueError:
        return ''


def _rows(db, project_id):
    has_collection = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_collection_sources'").fetchone()
    snapshots = ("SELECT s.*,ROW_NUMBER() OVER(PARTITION BY s.item_id ORDER BY s.observed_at DESC,s.identity) AS rank "
                 "FROM studio_collection_sources s" if has_collection else
                 "SELECT NULL AS item_id,NULL AS project_id,NULL AS evidence_version_id,NULL AS observed_at,1 AS rank WHERE 0")
    rows = db.execute(f"""WITH snapshots AS ({snapshots})
        SELECT i.id,i.project_id,i.category,i.title,i.url,i.source,i.snippet,i.published_at,i.collected_at,i.extra,
               i.triaged,i.status,s.evidence_version_id,s.observed_at,
               p.read_status,p.basis,b.id AS body_evidence_version_id,
               CASE WHEN p.basis='own_text' AND p.read_status IN
                    ('article_body','partial_body','filing_text','search_summary','paper_abstract')
                    AND b.id IS NOT NULL THEN b.record ELSE e.record END AS quality_record
        FROM studio_items i
        LEFT JOIN snapshots s ON s.item_id=i.id AND s.rank=1
        LEFT JOIN studio_evidence_versions e ON e.id=s.evidence_version_id AND e.project_id=i.project_id
        LEFT JOIN studio_item_evidence p ON p.item_id=i.id
        LEFT JOIN studio_evidence_versions b ON b.id=p.evidence_version_id AND b.project_id=i.project_id
        WHERE i.project_id=? AND i.category IN ('paper','patent','trial')
          AND i.status!='dismissed' AND i.relevant=1
          AND ((s.item_id IS NULL AND i.key NOT LIKE 'collection:%') OR
               (s.project_id=i.project_id AND e.visibility='public'))
          AND (p.item_id IS NULL OR (p.project_id=i.project_id AND
               (p.evidence_version_id IS NULL OR b.visibility='public')))
          AND NOT EXISTS (SELECT 1 FROM studio_evidence_lifecycle l
              WHERE l.project_id=i.project_id AND l.status='withdrawn'
                AND l.evidence_version_id IN (e.id,b.id))
          AND NOT EXISTS (SELECT 1 FROM studio_evidence_heads h
              LEFT JOIN studio_evidence_versions v ON v.id=h.evidence_version_id AND v.project_id=h.project_id
              WHERE h.project_id=i.project_id AND h.source_key IN (e.source_key,b.source_key,'item:'||i.id)
                AND (v.id IS NULL OR v.visibility!='public' OR EXISTS (
                    SELECT 1 FROM studio_evidence_lifecycle l WHERE l.project_id=i.project_id
                    AND l.evidence_version_id=v.id AND l.status='withdrawn')))
        ORDER BY COALESCE(s.observed_at,i.collected_at) DESC,i.id
        """, (project_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['extra'] = _extra(item['extra'])
        item['url'] = _url(item['url'])
        item['processing_status'] = 'organized' if item.pop('triaged') else 'pending'
        quality = json.loads(item.pop('quality_record') or '{}')
        item['source_type'] = quality.get('source_type')
        item['read_scope'] = quality.get('read_scope')
        # No model summary, impact, importance or inferred company ownership.
        result.append(item)
    return result


def _patent_groups(items):
    """Display grouping only; nothing is merged, deleted or fetched.

    A provider family ID wins. Otherwise collected documents linked by a
    document's own listed other versions (same application, e.g. A1 and B2)
    form one group. Anything else stays an independent document.
    """
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    numbers = {}
    for item in items:
        number = item['extra'].get('patent_number')
        if not item['extra'].get('family_id') and number:
            numbers.setdefault(number, []).append(item['id'])
    for ids in numbers.values():
        for item_id in ids[1:]:
            parent[find(item_id)] = find(ids[0])
    for item in items:
        if item['extra'].get('family_id') or not item['extra'].get('patent_number'):
            continue
        for other in item['extra'].get('other_versions') or []:
            if other in numbers:
                parent[find(item['id'])] = find(numbers[other][0])
    keys = {}
    for item in items:
        family = item['extra'].get('family_id')
        if family:
            keys[item['id']] = (family, 'family_id')
            continue
        root = find(item['id'])
        members = sorted(n for n, ids in numbers.items() if find(ids[0]) == root)
        keys[item['id']] = (('versions:' + members[0], 'listed_versions') if len(members) > 1
                            else ('document:' + item['id'], 'document'))
    return keys


def research_sources(store, project_id, category, *, limit=40, offset=0, revision=None):
    if category not in CATEGORIES or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('invalid research source page')
    with closing(store._connect()) as db:
        db.execute('BEGIN')
        items = [r for r in _rows(db, project_id) if r['category'] == category]
        groups = {}
        if category == 'patent':
            keys = _patent_groups(items)
            for item in items:
                groups.setdefault(keys[item['id']][0], []).append(item['id'])
            for item in items:
                key, basis = keys[item['id']]
                item['family'] = dict(key=key, basis=basis, document_count=len(groups[key]), document_ids=groups[key])
        digest = hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if revision is not None and revision != digest:
            raise SourceRevisionChanged('research_sources_changed')
        return dict(project_id=project_id, category=category, revision=digest, total=len(items), offset=offset,
                    family_count=len(groups) if category == 'patent' else None,
                    next_offset=offset + limit if offset + limit < len(items) else None,
                    items=items[offset:offset + limit])


def research_source_counts(store, project_id):
    with closing(store._connect()) as db:
        db.execute('BEGIN')
        rows = _rows(db, project_id)
        return {category: sum(r['category'] == category for r in rows) for category in CATEGORIES}
