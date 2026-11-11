"""How well a round worked, measured from what it stored: no model calls, no new requests.

Each figure is a count over this round's own records (collection observations,
screening results, section outcomes, request receipts) and is compared with the
previous round. A warning names a figure that is low or got worse; it does not
judge the facts collected.
"""
from __future__ import annotations

import json
from collections import defaultdict
from contextlib import closing

NEWS = ('company', 'competitor', 'industry', 'policy', 'trial')
STALE_ROUNDS = 3  # rounds a query is judged over
STALE_MINIMUM = 5  # screened items it needs before it can be called stale


def _ratio(part, whole):
    return round(part / whole, 3) if whole else None


def _extra(raw):
    try:
        value = json.loads(raw or '{}')
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _collect_jobs(db, pid, limit):
    return [dict(r) for r in db.execute(
        "SELECT id,created_at,finished_at FROM studio_jobs WHERE project_id=? AND kind='collect' AND status='done' "
        'ORDER BY created_at DESC,rowid DESC LIMIT ?', (pid, limit))]


def _analysis_for(db, collect_id):
    row = db.execute("SELECT id FROM studio_jobs WHERE kind='analyze' AND status='done' AND input_version IN (?,?) "
                     'ORDER BY created_at DESC LIMIT 1', ('collect:' + collect_id, 'redo:' + collect_id)).fetchone()
    return row['id'] if row else None


def unit_yield(store, collect_id):
    """Per planned query: items it brought in this round, how many were screened, how many were relevant."""
    from project_watch.studio.collect_job import PATENTS
    from project_watch.studio.collection_ledger import STAGE
    from project_watch.studio.execution import JobExecution

    execution = JobExecution(store, {'id': collect_id})
    units = {u['id']: u for stage in (STAGE, PATENTS)  # the main plan and the patent document follow-up
             for u in ((execution.read_checkpoint(stage) or {}).get('plan') or {}).get('units') or []}
    counts = defaultdict(lambda: {'collected': 0, 'screened': 0, 'relevant': 0})
    with closing(store._connect()) as db:
        for row in db.execute('SELECT DISTINCT o.unit_id,o.item_id,i.triaged,i.relevant FROM studio_collection_observations o '
                              "JOIN studio_items i ON i.id=o.item_id WHERE o.job_id=? AND o.operation IN ('added','updated')",
                              (collect_id,)):
            entry = counts[row['unit_id']]
            entry['collected'] += 1
            entry['screened'] += bool(row['triaged'])
            entry['relevant'] += bool(row['triaged'] and row['relevant'])
    return [{'source': units[uid]['source'], 'category': units[uid]['category'], 'query': units[uid]['query'], **entry}
            for uid, entry in counts.items() if uid in units]


