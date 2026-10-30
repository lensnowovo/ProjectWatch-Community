"""One bounded third-party patent document, never an official legal register.

Missing/ambiguous structured fields fail closed. No title-to-product rules,
family traversal, PDF fetching, pagination, retries or inferred grant status.
"""
import hashlib
import re
from datetime import date
from html.parser import HTMLParser

from project_watch.studio.http import HttpError
from project_watch.studio.sources import _item

CONTRACT = 'google-patents-document-v1'
FIELDS = frozenset(('publicationNumber', 'applicationNumber', 'title', 'assigneeOriginal',
    'assigneeCurrent', 'inventor', 'priorityDate', 'filingDate', 'publicationDate', 'familyId'))


def publication_number(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z]{2}[0-9]{4,14}[A-Z][0-9]{0,2}', value):
        raise ValueError('exact patent publication number required')
    return value


class Fields(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.stack, self.rows, self.spans = [], [], []
        self.top = None  # start offset of the page's first top-level microdata item
        self.html = html
        self.lines = [0]
        self.lines.extend(m.end() for m in re.finditer('\n', html))

    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def capture(self, entry, end):
        if not entry['field']:
            return
        raw = entry['attribute_value'] or ''.join(entry['parts'])
        value = ' '.join(raw.split())
        if not value:
            return
        start = entry['start']
        fragment = self.html[start:end]
        if len(fragment) > 16000:
            raise HttpError('invalid_patent_field_span')
        self.rows.append((entry['field'], value))
        self.spans.append(dict(field=entry['field'], value=value, start=start, end=end,
            tag=entry['tag'], attribute=entry['attribute'], html=fragment))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        field = attrs.get('itemprop')
        start = self.position()
        # Microdata scope, as the live page is built: this document's fields are
        # properties of the first top-level item (the result article), wherever
        # they sit in its layout — the title is a bare span and most dates/owners
        # are dd elements outside any dl. "Other versions", events, citation rows,
        # family members and claims are nested items; their numbers, dates and
        # owners never become this document's fields.
        scopes = [e for e in self.stack if e['itemscope']]
        if 'itemscope' in attrs and not scopes and self.top is None:
            self.top = start
        own_item = len(scopes) == 1 and scopes[0]['start'] == self.top and 'itemscope' not in attrs
        blocked = any(e['blocked'] for e in self.stack) or tag in ('table', 'script', 'style', 'template')
        blocked |= any(word in (attrs.get('itemprop', '') + ' ' + attrs.get('id', '')).lower()
                       for word in ('citation', 'references', 'similar', 'familyt', 'description', 'claims'))
        selected = not blocked and field in FIELDS and own_item and tag in ('dd', 'span', 'time', 'meta')
        attribute = 'datetime' if tag == 'time' and attrs.get('datetime') else 'content' if tag == 'meta' else None
        entry = dict(tag=tag, field=field if selected else None, parts=[], start=start, itemscope='itemscope' in attrs,
                     attribute=attribute, attribute_value=attrs.get(attribute) if attribute else None, blocked=blocked)
        if tag == 'meta':
            self.capture(entry, self.position() + len(self.get_starttag_text()))
            return
        if tag in ('br', 'hr', 'img', 'input', 'link', 'wbr', 'source'):
            return
        self.stack.append(entry)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1]['start'] == self.position():
            self.capture(self.stack.pop(), self.position() + len(self.get_starttag_text()))

    def handle_data(self, data):
        if any(e['blocked'] for e in self.stack):
            return
        for entry in self.stack:
            if entry['field']:
                entry['parts'].append(data)

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i]['tag'] == tag), None)
        if index is None:
            return
        end = self.html.find('>', self.position()) + 1
        for entry in self.stack[index:]:
            self.capture(entry, end)
        del self.stack[index:]


