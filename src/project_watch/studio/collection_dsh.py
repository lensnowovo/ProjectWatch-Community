"""Saved host evidence and receipt-bound DSH supplements."""
import hashlib
import json
from contextlib import closing
from types import SimpleNamespace

from project_watch.studio.collection_ledger import (
    STAGE,
    SUPPLEMENT_PREFIX,
    SUPPLEMENTS,
    CollectionLedger,
)
from project_watch.studio.collection_plan import (
    EXCLUDED,
    channel_key,
    channel_status,
    make_plan,
    reconcile,
    resolved_scopes,
)


def current_round(research):
    saved = research.execution.read_checkpoint(STAGE)
    if saved is None:
        return None
    plan = saved['plan']
    if plan['project_id'] != research.project_id or plan['round_id'] != research.execution.job['id']:
        raise ValueError('collection evidence outside research round')
    reconcile(plan, saved['receipts'])
    return saved


def rounds(research):
    base = current_round(research)
    if base is None:
        return []
    result = [base]
    for entry in research.execution.read_checkpoint(SUPPLEMENTS) or []:
        saved = research.execution.read_checkpoint(entry['stage']) or {'plan': entry['plan'], 'receipts': []}
        plan = saved['plan']
        if (plan != entry['plan'] or plan['project_id'] != research.project_id
                or plan['round_id'] != research.execution.job['id'] or plan['binding'] != base['plan']['binding']):
            raise ValueError('collection supplement outside research round')
        reconcile(plan, saved['receipts'])
        result.append(saved)
    return result


def coverage(research):
    saved = rounds(research)
    if saved:
        outcomes = [reconcile(s['plan'], s['receipts']) for s in saved]
        links = resolved_scopes(saved[0]['plan'], [s['plan'] for s in saved[1:]])
        rows = [r for o in outcomes for r in o['rows']]
        for row in rows:
            if row['unit_id'] in links:
                row.update(initial_outcome=row['outcome'], initial_reason=row['reason'],
                           outcome='scope_resolved', required=False,
                           resolved_by=links[row['unit_id']], reason='Concrete evidence-bound queries follow')
        unresolved = [r['unit_id'] for r in rows if r['outcome'] not in EXCLUDED | {'success', 'scope_resolved'}]
        return dict(status='complete' if rows and not unresolved else 'partial', rows=rows,
                    unresolved_ids=unresolved,
                    required_count=sum(r['required'] for r in rows),
                    successful_count=sum(o['successful_count'] for o in outcomes),
                    empty_plan=all(o['empty_plan'] for o in outcomes))
    admission = research.execution.read_checkpoint('collection_admission_v1')
    if admission:
        return dict(status='partial', rows=[], unresolved_ids=[], required_count=0,
                    successful_count=0, empty_plan=True,
                    reason=admission['reason'] or 'collection_manifest_missing')
    return None


def _refs(research):
    result = {}
    for saved in rounds(research):
        for receipt in saved['receipts']:
            for ref in receipt.get('evidence_refs', []):
                sid = 'C' + hashlib.sha256(ref['evidence_version_id'].encode()).hexdigest()[:18]
                result.setdefault(sid, []).append(ref)
    return result


def report(research, collected):
    """Project saved retrieval receipts, never model search counts or claims."""
    saved = rounds(research)
    receipts = {r['unit_id']: r for s in saved for r in s['receipts']}
    groups = {}
    for row in collected['rows']:
        groups.setdefault(channel_key(row), []).append(row)
    channels = {}
    for key, rows in groups.items():
        added = {ref['item_id'] for row in rows for ref in receipts.get(row['unit_id'], {}).get('evidence_refs', [])
                 if ref.get('operation') == 'added'}
        channels[key] = dict(**channel_status(rows), checked=sum(r['result_count'] is not None for r in rows),
            added=len(added), errors=list(dict.fromkeys(r['reason'] or r['outcome'] for r in rows
                                                      if r['outcome'] in ('failed', 'unknown'))),
            deferred=sum(r['outcome'] not in EXCLUDED | {'success', 'scope_resolved'} for r in rows),
            rows=rows, count_kind='saved_source_items')
    return dict(schema_version=1, job_id=research.execution.job['id'],
                mode=saved[0]['plan']['mode'] if saved else None,
                planned_at=saved[0]['plan']['created_at'] if saved else None,
                status=collected['status'], channels=channels, reason=collected.get('reason', ''))