def _round(store, db, collect):
    from project_watch.studio import analyze_job
    from project_watch.studio.execution import JobExecution

    items = [dict(r) for r in db.execute(
        'SELECT DISTINCT i.id,i.category,i.published_at,i.triaged,i.relevant,i.extra FROM studio_collection_observations o '
        "JOIN studio_items i ON i.id=o.item_id WHERE o.job_id=? AND o.operation IN ('added','updated')", (collect['id'],))]
    news = [i for i in items if i['category'] in NEWS]
    screened = [i for i in items if i['triaged']]
    bindings = [(_extra(i['extra']).get('triage_binding') or {}).get('status') for i in screened]
    bound = [b for b in bindings if b]
    analysis = _analysis_for(db, collect['id'])
    sections = None
    if analysis:
        execution = JobExecution(store, {'id': analysis})
        publication = execution.read_checkpoint(analyze_job.PUBLICATION) or {}
        # The frozen scope decides retired sections, whichever checkpoint wrote the outcomes.
        retired = (execution.read_checkpoint(analyze_job.SECTION_SCOPE) or {}).get('retired', ())
        outcomes = {k: ({'status': 'retired'} if k in retired else o)
                    for k, o in (publication.get('sections') or {}).items()}
        considered = {k: o for k, o in outcomes.items() if o.get('status') not in ('missing', 'unchanged', 'retired')}
        sections = {'published': len(publication.get('published') or []), 'considered': len(considered),
                    'unchanged': sorted(k for k, o in outcomes.items() if o.get('status') == 'unchanged'),
                    'dropped_points': sum(len(o.get('dropped') or []) for o in considered.values()),
                    'rejected': sorted(k for k, o in considered.items() if o.get('status') == 'rejected')}
    jobs = [collect['id'], *([analysis] if analysis else [])]
    marks = ','.join('?' * len(jobs))
    columns = {r[1] for r in db.execute('PRAGMA table_info(studio_job_calls)')}
    tokens = ('SUM(CASE WHEN kind=\'model\' THEN COALESCE(input_tokens,0) ELSE 0 END)' if 'input_tokens' in columns else '0')
    calls = db.execute(f"SELECT COUNT(*),SUM(status IN ('unknown','started')),{tokens} FROM studio_job_calls "
                       f'WHERE job_id IN ({marks})', jobs).fetchone()
    return {
        'collect_job_id': collect['id'], 'analyze_job_id': analysis, 'finished_at': collect['finished_at'],
        'items': len(items),
        'date_coverage': _ratio(sum(1 for i in news if i['published_at']), len(news)),
        'screened': len(screened),
        'relevant_share': _ratio(sum(1 for i in screened if i['relevant']), len(screened)),
        'matched_share': _ratio(sum(1 for b in bound if b == 'bound'), len(bound)),
        'sections': sections,
        'requests': calls[0] or 0, 'unclear_requests': calls[1] or 0, 'model_input_tokens': calls[2] or 0,
    }


def _warnings(now, before):
    notes = []

    def low(key, floor, text):
        value = now.get(key)
        if value is not None and value < floor:
            notes.append(f'{text} {value:.0%}，低于 {floor:.0%}')
        elif value is not None and before and before.get(key) is not None and value < before[key] - 0.1:
            notes.append(f'{text}从 {before[key]:.0%} 降到 {value:.0%}')
    low('date_coverage', 0.8, '新闻有发布日期的比例')
    low('matched_share', 0.9, 'AI 初筛结果能对应原文的比例')
    if now['unclear_requests']:
        notes.append(f"{now['unclear_requests']} 个请求结果不明，已记账、不会自动重发")
    sections = now.get('sections')
    if sections and sections['rejected']:
        notes.append(f"{len(sections['rejected'])} 个分析板块未发布：{'、'.join(sections['rejected'])}")
    return notes


def stale_queries(store, pid, rounds=STALE_ROUNDS, *, before=None):
    """Queries that, over the last rounds, brought screened items and none relevant."""
    with closing(store._connect()) as db:
        jobs = [j for j in _collect_jobs(db, pid, 50) if before is None or j['created_at'] < before][:rounds]
    if len(jobs) < rounds:
        return []
    totals = defaultdict(lambda: {'collected': 0, 'screened': 0, 'relevant': 0, 'rounds': 0, 'category': ''})
    for job in jobs:
        for unit in unit_yield(store, job['id']):
            entry = totals[(unit['source'], unit['query'])]
            entry['category'] = unit['category']
            entry['rounds'] += 1
            for key in ('collected', 'screened', 'relevant'):
                entry[key] += unit[key]
    stale = [{'source': source, 'query': query, 'category': t['category'], 'collected': t['collected'],
              'screened': t['screened'], 'rounds': rounds}
             for (source, query), t in totals.items() if t['screened'] >= STALE_MINIMUM and not t['relevant']]
    return sorted(stale, key=lambda s: -s['screened'])


def report(store, pid):
    """The latest finished round against the one before it, with warnings and stale queries."""
    with closing(store._connect()) as db:
        jobs = _collect_jobs(db, pid, 2)
        if not jobs:
            return None
        rounds = [_round(store, db, job) for job in jobs]
    current, previous = rounds[0], rounds[1] if len(rounds) > 1 else None
    return {'current': current, 'previous': previous, 'warnings': _warnings(current, previous),
            'stale_queries': stale_queries(store, pid)}
