"""Compare attributed material statements, not inferred real-world events.

Uses already-authorized compiled reading. No extra model call or fact promotion.
Receipt order is explicit: a later upload need not describe a later business event.
"""
from collections import defaultdict
from hashlib import sha256

from project_watch.company_state import TOPICS


def reading_updates(project_id, sources, compiled):
    result = dict(project_id=project_id, rule_version='reading-updates-v1', items=[],
                  total=0, truncated=False)
    if compiled.get('project_id') != project_id:
        return result
    live = {s.id: s for s in sources if s.project_id == project_id}
    grouped = defaultdict(lambda: defaultdict(list))
    for batch in compiled.get('batches', []):
        source = live.get(batch['source_id'])
        if not source or source.content_sha256 != batch['source_version']:
            continue
        for item in batch['items']:
            refs = item.get('source_refs', [])
            if (item.get('topic') not in TOPICS or item.get('origin') != 'ai_synthesis'
                    or item.get('review_status') != 'unreviewed' or not refs
                    or item.get('support_status') != 'quote_verified_not_semantic_verification'
                    or not all(r.get('source_id') == source.id and r.get('version') == source.content_sha256
                               and r.get('quote') and r.get('locator') for r in refs)):
                continue
            grouped[source.id][item['topic']].append(item)

    def fingerprint(item):
        # Same quote, new wording is not new evidence. Exact matching only;
        # this deliberately does not claim semantic equivalence or contradiction.
        quotes = sorted({' '.join(r['quote'].split()) for r in item['source_refs']})
        return (item['topic'], tuple(quotes), item.get('as_of'))

    def source_info(source):
        return dict(id=source.id, reference=source.reference, version=source.content_sha256,
                    received_at=source.created_at.isoformat())

    seen, past, updates = set(), [], []
    ordered = sorted((live[sid] for sid in grouped), key=lambda s: (s.created_at, s.id))
    for source in ordered:
        earlier = [s for s in past if s.created_at < source.created_at
                   and s.content_sha256 != source.content_sha256]
        for topic, items in grouped[source.id].items():
            current = []
            for item in sorted(items, key=lambda i: (i['text'], repr(fingerprint(i)))):
                key = fingerprint(item)
                if key not in seen:
                    current.append(item)
                    seen.add(key)
            if not current or not earlier:
                continue
            previous_source = next((s for s in reversed(earlier) if topic in grouped[s.id]), None)
            previous = grouped[previous_source.id][topic] if previous_source else []
            digest = sha256(repr([fingerprint(i) for i in current]).encode()).hexdigest()[:20]
            updates.append(dict(
                id=f'material:{source.id}:{topic}:{digest}', project_id=project_id,
                topic=topic, title=TOPICS[topic], status='unreviewed',
                change_kind='material_statement', business_change_verified=False,
                occurred_at=None, received_at=source.created_at.isoformat(),
                current_source=source_info(source),
                previous_source=source_info(previous_source) if previous_source else None,
                current=current[:6], previous=previous[:6],
                current_total=len(current), previous_total=len(previous),
                interpretation='按资料接收顺序对照；尚未判断两份材料是否描述同一事项或存在矛盾。',
            ))
        past.append(source)
    updates.sort(key=lambda i: (i['received_at'], i['id']), reverse=True)
    result.update(items=updates[:20], total=len(updates), truncated=len(updates) > 20)
    return result
