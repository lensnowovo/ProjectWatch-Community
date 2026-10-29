"""Bounded Europe PMC metadata queries; no publisher-access workaround."""
import re
from datetime import date
from urllib.parse import urlencode

from project_watch.studio.http import HttpError
from project_watch.studio.openalex_query import get_results
from project_watch.studio.sources import _item, _plain

BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest/'


def page(http, query, *, mode='topic', cursor=None, per_page=20, start=None, end=None):
    if (mode not in ('topic', 'affiliation', 'patent') or not isinstance(query, str)
            or not query.strip() or len(query) > 300 or any(ord(c) < 32 for c in query)
            or any(c in query for c in '\\"')):
        raise HttpError('invalid_collection_query')
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError('invalid paper page')
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048):
        raise ValueError('invalid paper cursor')
    # Literal phrase, not a model-authored Lucene expression. Scope terms retain
    # punctuation; new fields/operators cannot escape the enclosing quotation.
    term = ('AFFILIATION:' if mode == 'affiliation' else '') + '"' + query + '"'
    patent = mode == 'patent'
    term += ' AND SRC:PAT' if patent else ' AND (SRC:MED OR SRC:PMC)'
    if start or end:
        lower = date.fromisoformat(start).isoformat() if start else '*'
        upper = date.fromisoformat(end).isoformat() if end else '*'
        if start and end and lower > upper:
            raise ValueError('invalid paper window')
        term += f' AND FIRST_PDATE:[{lower} TO {upper}]'
    params = dict(query=term + ' sort_date:y', format='json', resultType='core',
                  pageSize=per_page, cursorMark=cursor or '*')
    data = get_results(http, BASE + 'search?' + urlencode(params))
    rows = ((data.get('resultList') or {}).get('result')) if isinstance(data, dict) else None
    count = data.get('hitCount') if isinstance(data, dict) else None
    if (not isinstance(rows, list) or type(count) is not int or count < 0
            or len(rows) > per_page or any(not isinstance(r, dict) for r in rows)):
        raise HttpError('invalid_collection_response')
    token = data.get('nextCursorMark')
    if token is not None and (not isinstance(token, str) or not token or len(token) > 2048):
        raise HttpError('invalid_collection_cursor')
    if not rows and cursor is None and count:
        raise HttpError('inconsistent_collection_page')
    more = bool(rows) and len(rows) == per_page and count > len(rows)
    if more and (not token or token == (cursor or '*')):
        raise HttpError('invalid_collection_cursor')
    items = []
    for row in rows:
        ident, origin, title = row.get('id'), row.get('source'), row.get('title')
        identity_pattern = r'[A-Z]{2}[A-Z0-9]+' if patent else r'(?:PMC)?[0-9]+'
        if (not isinstance(ident, str) or not re.fullmatch(identity_pattern, ident)
                or origin not in (('PAT',) if patent else ('MED', 'PMC')) or not isinstance(title, str) or not title.strip()):
            raise HttpError('invalid_collection_response')
        if patent and not isinstance(row.get('patentDetails'), dict):
            raise HttpError('invalid_collection_response')
        authors = (row.get('authorList') or {}).get('author') or []
        if not isinstance(authors, list) or any(not isinstance(a, dict) for a in authors):
            raise HttpError('invalid_collection_response')
        affiliations = list(dict.fromkeys(
            entry['affiliation'] for a in authors
            for entry in (a.get('authorAffiliationDetailsList') or {}).get('authorAffiliation') or []
            if isinstance(entry, dict) and isinstance(entry.get('affiliation'), str)))
        doi = row.get('doi') if isinstance(row.get('doi'), str) else ''
        extra_patent = {}
        if patent:
            details = row['patentDetails']
            extra_patent = dict(patent_number=ident, patent_kind=details.get('typeCode') or '',
                patent_country=details.get('countryCode') or '',
                provider_affiliation=row.get('affiliation') or '',
                legal_status='not_provided', family_status='not_provided',
                source_first_date=row.get('firstPublicationDate') or '')
        items.append(_item('patent' if patent else 'paper', _plain(title), f'https://europepmc.org/article/{origin}/{ident}',
            source='Europe PMC', snippet=_plain(row.get('abstractText') or ''),
            # PAT's firstPublicationDate can match application/priority dates.
            # Keep raw fields, but do not promote it into a news publication day.
            published_at=None if patent else row.get('firstPublicationDate'), extra={
                'source_identity': f'europepmc:{origin}:{ident}', 'source_record': row,
                'europepmc_id': f'{origin}:{ident}', 'pmcid': row.get('pmcid') or '', 'doi': doi,
                'authors': [a['fullName'] for a in authors if isinstance(a.get('fullName'), str)],
                'affiliations': affiliations, 'license': row.get('license') or '',
                'open_access': row.get('isOpenAccess') == 'Y',
                'paper_search': {'query': query, 'mode': mode},
                'date_basis': 'provider_first_date_unverified' if patent else 'firstPublicationDate', **extra_patent}))
    return dict(items=items, next_cursor=token if more else None, truncated=more,
                total_reported=count, date_basis='provider_first_date_unverified' if patent else 'firstPublicationDate', window_applied=True,
                coverage_kind='biomedical_patent_index' if patent else 'structured_pages', per_request_limit=per_page)


def open_body_url(snapshot):
    """Only saved provider OA metadata authorizes the fixed XML endpoint."""
    row = snapshot.get('source_record') or {}
    if (snapshot.get('source') != 'Europe PMC' or not isinstance(row, dict)
            or row.get('source') not in ('MED', 'PMC') or row.get('isOpenAccess') != 'Y'):
        return None
    pmcid = row.get('pmcid')
    if not isinstance(pmcid, str) or not re.fullmatch(r'PMC[0-9]+', pmcid):
        return None
    return BASE + pmcid + '/fullTextXML'
