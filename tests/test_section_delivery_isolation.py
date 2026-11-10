"""Synthetic per-output delivery and replay contracts. No production/network IO."""
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_section_analysis import host as host
from test_section_analysis import part, reviewer
from test_studio_runtime_health import row, setup

from project_watch.studio import analyze_job, section_analysis, section_progress
from project_watch.studio.execution import JobExecution, JobYield
from project_watch.studio.llm import ModelError
from project_watch.studio.runtime_health import projection


def enable(execution):
    execution.checkpoint(analyze_job.REVIEW_PROTOCOL, {'version': 3})


@pytest.mark.parametrize('failure', ['model_content_rejected', 'request_outcome_unknown', 'model_output_incomplete'])
def test_page_support_failure_retains_siblings_and_never_retries(host, failure):
    research, _, sources = host
    enable(research.execution)
    calls = []
    def checked(system, payload):
        calls.append(('support', tuple(payload['fields'])))
        if any(field.startswith('operations.') for field in payload['fields']):
            raise ModelError('synthetic held', code=failure)
        return reviewer(system, payload)
    def prepare(payload):
        name = next(iter(payload['fields'])).split('.')[0]
        calls.append(('counter', name))
        assert set(payload['evidence']) == set(sources)
        return {'issues': [], 'receipts': ['counter:' + name]}
    checked.prepare = prepare
    raw = {'operations': part(), 'chain': part()}
    result = section_analysis.evaluate(research.execution, raw, sources, checked, lambda: None)
    assert result['operations']['status'] == 'blocked'
    assert result['chain']['status'] == 'approved'
    before = list(calls)
    repeated = section_analysis.evaluate(research.execution, {'operations': part(), 'chain': part()}, sources, checked, lambda: None)
    assert repeated == result and calls == before
    assert calls.index(('counter', 'chain')) > 0


def test_unsupported_summary_never_starts_counter_scan(host):
    research, _, sources = host
    enable(research.execution)
    def unsupported(system, payload):
        value = reviewer(system, payload)
        for check in value['checks']:
            if check['field'].endswith('.summary'):
                check.update(status='insufficient', evidence=[])
        return value
    unsupported.prepare = lambda _: pytest.fail('full scan for rejected candidate')
    result = section_analysis.evaluate(research.execution, {'operations': part()}, sources, unsupported, lambda: None)
    assert result['operations']['status'] == 'rejected'


def test_counterevidence_keeps_full_sources_and_original_field_positions(host):
    research, _, sources = host
    enable(research.execution)
    sources = {**sources, 'S2': {**sources['S1'], 'context': 'Synthetic contradictory tail'}}
    def checked(system, payload):
        value = reviewer(system, payload)
        for check in value['checks']:
            if check['field'] == 'operations.for.0.text':
                check.update(status='insufficient', evidence=[])
        return value
    def prepare(payload):
        assert 'S2' in payload['evidence']  # not cited by the candidate, still checked
        assert payload['fields']['operations.for.0.text'] == 'second'
        return {'issues': [{'field': 'operations.for.0.text', 'code': 'evidence_contradicted',
                            'reason': 'tail contradicts second'}], 'receipts': ['full-counter']}
    checked.prepare = prepare
    value = part()
    value['for'] = [{'text': text, 'refs': ['S1']} for text in ('first', 'second', 'third')]
    raw = {'operations': value}
    result = section_analysis.evaluate(research.execution, raw, sources, checked, lambda: None)
    assert result['operations']['status'] == 'approved'
    assert [r['text'] for r in raw['operations']['for']] == ['third']
    assert set(result['operations']['dropped']) == {'first', 'second'}


def test_unknown_receipt_survives_page_isolation_without_new_request(host):
    from project_watch.studio.http import HttpError
    research, _, sources = host
    enable(research.execution)
    sent = []
    def checked(system, payload):
        return reviewer(system, payload)
    def lost():
        sent.append('counter')
        raise HttpError('connection_failed', transport_phase='response_body', failure_kind='transport_error')
    def prepare(payload):
        if any(field.startswith('operations.') for field in payload['fields']):
            try:
                research.execution.call('model', 'synthetic-counter', lost, timeout=None)
            except HttpError:
                raise ModelError('saved uncertain request', code='request_outcome_unknown') from None
        return {'issues': [], 'receipts': []}
    checked.prepare = prepare
    section_analysis.evaluate(research.execution, {'operations': part(), 'chain': part()}, sources, checked, lambda: None)
    with closing(research.store._connect()) as db:
        before = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls')]
    outcome = section_analysis.evaluate(research.execution, {'operations': part(), 'chain': part()}, sources, checked, lambda: None)
    with closing(research.store._connect()) as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls')] == before
    assert sent == ['counter']
    assert outcome['operations']['issues'] == ['request_outcome_unknown']
    assert outcome['chain']['status'] == 'blocked'  # shared counter depends on the lost request


