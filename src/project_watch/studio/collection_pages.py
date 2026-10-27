"""Bounded structured-source pages, retaining source dates and continuation."""
from datetime import date
from urllib.parse import quote, urlencode

from project_watch.studio.http import HttpError
from project_watch.studio.sources import _item, _openalex_abstract, _plain


def _rows(data, key):
    rows = data.get(key) if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HttpError('invalid_collection_response')
    return rows


def openalex_page(http, query, *, mode='topic', page=1, per_page=20, start=None, end=None):
    """Basic pagination is bounded at 10,000, avoiding unconfigured cursor keys."""
    if mode not in ('topic', 'author', 'affiliation') or not isinstance(query, str) or not query.strip():
        raise ValueError('invalid paper scope')
    if type(page) is not int or type(per_page) is not int or page < 1 or not 1 <= per_page <= 100 or page * per_page > 10000:
        raise ValueError('invalid paper page')
    params = {'page': page, 'per-page': per_page, 'sort': 'publication_date:desc',
              'select': 'id,doi,display_name,publication_date,primary_location,authorships,cited_by_count,abstract_inverted_index'}
    filters = []
    if mode == 'topic':
        params['search'] = query
    else:
        # These are API filter operators; reject delimiter injection into scope.
        if any(char in query for char in ',|!'):
            raise ValueError('paper scope requires resolved entity or unambiguous name')
        filters.append(('raw_affiliation_strings.search:' if mode == 'affiliation' else 'raw_author_name.search:') + query)
    for key, value in (('from_publication_date', start), ('to_publication_date', end)):
        if value:
            filters.append(key + ':' + date.fromisoformat(value).isoformat())
    if start and end and start > end:
        raise ValueError('invalid paper window')
    if filters:
        params['filter'] = ','.join(filters)
    data = http.get_json('https://api.openalex.org/works?' + urlencode(params))
    rows = _rows(data, 'results')
    count = (data.get('meta') or {}).get('count')
    if type(count) is not int or count < 0:
        raise HttpError('invalid_collection_response')
    if len(rows) > per_page or (not rows and (page - 1) * per_page < count):
        raise HttpError('inconsistent_collection_page')
    items = []
    for work in rows:
        if not work.get('id') or not work.get('display_name'):
            raise HttpError('invalid_collection_response')
        location = work.get('primary_location') or {}
        authorships = [a for a in (work.get('authorships') or []) if isinstance(a, dict)]
        authors = [a.get('author', {}).get('display_name', '') for a in authorships]
        affiliations = list(dict.fromkeys(s for a in authorships for s in a.get('raw_affiliation_strings') or [] if isinstance(s, str)))
        institutions = sorted({i['display_name'] for a in authorships for i in a.get('institutions') or [] if i.get('display_name')})
        abstract = _openalex_abstract(work.get('abstract_inverted_index'))
        items.append(_item('paper', work['display_name'], work.get('doi') or location.get('landing_page_url') or work['id'],
                           source=(location.get('source') or {}).get('display_name') or 'OpenAlex',
                           snippet=abstract['text'], published_at=work.get('publication_date'),
                           extra={'openalex_id': work['id'], 'doi': work.get('doi'), 'authors': authors,
                                  'affiliations': affiliations, 'institutions': institutions,
                                  'paper_abstract': abstract, 'cited_by': work.get('cited_by_count'),
                                  'paper_search': {'query': query, 'mode': mode},
                                  'source_record': work, 'date_basis': 'publication_date'}))
    more = page * per_page < count
    return {'items': items, 'next_cursor': page + 1 if more and (page + 1) * per_page <= 10000 else None,
            'truncated': more, 'total_reported': count, 'date_basis': 'publication_date',
            'window_applied': True, 'coverage_kind': 'structured_pages', 'per_request_limit': per_page}


