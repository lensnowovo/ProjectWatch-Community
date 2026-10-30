"""Bibliographic facts from immutable provider records, never company ownership.

No transport or scheduler. Search terms and provider query echoes are deliberately
excluded. A deterministic proposal saves extraction only, not semantic admission.
"""
import json
import re
from contextlib import closing
from datetime import date

KINDS = ('paper', 'patent')
RELATIONS = ('author_affiliation', 'applicant', 'technical_relevance', 'unverified')
GUIDANCE = ('paper/patent 为独立研究事实，不是公司经营事件。research_relation 必填：'
    'author_affiliation 仅论文作者机构，applicant 仅明确专利申请人，technical_relevance 仅技术相关，'
    'unverified 为关联待核。subject 是有证据关联的主体，reason 说明关系依据。'
    '不得把检索词、同页提及、作者姓名或发明人当成公司归属/申请人。'
    '论文发表不等于产品验证；专利申请、公开不等于授权或有效权利。'
    '专利日期只在原文明示对应事件时填写，不能用索引日期或优先权日代替。')


def _strings(values):
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError('research_metadata_limit')
    if any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in values):
        raise ValueError('research_metadata_invalid')
    return list(dict.fromkeys(values))


def metadata(snapshot):
    """Read only known provider fields; never search arbitrary JSON for a name."""
    row = snapshot.get('source_record')
    if not isinstance(row, dict):
        return None
    authors, affiliations, applicants = [], [], []
    ident, title, publication_date, legal_status = '', '', '', 'not_provided'
    if re.fullmatch(r'https://openalex.org/W\d+', str(row.get('id', ''))):
        ident, title = row['id'], row.get('display_name')
        publication_date = row.get('publication_date') or ''
        for author in row.get('authorships') or []:
            name = (author.get('author') or {}).get('display_name')
            if name:
                authors.append(name)
            affiliations.extend(author.get('raw_affiliation_strings') or [])
            affiliations.extend(i['display_name'] for i in author.get('institutions') or [] if i.get('display_name'))
        kind = 'paper'
    elif snapshot.get('source') == 'Europe PMC' and row.get('source') in ('MED', 'PMC', 'PAT'):
        patent = row['source'] == 'PAT'
        if not re.fullmatch(r'[A-Z]{2}[A-Z0-9]+' if patent else r'(?:PMC)?\d+', str(row.get('id', ''))):
            return None
        ident, title = 'europepmc:' + row['source'] + ':' + row['id'], row.get('title')
        kind = 'patent' if patent else 'paper'
        if patent:
            # Europe PMC does not supply a verified applicant/legal-status contract.
            # Do not rename its author/affiliation fields into patent ownership.
            if not isinstance(row.get('patentDetails'), dict):
                return None
        else:
            publication_date = row.get('firstPublicationDate') or ''
            for author in (row.get('authorList') or {}).get('author') or []:
                if author.get('fullName'):
                    authors.append(author['fullName'])
                affiliations.extend(a['affiliation'] for a in
                    (author.get('authorAffiliationDetailsList') or {}).get('authorAffiliation') or []
                    if a.get('affiliation'))
    elif row.get('contract') == 'google-patents-document-v1':
        from project_watch.studio.google_patents import metadata as patent_metadata

        parsed = patent_metadata(row)
        ident, title = parsed['publication_number'], parsed['title']
        kind, applicants = 'patent', parsed['applicants']
        legal_status, publication_date = 'unknown', parsed['publication_date'] or ''
    elif row.get('contract') == 'patent-registry-record-v1':
        from project_watch.studio.patent_registry import PatentRecord

        parsed = PatentRecord.model_validate(row)
        ident, title = parsed.publication_number, parsed.title
        kind, applicants = 'patent', parsed.applicants
        legal_status = parsed.status
        publication_date = parsed.event_date or ''
    else:
        return None
    if not isinstance(title, str) or not title.strip() or len(title) > 2000:
        return None
    try:
        publication_date = date.fromisoformat(publication_date).isoformat() if publication_date else ''
    except (TypeError, ValueError):
        publication_date = ''
    result = dict(kind=kind, identifier=ident, title=title, authors=_strings(authors),
                  affiliations=_strings(affiliations), applicants=_strings(applicants),
                  legal_status=legal_status, publication_date=publication_date)
    if kind == 'patent':
        result['date_basis'] = row.get('date_basis', 'unknown')
        result['inventors'] = row.get('inventors', []) if row.get('contract') == 'patent-registry-record-v1' else []
        if row.get('contract') == 'google-patents-document-v1':
            result.update({key: row[key] for key in ('inventors', 'current_assignees', 'document_kind',
                'family_id', 'application_number', 'filing_date', 'priority_date', 'provider_kind')})
    if snapshot.get('query_context'):
        result['query_context'] = snapshot['query_context']
    # Keep the whole metadata or decline; no silent shortening of asserted facts.
    if len(json.dumps(result, ensure_ascii=False)) > 3000:
        return None
    return result