def latest_report(store, project_id):
    """Read a coherent job/manifest/receipt snapshot, without claiming a lease.

    A newer queued round supersedes the last completed round even before its
    manifest exists. A topic never replaces the company's collection report.
    Corrupt evidence is explicitly unavailable, never a successful empty round.
    """
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.store import now

    with closing(store._connect()) as db:
        db.execute('BEGIN')
        job = db.execute("""SELECT j.* FROM studio_jobs j
            LEFT JOIN studio_job_checkpoints c ON c.job_id=j.id AND c.stage='collection_request_v1'
            WHERE j.project_id=? AND j.kind IN ('external','external_followup')
            AND COALESCE(json_extract(CASE WHEN json_valid(c.data) THEN c.data ELSE '{}' END,'$.mode'),'')!='topic'
            ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1""", (project_id,)).fetchone()
        if job is None:
            return None
        job = dict(job)
        execution = JobExecution(store, job)
        reader = SimpleNamespace(job=job, read_checkpoint=lambda stage: execution.read_checkpoint(stage, db=db))
        research = SimpleNamespace(project_id=project_id, execution=reader)
        observed = now()
        project = db.execute('SELECT status FROM studio_projects WHERE project_id=?', (project_id,)).fetchone()
        active = (job['status'] == 'running' and (job.get('lease_until') or '') > observed
                  and project is not None and project['status'] == 'active')
        base = dict(schema_version=1, job_id=job['id'], job_status=job['status'], observed_at=observed,
                    active=active, status='partial', channels={})
        try:
            intent = reader.read_checkpoint('collection_request_v1')
            collected = coverage(research)
            if collected is None:
                if intent is None:
                    return None  # Historical/legacy rounds keep their old report semantics.
                return dict(base, mode=intent.get('mode'), planned_at=None, view_state='awaiting_plan')
            result = report(research, collected)
            return {**base, **result, 'view_state': 'receipts'}
        except (ValueError, KeyError, TypeError):
            return dict(base, mode=None, planned_at=None, view_state='unavailable')


def page_source(research, sid):
    """A separately addressable page view never replaces the collected snapshot."""
    from project_watch.studio.collection_host import guard_scope

    guard_scope(research)
    refs = _refs(research)
    if sid.startswith('P'):
        parent = next((key for key in refs if 'P' + key[1:] == sid), None)
        if parent is None:
            raise ValueError('unknown collection page source_id')
        cached = research.execution.read_checkpoint('dsh_source_' + sid)
        if not cached or cached.get('collection_parent') != parent:
            raise ValueError('collection page has not been opened')
        cached['collection_matches'] = refs[parent]
        return cached
    original = source(research, sid)
    page_id = 'P' + sid[1:]
    cached = research.execution.read_checkpoint('dsh_source_' + page_id)
    if cached:
        return page_source(research, page_id)
    # On an unreadable page the original evidence remains available as a
    # fallback, with its original type and scope, never relabelled as body.
    page = {**original, 'id': page_id, 'collection_parent': sid, 'read_attempted': False}
    page.pop('next_offset', None)
    page.pop('window_offset', None)
    research.execution.checkpoint('dsh_source_' + page_id, page)
    return page


