"""Bounded original-text quantity projection. No model summaries, estimates or ranks.

This is deliberately not a general financial or clinical metric extractor.
An absent dimension is not a shared scope; divergent source values stay separate.
"""

import json
import re
from collections import defaultdict
from contextlib import closing
from datetime import date
from decimal import Decimal

from project_watch.studio.events import scope_dimensions
from project_watch.studio.public_profile import safe_url

_NUMBER = r'(\d+(?:,\d{3})*(?:\.\d+)?)\s*(千万|百万|万|亿)?\s*(台|颗|件|套)'
_METRIC = re.compile(r'(出货量|交付量|销量|销售量)\s*(?:为|达到|达|[:：])?\s*' + _NUMBER)
_QUANTITY = re.compile(_NUMBER)
_SCALE = {'': 1, '万': 10000, '百万': 1000000, '千万': 10000000, '亿': 100000000}
_DIMENSIONS = ('period', 'metric', 'product', 'segment', 'region', 'indication', 'regulatory')
_LABEL = {'出货量': '出货量', '交付量': '交付量', '销量': '销量'}


def with_originals(store, project_id: str, events: list[dict]) -> list[dict]:
    """Internal enrichment for projection only; never export whole article bodies.

    Only current public original versions linked to same-project source items
    qualify. All database reads share one snapshot. A 2M-character projection
    budget and 20 parts/source are explicit partial-reading limits, not coverage.
    """
    result = [{**e, 'sources': [{**s} for s in e.get('sources') or []]} for e in events]
    sources = [s for e in result for s in e['sources']]
    by_id = defaultdict(list)
    for source in sources:
        # Ignore caller-supplied passages: the store is the authority here.
        source.pop('original_parts', None)
        source.pop('original_read_scope', None)
        if source.get('id'):
            by_id[source['id']].append(source)
    ids, remaining = list(by_id), 2_000_000
    with closing(store._connect()) as db:
        db.execute('BEGIN')
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            placeholders = ','.join('?' for _ in chunk)
            rows = db.execute(f'''SELECT i.id, i.body_status, v.record FROM studio_items i
                JOIN studio_item_evidence p ON p.item_id=i.id AND p.project_id=i.project_id
                JOIN studio_evidence_versions v ON v.id=p.evidence_version_id AND v.project_id=i.project_id
                JOIN studio_evidence_heads h ON h.evidence_version_id=v.id AND h.project_id=v.project_id
                    AND h.source_key=v.source_key
                LEFT JOIN studio_evidence_lifecycle l ON l.evidence_version_id=v.id AND l.project_id=v.project_id
                WHERE i.project_id=? AND i.id IN ({placeholders}) AND v.visibility='public'
                    AND COALESCE(l.status,'current')='current'
                    AND i.body_status IN ('article_body','partial_body','filing_text','twin_body')''',
                (project_id, *chunk))
            for row in rows:
                record = json.loads(row['record'])
                if record['read_scope'] == 'summary' or record['source_type'] != 'public_page':
                    continue
                parts, total = [], sum(len(p['text']) for p in record['parts'])
                for index, part in enumerate(record['parts'][:20]):
                    text = part['text'][:remaining]
                    if not text:
                        break
                    remaining -= len(text)
                    parts.append({'evidence_version_id': record['id'], 'part_index': index, 'text': text})
                for source in by_id[row['id']]:
                    source['body_status'] = row['body_status']
                    source['original_read_scope'] = record['read_scope']
                    source['original_parts'] = parts
                    source['original_truncated'] = sum(len(p['text']) for p in parts) < total
    return result


def _passages(source):
    """Original headline plus self-contained sentences with exact part offsets."""
    yield source.get('title') or '', None
    if (source.get('body_status') not in ('article_body', 'partial_body', 'filing_text')
            and (source.get('body_status') != 'twin_body' or source.get('original_read_scope') not in ('partial', 'full'))):
        return
    for part in source.get('original_parts') or []:
        text = part['text']
        for match in re.finditer(r'[^\n。！？!?]+(?:[。！？!?]|$)', text):
            quote = match.group(0).strip()
            if not quote or len(quote) > 6000:
                continue
            start = match.start() + len(match.group(0)) - len(match.group(0).lstrip())
            yield quote, {'evidence_version_id': part['evidence_version_id'], 'part_index': part['part_index'],
                          'start': start, 'end': start + len(quote), 'quote': quote}