@pytest.mark.parametrize('failure', ['model_content_rejected', 'request_outcome_unknown', 'model_output_incomplete'])
def test_shared_counter_blocks_all_dependents_without_splitting(host, failure):
    research, _, sources = host
    enable(research.execution)
    calls = []
    def checked(system, payload):
        return reviewer(system, payload)
    def prepare(payload):
        calls.append(payload)
        raise ModelError('shared failure', code=failure)
    checked.prepare = prepare
    draft = {'operations': part(), 'chain': part()}
    for _ in range(2):
        result = section_analysis.evaluate(research.execution, deepcopy(draft), sources, checked, lambda: None)
        assert all(result[name]['status'] == 'blocked' for name in draft)
    assert len(calls) == 1
    assert {f.split('.')[0] for f in calls[0]['fields']} == set(draft)


def test_five_supported_sections_share_one_full_source_pass(host):
    research, _, sources = host
    enable(research.execution)
    calls = []
    def checked(system, payload):
        return reviewer(system, payload)
    def prepare(payload):
        calls.append(payload)
        return {'issues': [], 'receipts': ['shared-counter']}
    checked.prepare = prepare
    draft = {name: part() for name in section_analysis.SECTIONS}
    result = section_analysis.evaluate(research.execution, draft, sources, checked, lambda: None)
    assert all(result[name]['status'] == 'approved' for name in draft)
    assert len(calls) == 1
    assert set(calls[0]['evidence']) == set(sources)
    assert {f.split('.')[0] for f in calls[0]['fields']} == set(draft)


@pytest.mark.parametrize('unknown', [False, True])
@pytest.mark.parametrize('changed', ['VERSION', 'PROTOCOL', 'COUNTER_SYSTEM', 'METRIC_SCOPE', 'review_version'])
def test_counter_contract_change_stops_before_any_provider_or_replay(host, monkeypatch, unknown, changed):
    from project_watch.studio import judgment_review, review_source_traversal
    research, _, sources = host
    enable(research.execution)
    calls = []
    def checked(system, payload):
        calls.append('support')
        return reviewer(system, payload)
    def prepare(payload):
        calls.append('counter')
        # The stable signature is durable even if this request never resolves.
        from project_watch.studio.store import digest
        scope = digest([{'operations': part()}, sources, None])[:32]
        assert research.execution.read_checkpoint('section-counter-meta-v3:' + scope)
        if unknown:
            raise ModelError('synthetic unknown', code='request_outcome_unknown')
        return {'issues': [], 'receipts': ['complete']}
    checked.prepare = prepare
    section_analysis.evaluate(research.execution, {'operations': part()}, sources, checked, lambda: None)
    before = list(calls)
    if changed == 'review_version':
        research.execution.checkpoint(analyze_job.REVIEW_PROTOCOL, {'version': 4})
    else:
        module = judgment_review if changed == 'VERSION' else review_source_traversal
        monkeypatch.setattr(module, changed, str(getattr(module, changed)) + '-changed')
    with pytest.raises(RuntimeError, match='contract changed'):
        section_analysis.evaluate(research.execution, {'operations': part()}, sources, checked, lambda: None)
    assert calls == before


def test_independent_counter_includes_retained_evidence(host):
    research, _, sources = host
    enable(research.execution)
    inspected = []
    def checked(system, payload):
        return reviewer(system, payload)
    def prepare(payload):
        inspected.append(set(payload['evidence']))
        return {'issues': [], 'receipts': []}
    checked.prepare = prepare
    retained = {'chain': {'raw': part(), 'numbered': sources}}
    result = section_analysis.evaluate(research.execution, {'operations': part()}, sources,
                                       checked, lambda: None, retained=retained)
    assert result['operations']['status'] == 'approved'
    assert inspected == [set(sources) | {'retained_chain_' + ref for ref in sources}]


def test_new_protocol_cumulative_draft_never_jumps_past_unknown(tmp_path):
    import json

    from test_analysis_selection_coverage import Execution, selection
    context, _ = selection(tmp_path)
    sources = section_analysis.numbered(context)
    execution, sent = Execution(), []
    execution.saved[analyze_job.REVIEW_PROTOCOL] = {'version': 3}
    execution.yield_if_due = lambda: None
    def model(system, user, **kwargs):
        sent.append(json.loads(user))
        if len(sent) == 2:
            raise ModelError('synthetic unknown', code='request_outcome_unknown')
        return {'operations': part()}
    llm = SimpleNamespace(json=model)
    result = analyze_job.draft_batches(execution, llm, 'system', {}, sources, context, ['operations'])
    assert result == {'operations': part()}
    assert analyze_job.draft_batches(execution, llm, 'system', {}, sources, context, ['operations']) == result
    assert len(sent) == 2
    assert execution.saved[analyze_job.COVERAGE]['remaining_fragments'] > 0