def parse(html, number):
    publication_number(number)
    if not isinstance(html, str) or len(html.encode()) > 8 * 1024 * 1024:
        raise HttpError('invalid_patent_document_size')
    parser = Fields(html)
    parser.feed(html)
    values = {}
    for key, raw in parser.rows:
        value = ' '.join(raw.split())
        if value and value not in values.setdefault(key, []):
            values[key].append(value)
    if sum(len(v) for v in values.values()) > 120 or any(len(s) > 2000 for v in values.values() for s in v):
        raise HttpError('invalid_patent_document_fields')

    def one(key, required=False):
        rows = values.get(key, [])
        if len(rows) > 1 or (required and not rows):
            raise HttpError('invalid_patent_document_' + key)
        return rows[0] if rows else None

    if one('publicationNumber', True) != number:
        raise HttpError('invalid_patent_document_identity')
    title, applicants = one('title', True), values.get('assigneeOriginal', [])
    if not applicants or len(applicants) > 20:
        raise HttpError('invalid_patent_original_applicant')
    dates = {}
    for key in ('publicationDate', 'filingDate', 'priorityDate'):
        value = one(key)
        try:
            if value and date.fromisoformat(value).isoformat() != value:
                raise ValueError('date')
        except ValueError as exc:
            raise HttpError('invalid_patent_document_date') from exc
        dates[key] = value
    family = one('familyId')
    if family and not re.fullmatch(r'[0-9]{1,30}', family):
        raise HttpError('invalid_patent_family')
    return dict(contract=CONTRACT, provider='Google Patents', provider_kind='third_party_index',
        publication_number=number, title=title, applicants=applicants,
        current_assignees=values.get('assigneeCurrent', []), inventors=values.get('inventor', []),
        application_number=one('applicationNumber'), document_kind=re.search(r'[A-Z][0-9]*$', number)[0],
        family_id='google-patents:' + family if family else None,
        publication_date=dates['publicationDate'], filing_date=dates['filingDate'], priority_date=dates['priorityDate'],
        legal_status='unknown', date_basis='publication' if dates['publicationDate'] else 'unknown',
        field_evidence=values, field_spans=parser.spans, span_encoding='unicode_codepoints',
        decoded_html_sha256=hashlib.sha256(html.encode()).hexdigest())


def metadata(record):
    """Validate a stored parser result without consulting current remote state."""
    if not isinstance(record, dict) or record.get('contract') != CONTRACT:
        raise ValueError('invalid patent document contract')
    publication_number(record['publication_number'])
    fields = record['field_evidence']
    if fields.get('publicationNumber') != [record['publication_number']] or fields.get('title') != [record['title']]:
        raise ValueError('patent identity lost its source fields')
    if not record['applicants'] or record['applicants'] != fields.get('assigneeOriginal'):
        raise ValueError('original applicant is required')
    if record['legal_status'] != 'unknown' or record['provider_kind'] != 'third_party_index':
        raise ValueError('index cannot assert legal validity')
    for key, field in (('current_assignees', 'assigneeCurrent'), ('inventors', 'inventor')):
        if record[key] != fields.get(field, []):
            raise ValueError('patent fields changed')
    for key, field in (('publication_date', 'publicationDate'), ('filing_date', 'filingDate'),
                       ('priority_date', 'priorityDate'), ('application_number', 'applicationNumber')):
        if record[key] != (fields.get(field) or [None])[0]:
            raise ValueError('patent fields changed')
    if record['family_id'] != ('google-patents:' + fields['familyId'][0] if fields.get('familyId') else None):
        raise ValueError('patent family changed')
    for field, values in fields.items():
        if not all(any(s['field'] == field and s['value'] == v and s['end'] - s['start'] == len(s['html'])
                       for s in record['field_spans']) for v in values):
            raise ValueError('patent field provenance missing')
    return record


def page(http, query):
    number = publication_number(query)
    url = 'https://patents.google.com/patent/' + number + '/en'
    response = http.get(url, headers={'Accept': 'text/html'}, timeout=20)
    if response.status != 200:
        raise HttpError('http_patent_document', status=response.status)
    record = parse(response.text(), number)
    record['raw_html_sha256'] = hashlib.sha256(response.body).hexdigest()
    item = _item('patent', record['title'], url, source='Google Patents',
        snippet=' · '.join([number, *record['applicants']]), published_at=record['publication_date'],
        extra=dict(source_identity='google-patents:' + number, source_record=record,
            patent_number=number, patent_kind=record['document_kind'], applicants=record['applicants'],
            current_assignees=record['current_assignees'], inventors=record['inventors'],
            family_id=record['family_id'], legal_status='unknown', provider_kind='third_party_index',
            application_number=record['application_number'], filing_date=record['filing_date'],
            priority_date=record['priority_date'], date_basis=record['date_basis']))
    return dict(items=[item], next_cursor=None, truncated=False, window_applied=False,
                date_basis=record['date_basis'], coverage_kind='single_patent_document', per_request_limit=1)
