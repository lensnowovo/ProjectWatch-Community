"""Read-only presentation contract over current stories and validated saved analysis.

The caller supplies ExternalAnalysisStore.read(), which filters stale evidence.
This module never retrieves, dispatches, verifies facts or writes human judgments.
"""
from copy import deepcopy

from project_watch.external_attention import market_commentary_only

VERSION = 'external-intelligence-v1'
SECTIONS = (
    ('priority', '先看这些'),
    ('context', '行业与周边'),
    ('background', '背景消息'),
    ('unresolved', '关联待核实'),
)


def _placement(item, analysis):
    assessment = (analysis or {}).get('relationship', {}).get('assessment')
    if market_commentary_only(item['statement']):
        return dict(section='background', basis='headline_topic', reason='市场观点，暂不作为经营变化。')
    if assessment == 'not_relevant':
        return dict(section='background', basis='saved_analysis', reason='已有解读未发现具体项目关联。')
    if assessment == 'potentially_relevant':
        direct = item['category'] == 'company' and item.get('attention', {}).get('level') == 'read_first'
        return dict(section='priority' if direct else 'context', basis='saved_analysis',
                    reason='已有解读提示值得跟踪；不代表事件或项目影响已核实。')
    term = item.get('match_term', '').strip()
    # Topic-based reading priority is not a change to the saved AI assessment.
    if (item['category'] == 'company' and len(term) >= 2
            and term.casefold() in item['statement'].casefold()
            and item.get('attention', {}).get('level') == 'read_first'):
        return dict(section='priority', basis='named_company_headline',
                    reason='标题涉及同名公司的经营动态，先核对主体和原文。')
    return dict(section='unresolved', basis='insufficient_link', reason='暂不足以判断与本项目的关系。')


def build_intelligence(project_id, briefing, saved):
    """Pure projection; latest *current* result wins, independent of input order."""
    current = {}
    if saved.get('project_id') == project_id:
        for row in sorted(saved.get('items', []), key=lambda r: (r['recorded_at'], r['id']), reverse=True):
            packet, analysis = row['packet'], row['analysis']
            if (packet.get('project_id') != project_id or analysis.get('project_id') != project_id
                    or analysis.get('signal_id') != packet.get('signal_id')):
                continue
            current.setdefault(packet['signal_id'], row)
    items, seen = [], set()
    for source in briefing.get('items', []):
        if source.get('project_id') != project_id or source['id'] in seen:
            continue
        seen.add(source['id'])
        row = current.get(source['id'])
        analysis = row['analysis'] if row else None
        item = {k: deepcopy(source.get(k)) for k in (
            'id', 'project_id', 'category', 'category_label', 'statement', 'summary', 'url',
            'published_at', 'match_term', 'attention', 'question',
        )}
        item.update(
            placement=_placement(source, analysis),
            analysis_state='available' if row else 'not_available',
            analysis_id=row['id'] if row else None,
            analyzed_at=row['recorded_at'] if row else None,
            analysis=deepcopy(analysis),
            evidence=deepcopy(row['packet']['references']) if row else [],
            scope_evidence=deepcopy(source.get('evidence_refs', [])),
            content_scope=analysis['content_scope'] if row else source.get('content_scope'),
            gaps=deepcopy(analysis['unknowns'] if row else source.get('unknowns', [])),
            # Existing analysis validates quotes, not truth or identity. Never
            # infer verified status from a successful model/schema response.
            verification={k: 'not_verified' for k in ('event', 'identity', 'project_impact', 'policy_applicability')},
        )
        items.append(item)
    # Stable latest-first order inside each mutually exclusive section.
    items.sort(key=lambda i: (i.get('published_at') or '', i['id']), reverse=True)
    order = {key: index for index, (key, _) in enumerate(SECTIONS)}
    items.sort(key=lambda i: order[i['placement']['section']])
    counts = {key: sum(i['placement']['section'] == key for i in items) for key, _ in SECTIONS}
    return dict(project_id=project_id, version=VERSION, items=items, counts=counts,
                sections=[dict(key=key, label=label) for key, label in SECTIONS],
                coverage=deepcopy(briefing.get('coverage', [])),
                report_groups=deepcopy(briefing.get('report_groups', [])),
                stale_analysis_count=saved.get('stale_count', 0),
                deferred_market_count=briefing.get('deferred_market_count', 0),
                note='阅读顺序不代表已核实的投资影响；AI 解读与人的判断分别保留。')
