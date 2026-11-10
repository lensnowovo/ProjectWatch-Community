"""Page identity, publication, replay and withdrawal on real isolated storage."""
from contextlib import closing

import pytest
import test_collection_ingest as fixtures
from test_collection_ingest import trial

from project_watch.studio import material_snapshot, section_analysis
from project_watch.studio import page_summaries as pages


@pytest.fixture
def host(tmp_path):
    research, unit, ingest = fixtures.host.__wrapped__(tmp_path)
    ingest(unit, [trial()])
    saved = material_snapshot.capture(research.execution)
    context = material_snapshot.read(research.store, research.project_id, saved['id'])
    return research, context, section_analysis.numbered(context)


def draft(ref='S1'):
    return dict(status='ready', sentences=[dict(text='Synthetic study 正在招募受试者。', refs=[ref]), dict(text='研究正处于入组阶段。', refs=[ref])], details=[dict(text='登记状态为 RECRUITING。', refs=[ref])])


def reviewer(system, payload):
    return dict(checks=[dict(field=f, status='supported', reason='synthetic verification',
                            evidence=[dict(ref='S1', quote='RECRUITING')]) for f in payload['fields']])


def apply(host, value=None, checker=reviewer):
    research, context, sources = host
    value = value if value is not None else draft()
    result = pages.evaluate('trials', value, sources, checker)
    pages.publish(research.execution, context, 'trials', value, sources, result)
    return result, pages.read(research.store, research.project_id)


def test_publication_is_page_specific_with_original_evidence(host):
    outcome, shown = apply(host)
    assert outcome['status'] == 'approved'
    assert set(shown) == {'trials'}
    assert shown['trials']['text'] == ''.join(r['text'] for r in draft()['sentences'])
    assert shown['trials']['refs'][0]['source_id'] == host[2]['S1']['evidence_version_id']
    assert not shown['trials']['historical']


def test_failed_or_empty_page_preserves_previous_publication(host):
    _, first = apply(host)
    outcome, second = apply(host, dict(status='no_material', sentences=[]))
    assert outcome['status'] == 'no_material' and second == first
    def denied(system, payload):
        response = reviewer(system, payload)
        response['checks'][0]['status'] = 'insufficient'
        return response
    outcome, third = apply(host, checker=denied)
    assert outcome['status'] == 'rejected' and third == first


def test_verbatim_quote_and_factual_support_required(host):
    def invalid(system, payload):
        response = reviewer(system, payload)
        response['checks'][0]['evidence'][0]['quote'] = 'invented efficacy result'
        return response
    assert apply(host, checker=invalid)[1] == {}
    def non_factual(system, payload):
        response = reviewer(system, payload)
        response['checks'][0].update(status='not_factual', evidence=[])
        return response
    assert apply(host, checker=non_factual)[1] == {}