@pytest.mark.parametrize('code', ['model_balance_exhausted', 'model_rate_limited', 'project_paused'])
def test_global_errors_escape_independent_counter_boundary(host, code):
    research, _, sources = host
    enable(research.execution)
    def checked(system, payload):
        return reviewer(system, payload)
    def prepare(_):
        raise ModelError('synthetic global fault', code=code)
    checked.prepare = prepare
    with pytest.raises(ModelError) as caught:
        section_analysis.evaluate(research.execution, {'operations': part()}, sources, checked, lambda: None)
    assert caught.value.code == code


def test_shared_consistency_failure_cannot_publish_unchecked_peers(host):
    research, _, sources = host
    enable(research.execution)
    calls = []
    def checked(system, payload):
        calls.append(payload['phase'])
        if payload['phase'] == 'consistency':
            raise ModelError('synthetic unknown', code='request_outcome_unknown')
        return reviewer(system, payload)
    checked.prepare = lambda _: {'issues': [], 'receipts': []}
    raw = {'operations': part(), 'chain': part()}
    result = section_analysis.evaluate(research.execution, raw, sources, checked, lambda: None)
    assert all(result[n]['status'] == 'blocked' for n in raw)
    before = list(calls)
    assert section_analysis.evaluate(research.execution, {'operations': part(), 'chain': part()}, sources, checked, lambda: None) == result
    assert calls == before


def test_legacy_protocol_keeps_shared_counter_request(host):
    research, _, sources = host
    research.execution.checkpoint(analyze_job.REVIEW_PROTOCOL, {'version': 2})
    calls = []
    def checked(*_):
        pytest.fail('legacy shared counter should fail before supports')
    def prepare(payload):
        calls.append({field.split('.')[0] for field in payload['fields']})
        raise ModelError('legacy unknown', code='unknown_not_resent')
    checked.prepare = prepare
    with pytest.raises(ModelError):
        section_analysis.evaluate(research.execution, {'operations': part(), 'chain': part()}, sources, checked, lambda: None)
    assert calls == [{'operations', 'chain'}]


@pytest.mark.parametrize('version', [2, 3, 4])
@pytest.mark.parametrize('failure', ['model_content_rejected', 'model_output_incomplete'])
def test_repair_refusal_does_not_skip_later_section_receipts(host, monkeypatch, version, failure):
    research, context, _sources = host
    execution = research.execution
    execution.checkpoint(analyze_job.REVIEW_PROTOCOL, {'version': version})
    selected = {**context, 'coverage': {}}
    monkeypatch.setattr(analyze_job, 'select', lambda *_: selected)
    raw = {n: part() for n in section_analysis.SECTIONS}
    monkeypatch.setattr(analyze_job, 'draft_batches', lambda *_: deepcopy(raw))
    outcomes = {n: {'status': 'approved', 'refs': ['S1'], 'issues': []} for n in raw}
    monkeypatch.setattr(section_analysis, 'evaluate', lambda *_: deepcopy(outcomes))
    monkeypatch.setattr(analyze_job, '_normalization_problem', lambda generator, name, *args, **kwargs: 'repair' if name in ('position', 'operations') else None)
    attempts = []
    def repair(generator, execution, llm, raw, sources, outcomes, *args):
        name = next(iter(outcomes))
        attempts.append(name)
        if name == 'position':
            raise ModelError('synthetic unusable reply', code=failure)
        return outcomes
    monkeypatch.setattr(analyze_job, 'repair_summaries', repair)
    monkeypatch.setattr(section_analysis, 'publish', lambda *args, targets: {'published': targets})
    result = analyze_job.sections(research.pipeline, execution, SimpleNamespace(ready=True), selected)
    assert result['status'] == 'partial'
    assert set(result['published']) == set(raw) - {'position'}
    assert result['sections']['position']['issues'] == [failure]
    assert attempts == ['position', 'operations']
    assert all(execution.read_checkpoint('analyze_section_outcome_v1:' + n) for n in raw)
    analyze_job.sections(research.pipeline, execution, SimpleNamespace(ready=True), selected)
    assert attempts == ['position', 'operations']