def clinical_page(http, query, *, mode='sponsor', cursor=None, per_page=20):
    """Registered studies; last update is distinct from initial publication."""
    if mode not in ('sponsor', 'term') or not isinstance(query, str) or not query.strip():
        raise ValueError('invalid clinical scope')
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError('invalid clinical page')
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValueError('invalid clinical cursor')
    params = {'pageSize': per_page, 'sort': 'LastUpdatePostDate:desc', 'format': 'json',
              'query.spons' if mode == 'sponsor' else 'query.term': query}
    if cursor:
        params['pageToken'] = cursor
    data = http.get_json('https://clinicaltrials.gov/api/v2/studies?' + urlencode(params))
    rows = _rows(data, 'studies')
    next_cursor = data.get('nextPageToken')
    if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor):
        raise HttpError('invalid_collection_cursor')
    if len(rows) > per_page or (next_cursor and not rows):
        raise HttpError('inconsistent_collection_page')
    items = []
    for study in rows:
        proto = study.get('protocolSection') or {}
        ident, status = proto.get('identificationModule') or {}, proto.get('statusModule') or {}
        nct, title = ident.get('nctId'), ident.get('briefTitle')
        if not isinstance(nct, str) or not nct or not isinstance(title, str) or not title:
            raise HttpError('invalid_collection_response')
        sponsorship = proto.get('sponsorCollaboratorsModule') or {}
        sponsor = (sponsorship.get('leadSponsor') or {}).get('name', '')
        collaborators = [c['name'] for c in sponsorship.get('collaborators') or [] if c.get('name')]
        conditions = (proto.get('conditionsModule') or {}).get('conditions') or []
        phases = '/'.join((proto.get('designModule') or {}).get('phases') or []) or 'N/A'
        updated = (status.get('lastUpdatePostDateStruct') or {}).get('date')
        published = (status.get('studyFirstPostDateStruct') or {}).get('date')
        items.append(_item('trial', title, 'https://clinicaltrials.gov/study/' + quote(nct),
                           source='ClinicalTrials.gov', published_at=published,
                           snippet=f"{nct} · {phases} · {status.get('overallStatus', '')} · {sponsor}",
                           extra={'nct_id': nct, 'phase': phases, 'status': status.get('overallStatus'),
                                  'sponsor': sponsor, 'collaborators': collaborators, 'conditions': conditions,
                                  'source_updated_at': updated, 'source_published_at': published,
                                  'date_basis': 'last_update_post_date', 'source_record': study,
                                  'clinical_search': {'query': query, 'mode': mode}}))
    return {'items': items, 'next_cursor': next_cursor, 'truncated': next_cursor is not None,
            'date_basis': 'last_update_post_date', 'window_applied': False,
            'coverage_kind': 'structured_pages', 'per_request_limit': per_page}


def cninfo_page(http, query, *, page=1, per_page=20, start=None, end=None):
    """Existing announcement endpoint; missing totals are explicitly incomplete."""
    if not isinstance(query, str) or not query.strip() or type(page) is not int or page < 1:
        raise ValueError('invalid announcement scope')
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError('invalid announcement page')
    for value in (start, end):
        if value:
            date.fromisoformat(value)
    if start and end and start > end:
        raise ValueError('invalid announcement window')
    url = 'https://www.cninfo.com.cn/new/fulltextSearch/full?' + urlencode({
        'searchkey': query, 'sdate': start or '', 'edate': end or '', 'isfulltext': 'true',
        'sortName': 'pubdate', 'sortType': 'desc', 'pageNum': page, 'pageSize': per_page})
    data = http.get_json(url, headers={'Accept': 'application/json',
                                     'Referer': 'https://www.cninfo.com.cn/new/fulltextSearch'})
    # This endpoint encodes an explicit zero-result response with null rows.
    # Missing/ambiguous/contradictory metadata still fails validation; null by
    # itself never proves that the query succeeded with no results.
    if (isinstance(data, dict) and 'announcements' in data and data['announcements'] is None
            and type(data.get('totalAnnouncement')) is int and data['totalAnnouncement'] == 0
            and data.get('hasMore') is False
            and all(type(data.get(key)) is int and data[key] == 0
                    for key in ('totalRecordNum', 'totalpages') if key in data)):
        data = {**data, 'announcements': []}
    rows = _rows(data, 'announcements')
    total = data.get('totalAnnouncement')
    if total is not None and (type(total) is not int or total < 0):
        raise HttpError('invalid_collection_response')
    if len(rows) > per_page or (total is not None and not rows and (page - 1) * per_page < total):
        raise HttpError('inconsistent_collection_page')
    items = []
    for row in rows:
        ident, attachment = row.get('announcementId'), row.get('adjunctUrl')
        if ident is not None and (not isinstance(ident, (str, int)) or isinstance(ident, bool)):
            raise HttpError('invalid_collection_response')
        if not isinstance(row.get('announcementTitle'), str) or not row['announcementTitle'].strip():
            raise HttpError('invalid_collection_response')
        if attachment and (not isinstance(attachment, str) or ':' in attachment or '..' in attachment.split('/')):
            raise HttpError('invalid_collection_response')
        if not ident and not attachment:
            raise HttpError('invalid_collection_response')
        # Metadata-only announcements remain source records. Do not silently
        # drop them or claim that their PDF/full text was downloaded.
        source_url = ('https://static.cninfo.com.cn/' + attachment.lstrip('/')) if attachment else (
            url + '#' + quote(str(ident), safe=''))
        issuer, title = _plain(row.get('secName')), _plain(row['announcementTitle'])
        title = f'{issuer}：{title}' if issuer and issuer not in title else title
        items.append(_item('filing', title, source_url, source='巨潮资讯',
                           snippet=_plain(row.get('announcementContent')), published_at=row.get('announcementTime'),
                           extra={'announcement_id': str(ident) if ident is not None else '',
                                  'sec_code': row.get('secCode') or '', 'sec_name': issuer,
                                  'filing_target': query, 'source_record': row,
                                  'attachment_available': bool(attachment), 'date_basis': 'announcementTime',
                                  'source_identity': 'cninfo:' + str(ident or source_url)}))
    more = total is None or page * per_page < total
    return dict(items=items, next_cursor=page + 1 if more and total is not None else None,
                truncated=more, total_reported=total, date_basis='announcementTime',
                window_applied=False, coverage_kind='structured_pages', per_request_limit=per_page)
