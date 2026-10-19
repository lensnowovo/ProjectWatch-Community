"""Synthetic outcomes/worker controls; no semantic accuracy claim or provider calls."""
import json
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.llm import ModelError
from project_watch.studio.pipeline import Pipeline, StudioWorker
from project_watch.studio.public_profile import build_public_profile
from project_watch.studio.store import StudioStore
from tests.test_studio_public_profile_atomic import BODY, URL, facts, no_network, rig, run

__all__ = ['no_network', 'rig']


def fact(**changes):
    return {'kind': 'person', 'label': '甲', 'text': '甲任研发负责人', 'date': '',
            'refs': [{'ref': 'P1', 'quote': BODY}], **changes}


def compile_rows(rows, *, ready=True, hits=None):
    state = {'calls': 0}
    class Model:
        def __init__(self):
            self.ready = ready
        def json(self, *_args, **_kwargs):
            state['calls'] += 1
            return rows
    result = build_public_profile({'name': '合成研究公司', 'legal_name': '合成研究公司'},
        SimpleNamespace(has_search=False), Model(), hits if hits is not None else
        [{'title': '合成研究公司', 'url': URL, 'body': BODY, 'published_at': '2025-03-12'}])
    return result, state


@pytest.mark.parametrize('bad,reason', [
    (None, 'invalid_row'), (fact(kind='unsupported'), 'invalid_kind'),
    (fact(kind=[]), 'invalid_kind'),
    (fact(text=123), 'invalid_text'), (fact(text=''), 'invalid_text'),
    (fact(refs=[]), 'no_supported_reference'),
    (fact(refs=[{'ref': 'P2', 'quote': BODY}]), 'no_supported_reference'),
    (fact(text='甲持股87%'), 'unsupported_numeric_claim'),
])
def test_one_bad_row_preserves_sibling_and_explicit_diagnostics(bad, reason):
    rows = {'facts': [bad, fact()]}
    before = deepcopy(rows)
    result, state = compile_rows(rows)
    assert len(result['facts']) == state['calls'] == 1
    outcome = result['outcome']
    assert outcome['status'] == 'partial' and outcome['retained_count'] == 1
    assert outcome['rejected_rows'] == [{'id': 'F01', 'reason': reason}]
    assert rows == before
    assert 'raw' not in str(outcome) and '甲任研发负责人' not in str(outcome)


@pytest.mark.parametrize('rows,status', [
    ({'facts': []}, 'empty_extraction'), ({}, 'invalid_output'),
    ({'facts': None}, 'invalid_output'), ([], 'invalid_output'),
    ({'facts': [fact(refs=[])]}, 'rejected'),
])
def test_empty_invalid_and_all_rejected_are_distinct(rows, status):
    result, state = compile_rows(rows)
    assert result['facts'] == [] and state['calls'] == 1
    assert result['outcome']['status'] == status


def test_no_sources_and_unconfigured_model_are_not_empty_extraction():
    first, calls = compile_rows({'facts': [fact()]}, hits=[])
    second, other = compile_rows({'facts': [fact()]}, ready=False)
    assert first['outcome']['status'] == 'no_sources'
    assert second['outcome']['status'] == 'model_unavailable'
    assert calls['calls'] == other['calls'] == 0


def test_changed_date_is_recorded_without_claiming_fact_verified():
    result, _ = compile_rows({'facts': [fact(date='2025-03-12')]})
    assert result['facts'][0]['date'] == ''
    assert result['outcome']['changed_rows'] == [{'id': 'F01', 'reasons': ['occurrence_date_omitted']}]
    assert result['outcome']['status'] == 'partial'


def test_partial_numeric_clause_is_recorded():
    result, _ = compile_rows({'facts': [fact(text='甲任研发负责人；甲持股87%。')]})
    assert result['facts'][0]['text'] == '甲任研发负责人'
    assert result['outcome']['changed_rows'] == [{'id': 'F01', 'reasons': ['numeric_clauses_omitted']}]


