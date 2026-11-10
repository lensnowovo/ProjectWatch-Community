"""Publish reviewed cumulative drafts before drafting the rest of a frozen snapshot."""
from contextlib import closing
from copy import deepcopy

from project_watch.studio.store import digest

MODE = 'judgment-delivery-v1'
BASELINE = 'judgment-delivery-baseline-v1'
CURRENT = 'judgment-delivery-current-v1'


def enabled(execution):
    return bool((execution.read_checkpoint(MODE) or {}).get('enabled'))


def freeze_mode(execution):
    """Do not migrate an existing uncertain request or an already-started review."""
    saved = execution.read_checkpoint(MODE)
    if saved is not None:
        return saved['enabled']
    from project_watch.studio.dispatch import RECEIVED_FAILURE, batch_of, explicit_redo
    batch = batch_of(execution.job)
    with closing(execution.store._connect()) as db:
        uncertain = db.execute(
            "SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
            "WHERE j.project_id=? AND (j.id=? OR (? IS NOT NULL AND j.kind='analyze' "
            "AND j.input_version IN (?,?))) AND (c.status IN ('unknown','started') "
            f"OR (c.kind='model' AND c.status='completed' AND c.response_status IN {RECEIVED_FAILURE} "
            "AND c.retryable=0)) LIMIT 1",
            (execution.job['project_id'], execution.job['id'], batch,
             'collect:' + str(batch), 'redo:' + str(batch))).fetchone()
        reviewing = db.execute(
            "SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND "
            "(stage LIKE 'section-%' OR stage LIKE 'analyze_reviewed_%' "
            "OR stage LIKE 'analyze_section_outcome_%') LIMIT 1", (execution.job['id'],)).fetchone()
        if uncertain and batch and not explicit_redo(execution.job):
            from project_watch.studio.execution import JobExecution
            from project_watch.studio.llm import ModelError
            siblings = db.execute("SELECT id FROM studio_jobs WHERE project_id=? AND kind='analyze' "
                                  "AND input_version IN (?,?)", (execution.job['project_id'],
                                   'collect:' + batch, 'redo:' + batch)).fetchall()
            if any((JobExecution(None, {'id': row[0]}).read_checkpoint(MODE, db=db) or {}).get('enabled')
                   for row in siblings):
                raise ModelError('已有分批判断请求结果不明，未切换流程或重发', code='unknown_not_resent')
    version = (execution.read_checkpoint('analysis_review_sources_v1') or {}).get('version', 1)
    value = version >= 3 and not uncertain and not reviewing
    execution.checkpoint_chunked(MODE, {'enabled': bool(value), 'independent_pages': bool(value)})
    return bool(value)


def independent_pages(execution):
    # Existing scopes retain their exact review request identities.
    return bool((execution.read_checkpoint(MODE) or {}).get('independent_pages'))


def publish_pages(pipeline, execution, llm, draft, sources, selected, targets, retained, guard, revision):
    """Finish each page before starting another; never replay a page's uncertain review."""
    from project_watch.studio import analyze_job, section_analysis
    from project_watch.studio.section_progress import outcome_state

    peers = dict(retained)
    outcomes, published = {}, []
    for name in targets:
        page_revision = digest(['independent-page-v1', revision, name])[:32]
        key = 'independent-page-result:' + page_revision
        saved = execution.read_checkpoint(key)
        if saved is None:
            blocked = execution.read_checkpoint('independent-page-blocked:' + name)
            if blocked is not None:
                saved = {'outcome': blocked['outcome'], 'published': False, 'raw': None}
            else:
                peer_key = 'independent-page-peers:' + page_revision
                frozen = execution.read_checkpoint(peer_key)
                if frozen is None:
                    frozen = {'retained': peers}
                    execution.checkpoint_chunked(peer_key, frozen)
                result = analyze_job.publish_draft(
                    pipeline, execution, llm, deepcopy(draft), sources, selected, [name],
                    frozen['retained'], guard, revision=page_revision, independent=False)
                saved = execution.read_checkpoint('delivery-outcome:' + digest([page_revision, name])[:40])
                if 'request_outcome_unknown' in result['sections'][name].get('issues', ()):
                    execution.checkpoint_chunked('independent-page-blocked:' + name,
                                                 {'outcome': saved['outcome']})
            execution.checkpoint_chunked(key, saved)
        outcomes[name] = saved['outcome']
        if saved['published']:
            published.append(name)
            peers[name] = {'raw': saved['raw'],
                           'numbered': {ref: sources[ref] for ref in saved['outcome']['refs']}}
    for name in section_analysis.SECTIONS:
        if name not in targets:
            outcomes[name] = {'status': 'unchanged', 'reason': 'no new relevant material for this section'}
    settled = all(name in published or outcome_state(outcomes[name]) == 'insufficient' for name in targets)
    return {'status': 'completed' if settled else 'partial', 'targets': targets,
            'snapshot_id': selected['id'], 'sections': outcomes, 'published': published,
            'coverage': execution.read_checkpoint(analyze_job.COVERAGE) or selected['coverage']}


