"""Public promises versus located subsequent outcomes; reading never calls a model."""

import json
import re
from contextlib import closing
from datetime import UTC, date, datetime

from project_watch.studio.business_relations import current_fact
from project_watch.studio.public_dates import date_in_quote, due_interval

__all__ = ['build', 'due_interval', 'project_commitment', 'read']
_KINDS = {'量产': r'量产|批量生产|mass production', '交付': r'交付|delivered|delivery',
          '获批': r'获批|批准|approved', '上市（IPO）': r'上市|IPO', '融资': r'融资|received',
          '投产': r'投产|开始生产', '营收目标': r'营收|收入|revenue'}
_STATES = {'量产': {'completed'}, '交付': {'delivered', 'accepted'}, '获批': {'approved'},
           '上市（IPO）': {'completed', 'approved'}, '融资': {'received'}, '投产': {'started', 'completed'}}


def source_links(service, project_id, refs):
    """Use actual item URLs, including a guarded reader's resolved publisher URL."""
    out = []
    with closing(service.store._connect()) as db:
        for ref in refs:
            r = db.execute("SELECT i.url,i.source,i.title FROM studio_item_evidence ie JOIN studio_items i ON i.id=ie.item_id "
                           "WHERE ie.project_id=? AND ie.evidence_version_id=? ORDER BY i.collected_at DESC LIMIT 1",
                           (project_id, ref['evidence_version_id'])).fetchone()
            if r:
                item = dict(r)
                if item not in out:
                    out.append(item)
    return out


def project_commitment(service, project_id, fact, *, include_internal=False):
    obj = service.objects.get(project_id, fact['subject_id'], include_internal=include_internal)
    attrs = fact['attributes']
    reasons = []
    if fact['visibility'] != 'public' or not current_fact(service, project_id, fact):
        reasons.append('current_public_evidence')
    if fact['subject_attribution'] != 'explicit' or obj['name'] not in attrs['statement']:
        reasons.append('explicit_speaker')
    if not any(all(t in r['quote'] for t in (attrs['statement'], attrs['target_text'], attrs['due_text'], attrs['speaker']))
               for r in fact['refs']):
        reasons.append('located_promise')
    if any(service.evidence.get(project_id, r['evidence_version_id'], include_internal=include_internal)['read_scope'] == 'summary'
           for r in fact['refs']):
        reasons.append('original_passage')
    interval = due_interval(attrs['due_text'], date.fromisoformat(attrs['said_at']))
    if interval is None or tuple(d.isoformat() for d in interval) != (attrs['due_start'], attrs['due_end']):
        reasons.append('located_deadline')
    return {'fact_id': fact['id'], 'subject_id': obj['id'], 'subject_name': obj['name'], **attrs,
            'support': 'pending' if reasons else 'source_assertion', 'missing': reasons, 'refs': fact['refs'],
            'sources': source_links(service, project_id, fact['refs'])}