def test_withdrawal_hides_summary_not_audit_history(host):
    apply(host)
    research, context, _ = host
    research.store.evidence.dependencies.withdraw(research.project_id,
        context['materials'][0]['evidence_version_id'], 'Synthetic withdrawal', actor='test')
    assert pages.read(research.store, research.project_id) == {}
    with closing(research.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_insight_versions WHERE artifact_key='page-summary:trials'").fetchone()[0] == 1


def test_replay_does_not_create_more_versions(host):
    _, first = apply(host)
    _, second = apply(host)
    assert first == second
    with closing(host[0].store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_insight_versions WHERE artifact_key='page-summary:trials'").fetchone()[0] == 1


def test_missing_ref_and_unknown_page_fail_closed(host):
    with pytest.raises(ValueError):
        pages.validate('papers', draft('N404'), host[2])
    with pytest.raises(ValueError):
        pages.validate('documents', draft(), host[2])


def test_reviewer_receives_different_content_contracts(host):
    contracts = []
    def check(system, payload):
        contracts.append(payload['page_contract'])
        assert payload['page_contract'] in system
        return reviewer(system, payload)
    for page in ('trials', 'papers', 'patents'):
        pages.evaluate(page, draft(), host[2], check)
    assert len(set(contracts)) == 3


def test_counterevidence_blocks_publication(host):
    def check(system, payload):
        return reviewer(system, payload)
    check.prepare = lambda payload: {'issues': [{'field': next(iter(payload['fields'])), 'code': 'contradicted'}]}
    assert apply(host, checker=check)[1] == {}


def test_complete_job_replays_publication_without_new_model_calls(host, monkeypatch):
    from types import SimpleNamespace
    research, context, _ = host
    calls = []
    def generate(*args, **kwargs):
        assert research.execution.allowed_call_kinds == frozenset({'model'})
        calls.append(1)
        return {'trials': draft()}
    monkeypatch.setattr(pages, 'select_context', lambda *args: context)
    monkeypatch.setattr(research.pipeline, 'tools', lambda **kw: (None, None, SimpleNamespace(json=generate), None))
    monkeypatch.setattr(pages.analyze_job, '_reviewer', lambda *args: reviewer)
    result = pages.run(research.pipeline, research.execution, targets=('trials',))
    assert result['published'] == ['trials']
    assert pages.run(research.pipeline, research.execution, targets=('trials',)) == result
    assert len(calls) == 1 and research.execution.allowed_call_kinds is None
    with pytest.raises(ValueError, match='targets changed'):
        pages.run(research.pipeline, research.execution, targets=('papers',))


@pytest.mark.parametrize('code', ['model_timeout', 'unknown_not_resent', 'model_output_incomplete'])
def test_page_failure_is_durable_and_other_page_is_published(host, monkeypatch, code):
    from types import SimpleNamespace

    from project_watch.studio.llm import ModelError
    research, context, _ = host
    calls = []
    def check(system, payload):
        calls.append(payload['page_contract'])
        if payload['page_contract'] == pages.CONTRACTS['trials']:
            raise ModelError('synthetic failure', code=code)
        return reviewer(system, payload)
    monkeypatch.setattr(pages, 'select_context', lambda *args: context)
    monkeypatch.setattr(research.pipeline, 'tools', lambda **kw: (None, None,
        SimpleNamespace(json=lambda *a, **k: {'trials': draft(), 'papers': draft()}), None))
    monkeypatch.setattr(pages.analyze_job, '_reviewer', lambda *args: check)
    result = pages.run(research.pipeline, research.execution, targets=('trials', 'papers'))
    assert result['status'] == 'partial' and result['failures'] == {'trials': code}
    assert 'papers' in result['published']
    assert set(pages.read(research.store, research.project_id)) == {'papers'}
    count = len(calls)
    assert pages.run(research.pipeline, research.execution, targets=('trials', 'papers')) == result
    assert len(calls) == count


def test_shared_draft_timeout_preserves_already_verified_page(host, monkeypatch):
    from types import SimpleNamespace

    from project_watch.studio.llm import ModelError
    research, context, sources = host
    calls = []
    def generate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ModelError('synthetic timeout', code='model_timeout')
        return {'trials': draft()}
    monkeypatch.setattr(pages, 'select_context', lambda *args: context)
    monkeypatch.setattr(pages.analyze_job, 'source_batches', lambda *args: iter([sources, sources]))
    monkeypatch.setattr(research.pipeline, 'tools', lambda **kw: (None, None, SimpleNamespace(json=generate), None))
    monkeypatch.setattr(pages.analyze_job, '_reviewer', lambda *args: reviewer)
    result = pages.run(research.pipeline, research.execution, targets=('trials',))
    assert result['status'] == 'partial' and result['draft_failure']['code'] == 'model_timeout'
    assert result['published'] == ['trials'] and pages.read(research.store, research.project_id)['trials']['text']
    assert pages.run(research.pipeline, research.execution, targets=('trials',)) == result
    assert len(calls) == 2


def test_flash_upgrade_preserves_old_unknown_wire_options(host):
    from project_watch.studio.http import Http
    from project_watch.studio.llm import LLM, ModelError, job_structured_options
    from project_watch.studio.settings import ModelConfig
    research, _, _ = host
    config = ModelConfig('openai', 'https://open.bigmodel.cn/api/coding/paas/v4', 'glm-5.3-flash', 'synthetic')
    sent = []
    def transport(*args):
        sent.append(1)
        raise TimeoutError('synthetic')
    http = research.execution.http(Http(transport), lambda url: ('model', 'synthetic'))
    legacy = LLM(config, http, json_options={})
    with pytest.raises((TimeoutError, ModelError)):
        legacy.json('s', 'u')
    options = job_structured_options(research.execution, config)
    assert options == {}
    with pytest.raises(ModelError) as exc:
        LLM(config, http, json_options=options).json('s', 'u')
    assert exc.value.code == 'unknown_not_resent'
    assert len(sent) == 1
    from synthetic_providers import legacy_job

    from project_watch.studio.execution import JobExecution
    with closing(research.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET kind='analyze', input_version='collect:legacy-batch', status='failed' WHERE id=?", (research.execution.job['id'],))
    next_job = legacy_job(research.store, research.project_id, 'analyze', status='running', input_version='collect:legacy-batch')
    resumed = JobExecution(research.store, next_job)
    options = job_structured_options(resumed, config)
    assert options == {}
    with pytest.raises(ModelError) as exc:
        LLM(config, resumed.http(Http(transport), lambda url: ('model', 'synthetic')), json_options=options).json('s', 'u')
    assert exc.value.code == 'unknown_not_resent'
    assert len(sent) == 1


def test_first_page_visible_before_later_batch_failure_and_resume(host, monkeypatch):
    from types import SimpleNamespace

    from project_watch.studio.llm import ModelError
    research, context, sources = host
    calls = []
    fail = [True]
    def generate(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            assert 'trials' in pages.read(research.store, research.project_id)
            if fail[0]:
                raise ModelError('synthetic outage', code='model_service_unavailable', status=503)
        return {'trials': draft()}
    monkeypatch.setattr(pages, 'select_context', lambda *args: context)
    monkeypatch.setattr(pages.analyze_job, 'source_batches', lambda *args: iter([sources, sources]))
    monkeypatch.setattr(research.pipeline, 'tools', lambda **kw: (None, None, SimpleNamespace(json=generate), None))
    monkeypatch.setattr(pages.analyze_job, '_reviewer', lambda *args: reviewer)
    with pytest.raises(ModelError):
        pages.run(research.pipeline, research.execution, targets=('trials',))
    shown = pages.read(research.store, research.project_id)
    assert shown['trials']['text']
    fail[0] = False
    result = pages.run(research.pipeline, research.execution, targets=('trials',))
    assert result['published'] == ['trials']
    assert len(calls) == 3  # first batch checkpoint was reused
    assert pages.read(research.store, research.project_id)['trials']['text'] == shown['trials']['text']


def test_early_page_still_checks_full_snapshot_counterevidence(host, monkeypatch):
    from types import SimpleNamespace
    research, context, sources = host
    inspected = []
    def check(system, payload):
        return reviewer(system, payload)
    def counter(payload):
        inspected.append(set(payload['evidence']))
        return {'issues': [{'field': next(iter(payload['fields'])), 'code': 'contradicted'}]}
    check.prepare = counter
    monkeypatch.setattr(pages, 'select_context', lambda *args: context)
    monkeypatch.setattr(research.pipeline, 'tools', lambda **kw: (None, None, SimpleNamespace(json=lambda *a, **k: {'trials': draft()}), None))
    monkeypatch.setattr(pages.analyze_job, '_reviewer', lambda *args: check)
    pages.run(research.pipeline, research.execution, targets=('trials',))
    assert inspected and all(refs == set(sources) for refs in inspected)
    assert pages.read(research.store, research.project_id) == {}


def test_older_job_cannot_change_newer_publication_or_dependencies(host, monkeypatch):
    from project_watch.studio.execution import JobExecution
    research, context, sources = host
    older = research.execution
    research.store.finish(older.job['id'], lease_token=older.job['lease_token'])
    queued = research.store.enqueue(research.project_id, 'page_summary', input_version='page:trials')
    # Claim only the synthetic successor; unrelated fixture jobs are not part of this test.
    with closing(research.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE id!=?", (queued['id'],))
    newer = JobExecution(research.store, research.store.claim())
    outcome = pages.evaluate('trials', draft(), sources, reviewer)
    pages.publish(newer, context, 'trials', draft(), sources, outcome)
    before = pages.read(research.store, research.project_id)
    monkeypatch.setattr(research.store.evidence.dependencies, 'track', lambda *a, **k: pytest.fail('older publication mutated dependencies'))
    earlier = draft()
    earlier['sentences'][1]['text'] = '研究处于招募阶段。'
    earlier_outcome = pages.evaluate('trials', earlier, sources, reviewer)
    with closing(research.store._connect()) as db, db:
        assert pages._publish(db, older, context, 'trials', earlier, sources, earlier_outcome) is False
    assert pages.read(research.store, research.project_id) == before


def test_request_failure_propagates_without_a_second_attempt(host, monkeypatch):
    from types import SimpleNamespace

    from project_watch.studio.http import HttpError
    research, context, _ = host
    calls = []
    def failed(*args, **kwargs):
        calls.append(1)
        raise HttpError('synthetic_unknown')
    monkeypatch.setattr(pages, 'select_context', lambda *args: context)
    monkeypatch.setattr(research.pipeline, 'tools', lambda **kw: (None, None, SimpleNamespace(json=failed), None))
    with pytest.raises(HttpError, match='synthetic_unknown'):
        pages.run(research.pipeline, research.execution, targets=('trials',))
    assert len(calls) == 1 and pages.read(research.store, research.project_id) == {}
    assert research.execution.allowed_call_kinds is None

def test_concise_body_and_details_have_separate_evidence_checks(host):
    fields = []
    def check(system, payload):
        fields.extend(payload['fields'])
        return reviewer(system, payload)
    outcome, shown = apply(host, checker=check)
    assert outcome['status'] == 'approved'
    assert 'trials.details.0.text' in fields
    assert shown['trials']['details'][0]['text'] == draft()['details'][0]['text']
    assert 'RECRUITING' not in shown['trials']['text']
    for value in [
        dict(status='ready', sentences=draft()['sentences'][:1]),
        dict(status='ready', sentences=[dict(text='x'*261, refs=['S1']), draft()['sentences'][1]]),
        dict(status='no_material', sentences=[], details=draft()['details']),
        {**draft(), 'details':[dict(text='Unsupported parameter', refs=['missing'])]},
    ]:
        with pytest.raises(ValueError):
            pages.validate('trials', value, host[2])


def test_legacy_checkpoint_is_not_replayed_under_new_protocol(host):
    research = host[0]
    research.execution.checkpoint('page-summary-v1:targets', ['trials'])
    with pytest.raises(ValueError, match='legacy summary request'):
        pages.run(research.pipeline, research.execution, targets=('trials',))


def test_internal_and_public_scopes_cannot_be_mixed(host):
    with pytest.raises(ValueError, match='visibility scope'):
        pages.run(host[0].pipeline, host[0].execution, targets=('ops',))

def test_internal_publication_permission_and_withdrawal(host):
    from project_watch.domain import FragmentInput
    research=host[0]
    repo=research.pipeline.repository
    source=repo.add_source(research.project_id,kind='document',reference='synthetic.txt',content_sha256='a'*64)
    repo.enqueue_intake(research.project_id,source.id)
    job=repo.claim_next_intake()
    repo.complete_intake(job.id,[FragmentInput(ordinal=1,locator='line 1',text='RECRUITING synthetic')])
    evidence=research.pipeline.evidence.from_fragments(repo,research.project_id,source.id,extraction_version='test')
    eid=evidence['id']
    context={'id':'internal-fixture','binding':'allowed','object_versions':{},'internal_sources':{eid:source.id}}
    sources={'S1':dict(title='Synthetic document',context='RECRUITING synthetic',text='RECRUITING synthetic',
                       evidence_version_id=eid,kind='internal',source_id=source.id)}
    outcome=pages.evaluate('ops',draft(),sources,reviewer)
    with closing(research.store._connect()) as db,db:
        db.execute('INSERT INTO studio_source_permissions VALUES (?,?,?,?,?)',
                   (source.id,research.project_id,'revoked','test','2026-11-05'))
    with pytest.raises(ValueError,match='permission changed'):
        pages.publish(research.execution,context,'ops',draft(),sources,outcome)
    with closing(research.store._connect()) as db,db:
        db.execute("UPDATE studio_source_permissions SET model_binding='allowed' WHERE source_id=?",(source.id,))
    assert pages.publish(research.execution,context,'ops',draft(),sources,outcome)
    assert pages.read(research.store,research.project_id)['ops']['refs'][0]['kind']=='internal'
    research.store.evidence.dependencies.withdraw(research.project_id,eid,'test withdrawal',actor='test')
    assert pages.read(research.store,research.project_id)=={}

def test_internal_job_uses_only_permitted_fragments(host, monkeypatch):
    from types import SimpleNamespace

    from project_watch.domain import FragmentInput
    research=host[0]
    repo=research.pipeline.repository
    source=repo.add_source(research.project_id,kind='document',reference='synthetic.txt',content_sha256='b'*64)
    repo.enqueue_intake(research.project_id,source.id)
    job=repo.claim_next_intake()
    repo.complete_intake(job.id,[FragmentInput(ordinal=1,locator='line 1',text='RECRUITING synthetic')])
    research.execution.job['kind']='internal_page_summary'
    def generate(system,payload,**kwargs):
        import json
        data=json.loads(payload)
        assert set(data['page_contracts'])=={'ops'}
        assert all(v['kind']=='internal' for v in data['materials'].values())
        assert 'Synthetic study' not in payload
        return {'ops':draft('F1')}
    def check(system,payload):
        return dict(checks=[dict(field=f,status='supported',reason='synthetic',
                   evidence=[dict(ref='F1',quote='RECRUITING')]) for f in payload['fields']])
    monkeypatch.setattr(research.pipeline,'tools',lambda **kw:(None,None,SimpleNamespace(json=generate),None))
    monkeypatch.setattr(pages.analyze_job,'_reviewer',lambda *args:check)
    result=pages.run(research.pipeline,research.execution,targets=('ops',))
    assert result['published']==['ops']

def test_sourced_candidate_survives_batch_without_page_material(host):
    sources = host[2]
    previous = {'trials': draft()}
    for update in ({}, {'trials': {'status': 'no_material', 'sentences': []}}, {'trials': draft('missing')}):
        assert pages.retain_candidates(previous, update, sources, ['trials']) == previous


def test_parameter_conflict_does_not_block_verified_body(host):
    def check(system, payload):
        response = reviewer(system, payload)
        response['checks'][-1]['status'] = 'contradicted'
        return response
    result, shown = apply(host, checker=check)
    assert shown['trials']['text'] == ''.join(r['text'] for r in draft()['sentences'])
    assert shown['trials']['details'] == [] and result['issues']


def test_only_independent_sentence_survives_missing_premise(host):
    value = draft()
    value['sentences'][1]['text'] = '另一项单独登记的研究仍在招募。'
    def check(system, payload):
        response = reviewer(system, payload)
        response['checks'][0]['status'] = 'contradicted'
        response['checks'][1]['standalone'] = True
        return response
    _, shown = apply(host, value, check)
    assert shown['trials']['text'] == value['sentences'][1]['text']
    assert shown['trials']['details'] == []

def test_batches_keep_earlier_revision_when_latest_review_fails(host, monkeypatch):
    from types import SimpleNamespace
    research, context, _ = host
    early = draft()
    late = draft()
    late['sentences'][0]['text'] = '另一项待验证的主张。'
    values = iter([{'trials': early}, {'trials': late}, {'trials': {'status': 'no_material', 'sentences': []}}])
    def check(system, payload):
        result = reviewer(system, payload)
        if late['sentences'][0]['text'] in payload['fields'].values():
            for row in result['checks']:
                row.update(status='insufficient', evidence=[])
        return result
    monkeypatch.setattr(pages, 'select_context', lambda *args: context)
    monkeypatch.setattr(pages.analyze_job, 'source_batches', lambda sources: [1, 2, 3])
    monkeypatch.setattr(research.pipeline, 'tools', lambda **kw: (None, None, SimpleNamespace(json=lambda *a, **kw: next(values)), None))
    monkeypatch.setattr(pages.analyze_job, '_reviewer', lambda *args: check)
    result = pages.run(research.pipeline, research.execution, targets=('trials',))
    assert result['published'] == ['trials']
    assert pages.read(research.store, research.project_id)['trials']['text'] == ''.join(r['text'] for r in early['sentences'])

def test_joint_counter_checks_distinct_claims_and_vetoes_repeated_claim(host):
    from collections import Counter
    candidates = []
    for amount in (1, 2, 3, 4):
        c = draft()
        c['sentences'][0]['text'] = '已完成交付。'
        c['sentences'][1]['text'] = f'本期收入为{amount}万元。'
        candidates.append(c)
    seen = Counter()
    scans = []
    def check(system, payload):
        result = reviewer(system, payload)
        for row in result['checks']:
            row['standalone'] = True
            if '.details.' in row['field']:
                row['status'] = 'insufficient'
        return result
    def counter(payload):
        issues = []
        scans.append(set(payload['fields'].values()))
        for field, text in payload['fields'].items():
            seen[text] += 1
            if text != '本期收入为4万元。':
                issues.append({'field':field, 'code':'evidence_contradicted'})
        return {'issues':issues}
    check.prepare = counter
    outputs = list(pages.grouped_outcomes('trials', candidates, host[2], check))
    assert [outcome['status'] for _, outcome in outputs] == ['rejected'] * 3 + ['approved']
    assert [row['text'] for row in outputs[-1][1]['selected']['sentences']] == ['本期收入为4万元。']
    assert outputs[-1][1]['selected']['details'] == []
    assert len(seen) == 5 and set(seen.values()) == {1}
    assert {'本期收入为3万元。','本期收入为4万元。'} in scans

def test_flash_options_follow_batch_and_do_not_leak_to_other_provider(host):
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.llm import job_structured_options
    from project_watch.studio.settings import ModelConfig
    research = host[0]
    execution = research.execution
    config = ModelConfig('openai', 'https://open.bigmodel.cn/api/coding/paas/v4', 'glm-5.3-flash', 'synthetic')
    execution.job.update(kind='analyze', input_version='collect:synthetic-batch')
    with closing(research.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET kind='analyze',input_version='collect:synthetic-batch' WHERE id=?", (execution.job['id'],))
    expected = job_structured_options(execution, config)
    assert expected == {'reasoning_effort':'low', 'json_object':True, 'stream':True}
    from synthetic_providers import legacy_job
    next_job = legacy_job(research.store, research.project_id, 'analyze', status='running', input_version='collect:synthetic-batch')
    with closing(research.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE id=?", (execution.job['id'],))
    resumed = JobExecution(research.store, next_job)
    assert job_structured_options(resumed, config) == expected
    other = ModelConfig('openai', 'https://synthetic.invalid', 'other', 'synthetic')
    assert job_structured_options(resumed, other) == {}
    assert job_structured_options(resumed, config) == expected

def test_review_permission_failure_is_not_a_page_model_failure():
    from project_watch.studio.analyze_job import _reviewer
    def revoked():
        raise ValueError('internal source permission changed')
    reviewer = _reviewer(None, None, None, revoked)
    with pytest.raises(RuntimeError, match='permission or snapshot changed'):
        reviewer.prepare({'fields': {'trials.sentences.0.summary': 'claim'}, 'analysis': {'trials': {'refs': ['S1']}}, 'evidence': {'S1': {'context': 'evidence'}}, 'page_contract': 'contract'})