def deliver_batches(pipeline, execution, llm, company, sources, selected, targets, retained, guard):
    from project_watch.studio import analyze_job
    from project_watch.studio.llm import ModelError

    baseline = execution.read_checkpoint(BASELINE)
    if baseline is None:
        baseline = {'retained': retained}
        execution.checkpoint_chunked(BASELINE, baseline)
    latest = None
    published = set()
    delivered_peers = {}
    seen = set()

    def deliver(draft, *, final=False):
        nonlocal latest
        requested = ([name for name in targets if name not in published]
                     if independent_pages(execution) and not final else targets)
        if not requested:
            return
        revision = digest([draft, selected['id'], requested])[:32]
        if revision in seen:
            return  # A -> B -> A must not move the visible/reviewed head back to A.
        seen.add(revision)
        key = 'delivery-result:' + revision
        result = execution.read_checkpoint(key)
        if result is None:
            peer_key = 'delivery-peers:' + revision
            peers = execution.read_checkpoint(peer_key)
            if peers is None:
                peers = {'retained': {**baseline['retained'], **delivered_peers}}
                execution.checkpoint_chunked(peer_key, peers)
            # Full frozen sources remain available for counterevidence, including
            # material not yet traversed by the drafting loop. Never publish drafts.
            result = analyze_job.publish_draft(
                pipeline, execution, llm, deepcopy(draft), sources, selected, requested,
                peers['retained'], guard, revision=revision)
            execution.checkpoint_chunked(key, result)
        published.update(result['published'])
        for name in result['published']:
            page_revision = (digest(['independent-page-v1', revision, name])[:32]
                             if independent_pages(execution) else revision)
            receipt = execution.read_checkpoint('delivery-outcome:' + digest([page_revision, name])[:40])
            delivered_peers[name] = {'raw': receipt['raw'],
                'numbered': {ref: sources[ref] for ref in receipt['outcome']['refs']}}
        prior_sections = (latest or {}).get('sections', {})
        sections = {name: (result['sections'][name] if name in requested else
                          prior_sections.get(name, result['sections'][name]))
                    for name in result['sections']}
        latest = {**result, 'targets': targets, 'sections': sections, 'published': sorted(published)}
        execution.checkpoint_chunked(CURRENT, latest)
        if not independent_pages(execution) and any('request_outcome_unknown' in outcome.get('issues', ())
               for outcome in result['sections'].values()):
            # A different cumulative candidate must not sidestep this lost review.
            raise ModelError('已保存核验结果；未重发结果不明的请求', code='unknown_not_resent')

    draft = analyze_job.draft_batches(
        execution, llm, pipeline.insight_generator.SYSTEM + analyze_job.GUIDANCE,
        company, sources, selected, targets, on_draft=deliver)
    if latest is None or independent_pages(execution):
        deliver(draft, final=True)
    latest['coverage'] = execution.read_checkpoint(analyze_job.COVERAGE) or selected['coverage']
    if latest['coverage'].get('blocked') or latest['coverage'].get('remaining_fragments', 0):
        latest['status'] = 'partial'
    execution.checkpoint_chunked(analyze_job.PUBLICATION, latest)
    return latest