@pytest.mark.parametrize('version', [3, 4])
def test_summary_repair_keeps_original_scalar_evidence_only_in_new_protocol(host, version):
    from project_watch.studio.review_source_traversal import cited_payload
    research, _, sources = host
    research.execution.checkpoint(analyze_job.REVIEW_PROTOCOL, {'version': version})
    sources['S2'] = {**sources['S1'], 'context': 'Synthetic route', 'title': 'Synthetic route'}
    candidate = dict(route='Synthetic route', maturity='advanced', consensus='forming', confidence='low',
                     summary='old unsupported summary', academic=[{'text': 'RECRUITING', 'refs': ['S1']}],
                     industry=[], risks=[])
    raw = {'technology': deepcopy(candidate)}
    outcomes = {'technology': dict(status='rejected', refs=['S1', 'S2'], dropped=[],
        blocking=[{'field': 'technology.summary', 'code': 'evidence_insufficient'}], repair_basis=candidate)}
    def checked(system, payload):
        if payload.get('phase') == 'consistency':
            return {'consistent': True, 'reason': 'synthetic', 'conflicts': []}
        available = cited_payload(payload)['evidence']
        return {'checks': [dict(field=f, status='supported' if not f.endswith('.route') or 'S2' in available else 'insufficient',
            reason='synthetic original source binding', evidence=[{'ref':'S2','quote':'Synthetic route'}]
            if f.endswith('.route') and 'S2' in available else [{'ref':'S1','quote':'RECRUITING'}]) for f in payload['fields']]}
    checked.prepare = lambda _: {'issues': [], 'receipts': []}
    llm = SimpleNamespace(ready=True, json=lambda *_: {'summary': '合成材料仍在招募阶段。'})
    result = analyze_job.repair_summaries(research.pipeline.insight_generator, research.execution, llm,
                                         raw, sources, outcomes, checked, lambda: None, None)
    assert result['technology']['status'] == ('approved' if version == 4 else 'rejected')
    if version == 4:
        assert raw['technology']['refs'] == ['S1', 'S2']


@pytest.mark.parametrize('version', [2, 3, 4])
def test_synthesis_instruction_does_not_change_legacy_requests(host, monkeypatch, version):
    from project_watch.studio import review_source_traversal as traversal
    research, _, _ = host
    research.execution.checkpoint(analyze_job.REVIEW_PROTOCOL, {'version': version})
    sent = []
    monkeypatch.setattr(traversal, 'run', lambda execution, llm, system, payload, guard: sent.append(system))
    checked = analyze_job._reviewer(research.pipeline, research.execution, None, lambda: None)
    checked('support', {'phase': 'fields', 'analysis': {}, 'evidence': {}})
    assert (analyze_job.SYNTHESIS_SUPPORT in sent[0]) == (version == 4)


def test_v4_review_contract_change_cannot_send_new_request(host):
    research, _, sources = host
    research.execution.checkpoint(analyze_job.REVIEW_PROTOCOL, {'version': 4})
    calls = []
    def checked(system, payload):
        calls.append(payload)
        return reviewer(system, payload)
    checked.contract = 'original support contract'
    checked.prepare = lambda _: {'issues': [], 'receipts': []}
    section_analysis.evaluate(research.execution, {'operations': part()}, sources, checked, lambda: None)
    before = len(calls)
    checked.contract = 'changed support contract'
    with pytest.raises(RuntimeError, match='contract changed'):
        section_analysis.evaluate(research.execution, {'operations': part()}, sources, checked, lambda: None)
    assert len(calls) == before


def test_new_counter_yields_after_checkpoint_without_replaying_model():
    import json

    from test_review_source_traversal import Journal, payload

    from project_watch.studio import review_source_traversal as traversal
    execution, calls = Journal(), []
    execution.saved[analyze_job.REVIEW_PROTOCOL] = {'version': 3}
    execution.yield_if_due = lambda: (_ for _ in ()).throw(JobYield())
    def model(system, user, **kwargs):
        body = json.loads(user)
        calls.append(user)
        return {'checked_fields': list(body['fields']), 'checks': []}
    llm = SimpleNamespace(json=model)
    while True:
        try:
            result = traversal.counterevidence(execution, llm, payload(), lambda: None)
            break
        except JobYield:
            assert len([k for k in execution.saved if k.startswith('review-counter:')]) == len(calls)
    assert result['issues'] == []
    assert len(calls) == len(set(calls)) == len(result['receipts'])


@pytest.mark.parametrize('key', [analyze_job.PUBLICATION, analyze_job.RESULT])
def test_current_analysis_partial_projects_partial(tmp_path, key):
    store, _, job = setup(tmp_path)
    JobExecution(store, job).checkpoint(key, {'status': 'partial'})
    store.finish(job['id'], lease_token=job['lease_token'])
    with closing(store._connect()) as db:
        assert projection(db, row(store, job))['state'] == 'partial'


def test_approved_without_publication_is_not_displayed_as_published(tmp_path):
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    execution.checkpoint(analyze_job.PUBLICATION, {'status': 'partial', 'sections': {}})
    execution.checkpoint('analyze_section_outcome_v1:operations', {'outcome': {'status': 'approved'}, 'published': False})
    with closing(store._connect()) as db:
        assert section_progress.states(db, row(store, job))[0]['operations'] == 'unfinished'