def test_output_overflow_is_bounded_and_not_complete():
    result, _ = compile_rows({'facts': [fact()] * 33})
    assert len(result['facts']) == 30
    assert result['outcome']['overflow_count'] == 3 and result['outcome']['status'] == 'partial'


def test_complete_does_not_mean_semantically_verified():
    result, _ = compile_rows({'facts': [fact(text='错误地称甲已离职')]})
    assert result['outcome']['status'] == 'complete'
    assert result['outcome']['semantic_verification'] == 'not_proven'


@pytest.mark.parametrize('old_profile', [False, True])
def test_pipeline_empty_preserves_last_good_profile_and_does_not_create_first(rig, old_profile):
    if old_profile:
        run(rig)
    before = rig.store.get_public_profile(rig.pid)
    old_facts = facts(rig)
    brief_ids = [j['id'] for j in rig.store.jobs(rig.pid) if j['kind'] == 'brief']
    rig.state['texts'] = []
    with pytest.raises(ModelError, match='本轮未整理出') as error:
        run(rig)
    assert error.value.code == 'public_profile_no_output'
    assert rig.store.get_public_profile(rig.pid) == before
    assert facts(rig) == old_facts
    assert [j['id'] for j in rig.store.jobs(rig.pid) if j['kind'] == 'brief'] == brief_ids


def test_leased_empty_result_is_recorded_and_reopens_without_model_resend(rig, monkeypatch):
    calls = []
    class Empty:
        ready = True
        def json(self, *_args, **_kwargs):
            calls.append('called')
            return {'facts': []}
    monkeypatch.setattr(rig.pipeline, 'tools', lambda: (None, None, Empty(), SimpleNamespace(has_search=False)))
    job = rig.store.enqueue(rig.pid, 'public_profile')
    worker = StudioWorker(rig.pipeline)
    worker._last_schedule = datetime.now(UTC)
    worker._last_notify = datetime.now(UTC)
    assert worker.step() is True
    reopened = StudioStore(rig.path)
    saved = next(j for j in reopened.jobs(rig.pid) if j['id'] == job['id'])
    assert saved['status'] == 'failed' and '本轮未整理出' in saved['error']
    execution = JobExecution(reopened, saved)
    assert execution.read_checkpoint('public_profile_outcome')['status'] == 'empty_extraction'
    assert execution.read_checkpoint('dispatch_completed') is None
    assert reopened.get_public_profile(rig.pid) is None
    assert worker.step() is False and calls == ['called']
    assert reopened.missing_public_profile_projects() == []


def test_valid_partial_profile_persists_diagnostics_and_exact_refs(rig, monkeypatch):
    class Partial:
        ready = True
        def json(self, *_args, **_kwargs):
            return {'facts': [fact(refs=[]), fact()]}
    monkeypatch.setattr(rig.pipeline, 'tools', lambda: (None, None, Partial(), SimpleNamespace(has_search=False)))
    run(rig)
    reopened = StudioStore(rig.path)
    profile = reopened.get_public_profile(rig.pid)
    assert profile['outcome']['status'] == 'partial' and len(facts(rig)) == 1
    for ref in profile['facts'][0]['refs']:
        part = reopened.evidence.get(rig.pid, ref['evidence_version_id'])['parts'][ref['part_index']]
        assert part['text'][ref['start']:ref['end']] == ref['quote']