def evidence_metadata(store, project_id, evidence_id):
    version = store.evidence.get(project_id, evidence_id, include_internal=False)
    if version['source_type'] != 'structured_record':
        return None
    try:
        snapshot = json.loads(''.join(p['text'] for p in version['parts']))
        value = metadata(snapshot)
        if value and value.get('query_context'):
            from project_watch.studio.retrieval_queries import validate

            validate(store, project_id, value['query_context'])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if value:
        value['evidence_version_id'] = version['id']
    return value


def attach(store, project_id, source, kind, relation, reason):
    value = evidence_metadata(store, project_id, source['evidence_version_id'])
    if not value or value['kind'] != kind or relation not in RELATIONS:
        raise ValueError('research_record_requires_structured_identity')
    if relation == 'author_affiliation' and (kind != 'paper' or not value['affiliations']):
        raise ValueError('research_author_affiliation_missing')
    if relation == 'applicant' and (kind != 'patent' or not value['applicants']):
        raise ValueError('research_applicant_missing')
    if relation != 'unverified' and not reason:
        raise ValueError('research_relation_reason_required')
    return value


def proposal(execution, task, source):
    """Pristine, attributable bibliography can skip extraction, never review.

    No replacement of any prior attempt, call, deferred input or settled result.
    A name match is only a proposal and is rechecked by the independent reviewer.
    """
    with closing(execution.store._connect()) as db:
        execution._guard(db)
        if db.execute('SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage IN (?,?,?,?)',
                (execution.job['id'], 'extract_attempt:' + task['id'], 'extract_result:' + task['id'],
                 'extract_deferred:' + task['id'], 'material_deferred:' + task['id'])).fetchone():
            return None
        if db.execute('SELECT 1 FROM studio_material_calls WHERE task_id=?', (task['id'],)).fetchone():
            return None
    return bibliographic_proposal(execution.store, task, source)


def bibliographic_proposal(store, task, source):
    """Pure proposal builder; callers own admission and original-task guards."""
    value = evidence_metadata(store, task['project_id'], task['evidence_version_id'])
    if not value or value['kind'] != 'paper' or not value['authors'] or len(value['title']) > 200:
        return None
    company = store.get_project(task['project_id'])['legal_name']
    # Use only the saved confirmed name. Alias discovery is separate, evidenced work.
    matches = [a for a in value['affiliations'] if re.search(
        r'(?<!\w)' + re.escape(company) + r'(?!\w)', a, re.IGNORECASE)] if company.strip() else []
    if not matches or len(matches[0]) > 600:
        return None  # A search hit without attribution still needs real extraction/review.
    literal = json.dumps(value['title'], ensure_ascii=False)[1:-1]
    for key, ref in source['refs'].items():
        start = ref['quote'].find(literal)
        if start >= 0 and len(literal) >= 6:
            return {'observations': [dict(kind='paper', subject=company, label=value['title'], text=value['title'],
                event_at=value['publication_date'] or None, refs=[key], start=start, end=start + len(literal),
                research_relation='author_affiliation', reason=matches[0])]}
    return None


def extraction_contract(execution, source):
    """Freeze the additional prompt for untouched tasks; preserve old wires."""
    task_id = source['task_id']
    stage = 'research_contract_v1:' + task_id
    saved = execution.read_checkpoint(stage)
    if saved is not None:
        if saved.get('evidence_version_id') != source['evidence_version_id']:
            raise ValueError('research_contract_input_changed')
        return saved
    with closing(execution.store._connect()) as db:
        execution._guard(db)
        if db.execute('SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage IN (?,?,?)',
                (execution.job['id'], 'extract_attempt:' + task_id, 'extract_result:' + task_id,
                 'extract_deferred:' + task_id)).fetchone() or db.execute(
                'SELECT 1 FROM studio_material_calls WHERE task_id=?', (task_id,)).fetchone():
            return None
    data = evidence_metadata(execution.store, execution.job['project_id'], source['evidence_version_id'])
    if data:
        value = {'version': 'bibliography-v1', 'evidence_version_id': source['evidence_version_id'], 'guidance': GUIDANCE}
        execution.checkpoint(stage, value)
        return value
    return None