def build(promises, outcomes, *, today=None):
    today = today or datetime.now(UTC).date()
    groups = {}
    for p in promises:
        if p.get('support') != 'source_assertion' or p['said_at'] > today.isoformat():
            continue
        # Exact subject + target, never fuzzy merging different products, rounds or amounts.
        groups.setdefault((p['subject_id'], p['target_kind'], p['target_text']), []).append(p)
    rows = []
    for history in groups.values():
        history.sort(key=lambda p: (p['said_at'], p['fact_id']))
        p = history[-1]
        first = history[0]['said_at']
        end = date.fromisoformat(p['due_end'])
        matches = []
        for e in outcomes:
            attrs = e['attributes']
            day = e.get('occurred_at')
            if (e['subject_id'] != p['subject_id'] or e.get('basis') != 'actual' or e.get('support') != 'source_assertion'
                    or not day or not first <= day <= today.isoformat() or p['target_text'] != attrs.get('name', '')):
                continue
            state = attrs.get('state') or attrs.get('phase')
            text = attrs.get('state_text') or attrs.get('phase_text') or ''
            if state in ('cancelled', 'delayed', 'withdrawn', 'paused'):
                matches.append((day, 'cancelled' if state in ('cancelled', 'withdrawn') else 'delayed', e))
            elif p['target_kind'] != '营收目标' and (
                state in _STATES.get(p['target_kind'], set()) or state == 'actual') and re.search(
                    _KINDS.get(p['target_kind'], r'(?!)'), text, re.I):
                matches.append((day, 'fulfilled', e))
        matches.sort(key=lambda m: m[0])
        status, label, evidence = 'not_due', '未到期', []
        late_months = 0
        if matches:
            day, action, e = matches[-1]  # reversals are shown, never a monotonically increasing score
            evidence = e.get('sources', [])
            if len({m[1] for m in matches if m[0] == day}) > 1:
                status, label = 'uncertain', '同日记录冲突，系统依据不足'
                evidence = [s for m in matches if m[0] == day for s in m[2].get('sources', [])]
            elif action != 'fulfilled':
                status, label = action, '已取消' if action == 'cancelled' else '已推迟'
            else:
                reversal = max((m[0] for m in matches if m[1] != 'fulfilled'), default='')
                first_done = next(m[0] for m in matches if m[1] == 'fulfilled' and m[0] > reversal)
                d = date.fromisoformat(first_done)
                status, label = ('fulfilled', '已兑现') if d <= end else ('late', '晚 {n} 个月兑现')
                if status == 'late':
                    late_months = max(1, (d.year-end.year)*12+d.month-end.month)
                    label = label.format(n=late_months)
                evidence = next(m[2].get('sources', []) for m in matches if m[0] == first_done and m[1] == 'fulfilled')
        elif today > end:
            status, label = ('overdue', '逾期无消息') if (today-end).days > 30 else ('grace', '到期观察中')
        rows.append({**p, 'status': status, 'status_label': label, 'late_months': late_months,
                     'first_said_at': first, 'last_said_at': p['said_at'], 'statements': len(history),
                     'outcome_sources': evidence, 'due_changed': len({r['due_end'] for r in history}) > 1})
    return {'items': sorted(rows, key=lambda r: (r['status'] != 'overdue', r['due_end'], r['subject_name'])),
            'generated_at': datetime.now(UTC).isoformat(), 'network_on_read': False,
            'basis': '公开来源的表态与已收录后续记录；没有消息不等于未完成'}


def read(service, project_id, *, today=None):
    with closing(service.store._connect()) as db:
        records = db.execute("SELECT record FROM studio_fact_records WHERE project_id=? AND visibility='public' "
                             "AND json_extract(record,'$.attributes.kind') IN ('commitment','event','milestone') ORDER BY id LIMIT 501",
                             (project_id,)).fetchall()
    promises, outcomes = [], []
    from project_watch.studio.event_state import project_event

    for row in records[:500]:
        fact = json.loads(row[0])
        try:
            if fact['attributes']['kind'] == 'commitment':
                promises.append(project_commitment(service, project_id, fact))
            elif fact['attributes']['kind'] == 'event':
                projected = project_event(service, project_id, fact, include_internal=False)
                outcomes.append({**fact, 'support': projected['support'], 'sources': source_links(service, project_id, fact['refs'])})
            else:
                subject = service.objects.get(project_id, fact['subject_id'], include_internal=False)
                attrs = fact['attributes']
                valid = current_fact(service, project_id, fact) and any(subject['name'] in r['quote'] and
                    attrs['name'] in r['quote'] and attrs['phase_text'] in r['quote'] and
                    fact.get('occurred_at') and date_in_quote(date.fromisoformat(fact['occurred_at']), r['quote'])
                    for r in fact['refs']) and all(service.evidence.get(project_id, r['evidence_version_id'], include_internal=False)['read_scope'] != 'summary'
                                                 for r in fact['refs'])
                outcomes.append({**fact, 'support': 'source_assertion' if valid else 'pending',
                                 'sources': source_links(service, project_id, fact['refs'])})
        except KeyError:
            continue
    result = build(promises, outcomes, today=today)
    result['truncated'] = len(records) > 500
    if result['truncated']:
        result['items'] = []  # a partial history cannot support absence or a definitive fulfillment status
    return result