def real_client_with_synthetic_transport(rig, monkeypatch, *, unknown=False):
    rig.pipeline.settings_store.update({'model': {'protocol': 'openai',
        'base_url': 'https://model.synthetic.example/v1', 'model': 'synthetic',
        'api_key': 'synthetic-never-real-key', 'max_output_tokens': 8000}})
    monkeypatch.setattr(rig.pipeline, 'tools', Pipeline.tools.__get__(rig.pipeline))
    calls = []
    def transport(method, url, headers, body, timeout):
        assert method == 'POST' and url == 'https://model.synthetic.example/v1/chat/completions'
        calls.append(json.loads(body))
        if unknown:
            raise HttpError('connection_failed')
        return Response(200, {'set-cookie': 'synthetic-do-not-save'}, json.dumps({
            'choices': [{'message': {'content': '{"facts":[]}'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 30, 'completion_tokens': 5}}).encode())
    rig.pipeline.http_factory = lambda _settings: Http(transport)
    return calls


def test_completed_empty_http_response_replays_after_crash_without_rebilling(rig, monkeypatch):
    calls = real_client_with_synthetic_transport(rig, monkeypatch)
    rig.store.enqueue(rig.pid, 'public_profile')
    first = rig.store.claim()
    original_checkpoint = JobExecution.checkpoint
    def crash_before_outcome(self, stage, data):
        if stage == 'public_profile_outcome':
            raise KeyboardInterrupt('synthetic crash after paid response')
        return original_checkpoint(self, stage, data)
    monkeypatch.setattr(JobExecution, 'checkpoint', crash_before_outcome)
    with pytest.raises(KeyboardInterrupt):
        rig.pipeline.run(first)
    with closing(rig.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?", (first['id'],))
    reopened = StudioStore(rig.path)
    second = reopened.claim()
    assert second['id'] == first['id'] and second['lease_token'] != first['lease_token']
    monkeypatch.setattr(JobExecution, 'checkpoint', original_checkpoint)
    with pytest.raises(ModelError) as error:
        rig.pipeline.run(second)
    assert error.value.code == 'public_profile_no_output'
    execution = JobExecution(reopened, second)
    assert execution.read_checkpoint('public_profile_outcome')['status'] == 'empty_extraction'
    assert execution.usage()['model'] == {'requests': 1, 'completed': 1, 'unknown': 0, 'cache_hits': 1}
    assert len(calls) == 1
    assert reopened.usage()[0]['calls'] == 1
    with closing(reopened._connect()) as db:
        rows = str([tuple(r) for r in db.execute('SELECT * FROM studio_job_calls')])
    assert 'synthetic-never-real-key' not in rows and 'synthetic-do-not-save' not in rows
    assert reopened.get_public_profile(rig.pid) is None and facts(rig) == []


def test_unknown_http_outcome_is_held_without_result_checkpoint_or_retry(rig, monkeypatch):
    calls = real_client_with_synthetic_transport(rig, monkeypatch, unknown=True)
    rig.store.enqueue(rig.pid, 'public_profile')
    job = rig.store.claim()
    with pytest.raises(ModelError) as error:
        rig.pipeline.run(job)
    assert error.value.code == 'request_outcome_unknown'
    rig.store.finish(job['id'], error=str(error.value), lease_token=job['lease_token'])
    reopened = StudioStore(rig.path)
    execution = JobExecution(reopened, job)
    assert execution.usage()['model']['unknown'] == 1
    assert execution.read_checkpoint('public_profile_outcome') is None
    assert reopened.claim() is None and len(calls) == 1
    assert reopened.get_public_profile(rig.pid) is None


def test_successful_core_fixture_uses_given_public_quote_not_an_invented_fact():
    from project_watch.studio.public_profile import SYSTEM
    from tests.core_without_insight_support import material_model
    response = material_model(lambda *_args: {'facts': []})(
        SYSTEM, '研究主体：合成研究公司\n\n[P2] 合成公告 2025-01-01\n合成研究公司发布合成进展。仅用于测试。')
    assert response['facts'][0]['refs'] == [{'ref': 'P2', 'quote': '合成研究公司发布合成进展'}]
    assert response['facts'][0]['text'] == '合成研究公司发布合成进展'
    assert material_model(lambda *_args: {})(SYSTEM, '没有给来源') == {'facts': []}