def search(research, query, category, *, target_id=None, adapter='search', evidence_ids=(), historical=False):
    """Freeze each model supplement before dispatch without changing base scope."""
    from datetime import datetime, timedelta

    from project_watch.studio import collection_continuation, collection_history
    from project_watch.studio.collection_host import guard_scope, sources_for
    from project_watch.studio.collection_ingest import CollectionIngest
    from project_watch.studio.collection_runner import run_collection
    from project_watch.studio.collection_targets import require_source_enabled

    guard_scope(research)
    require_source_enabled(research.store, research.project_id, adapter)
    saved = rounds(research)
    if not saved:
        raise ValueError('host collection manifest required')
    base = saved[0]['plan']
    target_id = target_id or research.project_id
    # An exact host query is not repeated just because DSH asks for it again.
    match = next(((s, u) for s in saved for u in s['plan']['units']
                  if u['source'] == adapter and u['query'] == query and u['category'] == category
                  and u['target_id'] == target_id and u['disposition'] != 'not_due'), None)
    if match:
        state, unit = match
        plan = state['plan']
        stage = STAGE if state is saved[0] else SUPPLEMENT_PREFIX + unit['id'][:40]
    else:
        sources = sources_for(research)
        capability = sources.capabilities()[adapter]
        query_version = 'supplement-v1'
        if target_id.startswith('scope:'):
            query_version = 'scope-v1'  # Same registered scope as the next host round.
        if target_id.startswith('watch:'):
            watch = next((w for w in research.pipeline.monitoring_plan.items(research.project_id)
                          if 'watch:' + w['id'] == target_id), None)
            if watch is not None:
                query_version = 'watch:' + str(watch['lifecycle_version'])
        end = base['created_at']
        start = (datetime.fromisoformat(end) - timedelta(days=90)).isoformat()
        plan = make_plan(round_id=base['round_id'], project_id=base['project_id'], binding=base['binding'],
                         mode=base['mode'], at=end, candidates=[dict(
                             source=adapter, target_id=target_id, category=category, query_version=query_version,
                             query=query, capability=capability, reason='' if capability == 'available' else capability,
                             applicability_evidence_ids=list(evidence_ids),
                             window_start=None if historical else start, window_end=end)])
        if base.get('history_binding'):
            plan = collection_history.schedule(plan, base['history_binding'],
                                               collection_history.read(research.store, research.project_id),
                                               continuations=collection_continuation.read(research.store, research.project_id),
                                               **base['history_policy'])
        unit = plan['units'][0]
        stage = SUPPLEMENT_PREFIX + unit['id'][:40]
        entries = research.execution.read_checkpoint(SUPPLEMENTS) or []
        # Index the frozen intent first: an interruption before ledger creation
        # must remain pending in finish coverage, never silently disappear.
        research.execution.checkpoint_chunked(SUPPLEMENTS, [*entries, dict(stage=stage, plan=plan)])
    ledger = CollectionLedger(research.execution, plan, stage=stage)
    receipt = next((r for r in ledger.receipts if r['unit_id'] == unit['id']), None)
    if unit['disposition'] == 'pending' and (not receipt or receipt['outcome'] == 'running'):
        sources = sources_for(research)
        # Main scope is dispatched by prepare(); a supplement never expands or
        # reruns the other entries in that frozen manifest.
        if stage == STAGE:
            raise ValueError('base collection must finish before model supplements')
        run_collection(ledger, collect=sources.collect, ingest=CollectionIngest(research.execution),
                       guard=lambda: guard_scope(research))
    row = next(r for r in ledger.coverage()['rows'] if r['unit_id'] == unit['id'])
    receipt = next((r for r in ledger.receipts if r['unit_id'] == unit['id']), {})
    ids = list(dict.fromkeys('C' + hashlib.sha256(ref['evidence_version_id'].encode()).hexdigest()[:18]
                            for ref in receipt.get('evidence_refs', [])))
    result = dict(sources=[source(research, sid) for sid in ids], collection=row, untrusted_data=True)
    if row['outcome'] == 'success' and adapter == 'search':
        if not any(r.get('unit_id') == unit['id'] for r in research.state['searches']):
            research.state['searches'].append(dict(query=query, category=category, results=len(ids), unit_id=unit['id']))
    elif row['outcome'] != 'success':
        result['error'] = row['reason'] or row['outcome']
    return result