def _closed_period(period: str, day: str) -> bool:
    """A source published mid-period cannot establish a completed-period quantity."""
    match = re.fullmatch(r'(20\d{2})(Q[1-4]|H[12]|Y)', period)
    if not match:
        return False
    year, suffix = int(match[1]), match[2]
    month = int(suffix[1]) * 3 if suffix.startswith('Q') else int(suffix[1]) * 6 if suffix.startswith('H') else 12
    after = date(year + (month == 12), month % 12 + 1, 1)
    return date.fromisoformat(day) >= after


def quantities(companies: list[dict], events: list[dict], *, today: date) -> dict:
    from project_watch.studio.peer_names import headline_leads as _leads

    records, seen = [], set()
    for event in events:
        # Prefer original source headlines; the event summary/impact can be model-written.
        for source in event.get('sources') or [event]:
            title, url = source.get('title') or '', source.get('url') or ''
            if not safe_url(url) or not isinstance(title, str):
                continue
            try:
                day = date.fromisoformat(str(source.get('published_at') or event.get('event_at') or '')[:10])
            except ValueError:
                continue
            if day > today:
                continue
            for quote, ref in _passages(source):
                owners = [c for c in companies if any(n and n.casefold() in quote.casefold()
                          for n in [c['name'], *(c.get('aliases') or [])])]
                if len(owners) != 1:
                    continue
                company = owners[0]
                names = [company['name'], *(company.get('aliases') or [])]
                if not _leads(quote, names):
                    continue
                if not any(re.match(re.escape(n) + r'(?=\s|20\d{2}|披露|公布|宣布|实现|发布|录得|报告)', quote, re.I)
                           for n in names if n):
                    continue
                dims = scope_dimensions(quote)
                matches = list(_METRIC.finditer(quote))
                if (len(matches) != 1 or len(_QUANTITY.findall(quote)) != 1 or len(dims['metric']) != 1
                        or dims['basis'] != {'reported'}
                        or re.search(r'未达|未实现|否认|并非|不是|累计|截至|左右|以上|以下|至少|至多|超过|不足|约\s*\d', quote)):
                    continue
                match = matches[0]
                metric, value, scale, unit = match.groups()
                value = format(Decimal(value.replace(',', '')) * _SCALE[scale or ''], 'f')
                value = value.rstrip('0').rstrip('.') if '.' in value else value
                scope = {key: sorted(dims[key]) for key in _DIMENSIONS}
                # Delivery and shipment remain separate measurements.
                scope['metric'] = ['销量' if metric == '销售量' else metric]
                key = (company['name'], quote.rstrip('。'), url, day.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                records.append({'company': company['name'], 'metric': metric, 'value': value, 'unit': unit,
                                'raw_value': match.group(0), 'scope': scope, 'title': title, 'quote': quote,
                                'url': url, 'source': source.get('source') or '', 'date': day.isoformat(),
                                'basis': 'original_text' if ref else 'headline', **({'ref': ref} if ref else {})})
    # No hidden top-N truncation: the race endpoint already bounds its source inputs.
    grouped = defaultdict(list)
    for record in records:
        dims = record['scope']
        if not all(len(dims[key]) == 1 for key in ('period', 'metric', 'product', 'segment', 'region')):
            continue
        if not _closed_period(dims['period'][0], record['date']):
            continue
        # Recognized clinical scopes need both explicit indication and approval state.
        if (dims['indication'] or dims['regulatory']) and not (dims['indication'] and dims['regulatory']):
            continue
        key = (*(tuple(dims[k]) for k in _DIMENSIONS), record['unit'])
        grouped[key].append(record)
    groups = []
    for rows in grouped.values():
        by_company = defaultdict(list)
        for row in rows:
            by_company[row['company']].append(row)
        # Conflicting sources are not resolved by recency, averaging or majority.
        clean = [items for items in by_company.values() if len({r['value'] for r in items}) == 1]
        if len(clean) >= 2:
            groups.append({'scope': rows[0]['scope'], 'metric': _LABEL[rows[0]['scope']['metric'][0]],
                           'unit': rows[0]['unit'], 'records': [items[0] for items in clean]})
    return {'records': records, 'groups': groups, 'ranked': False,
            'originals_truncated': any(s.get('original_truncated') for e in events for s in e.get('sources') or [])}