def collect_specialist(research, *, adapter, query, source_id, quote, reason):
    from project_watch.studio.collection_host import guard_scope
    from project_watch.studio.collection_targets import require_source_enabled, retain

    guard_scope(research)
    require_source_enabled(research.store, research.project_id, adapter)
    if current_round(research) is None:
        raise ValueError('host collection manifest required')
    cited = research.source(source_id)
    if not quote or quote not in cited['excerpt']:
        raise ValueError('professional query requires an exact public quote')
    start = cited['excerpt'].index(quote)
    span = next(((i, s) for i, s in enumerate(cited['part_spans'])
                 if s['start'] <= start and start + len(quote) <= s['start'] + s['length']), None)
    if span is None:
        raise ValueError('quote crosses an evidence part')
    index, bounds = span
    offset = start - bounds['start']
    target = retain(research, adapter=adapter, query=query, reason=reason, ref=dict(
        evidence_version_id=cited['evidence_version_id'], part_index=index,
        start=offset, end=offset + len(quote), quote=quote))
    result = search(research, target['query'], target['category'], target_id='scope:' + target['id'],
                    adapter=adapter, evidence_ids=[target['ref']['evidence_version_id']], historical=target['historical'])
    return {**result, 'query_scope': target}


def _evidence(research, refs):
    record = research.pipeline.evidence.get(research.project_id, refs[0]['evidence_version_id'], include_internal=False)
    if record['source_type'] not in ('structured_record', 'search_summary'):
        raise ValueError('invalid collection evidence type')
    text = ''.join(p['text'] for p in record['parts'])
    return record, json.loads(text)


def catalog(research, *, offset=0, limit=20):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 40:
        raise ValueError('invalid collection source page')
    entries = list(_refs(research).items())
    sources = []
    for sid, refs in entries[offset:offset + limit]:
        record, snapshot = _evidence(research, refs)
        sources.append(dict(id=sid, title=snapshot['title'], url=snapshot['url'],
                            source_type=record['source_type'], read_scope=record['read_scope'],
                            evidence_version_id=record['id'], matches=refs))
    return dict(sources=sources, total=len(entries), offset=offset,
                next_offset=offset + limit if offset + limit < len(entries) else None,
                untrusted_data=True)


def source(research, sid, *, offset=None):
    refs = _refs(research).get(sid)
    if not refs:
        raise ValueError('unknown collection source_id')
    record, snapshot = _evidence(research, refs)
    cached = research.execution.read_checkpoint('dsh_source_' + sid)
    if offset is None and cached and cached['evidence_version_id'] == record['id']:
        if cached.get('collection_matches') != refs:
            cached['collection_matches'] = refs
            research.execution.checkpoint('dsh_source_' + sid, cached)
        return cached
    offset = 0 if offset is None else offset
    size = sum(len(p['text']) for p in record['parts'])
    if type(offset) is not int or not 0 <= offset < size:
        raise ValueError('offset outside source record')
    # Keep each displayed window inside one immutable evidence part. Quote
    # offsets remain part-relative, including windows beyond the first part.
    start = 0
    spans = []
    for part in record['parts']:
        spans.append({'start': start - offset, 'length': len(part['text'])})
        if start <= offset < start + len(part['text']):
            excerpt = part['text'][offset - start:offset - start + 12000]
        start += len(part['text'])
    result = dict(id=sid, url=snapshot['url'], title=snapshot['title'], source=snapshot['source'],
                  published_at=snapshot.get('published_at'), excerpt=excerpt,
                  read_scope=record['read_scope'], source_type=record['source_type'],
                  evidence_version_id=record['id'], part_spans=spans, collection_matches=refs,
                  read_status='structured_record' if record['source_type'] == 'structured_record' else 'summary_only',
                  read_attempted=True, window_offset=offset,
                  next_offset=offset + len(excerpt) if offset + len(excerpt) < size else None,
                  untrusted_data=True)
    research.execution.checkpoint('dsh_source_' + sid, result)
    return result
