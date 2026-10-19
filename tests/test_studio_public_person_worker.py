"""Normal leased compiler/HTTP adapter, synthetic transport only."""
import json
from contextlib import closing
from datetime import UTC, datetime

import pytest

from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.llm import ModelError
from project_watch.studio.pipeline import Pipeline, StudioWorker
from project_watch.studio.store import StudioStore
from tests.test_studio_public_profile_atomic import facts, no_network, rig, run

__all__ = ['no_network', 'rig']

BODY = '合成研究公司甲任管理者，后来离职。管理人员名单还包括乙。'


def transport_setup(rig, monkeypatch, text):
    rig.pipeline.settings_store.update({'model': {'protocol': 'openai',
        'base_url': 'https://model.synthetic.example/v1', 'model': 'synthetic',
        'api_key': 'synthetic-never-real-key', 'max_output_tokens': 8000}})
    monkeypatch.setattr(rig.pipeline, 'tools', Pipeline.tools.__get__(rig.pipeline))
    monkeypatch.setattr(rig.store, 'list_items', lambda **_kw: [{
        'title': '合成研究公司', 'url': 'https://synthetic.example/people',
        'body': BODY, 'published_at': '2025-03-12'}])
    calls = []
    def transport(method, url, headers, body, timeout):
        assert method == 'POST' and url == 'https://model.synthetic.example/v1/chat/completions'
        calls.append(json.loads(body))
        raw = {'facts': [{'kind': 'person', 'label': '甲', 'text': text,
                         'refs': [{'ref': 'P1', 'quote': BODY}]}]}
        return Response(200, {}, json.dumps({'choices': [{'message': {
            'content': json.dumps(raw)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 40, 'completion_tokens': 20}}).encode())
    rig.pipeline.http_factory = lambda _settings: Http(transport)
    return calls


def test_worker_publishes_only_retained_person_clause_and_reopen_refs(rig, monkeypatch):
    calls = transport_setup(rig, monkeypatch, '甲曾任管理者并已离职，为首位职业经理人')
    job = rig.store.enqueue(rig.pid, 'public_profile')
    worker = StudioWorker(rig.pipeline)
    worker._last_schedule = worker._last_notify = datetime.now(UTC)
    assert worker.step()
    reopened = StudioStore(rig.path)
    saved = next(row for row in reopened.jobs(rig.pid) if row['id'] == job['id'])
    assert saved['status'] == 'done' and len(calls) == 1
    profile = reopened.get_public_profile(rig.pid)
    assert [row['text'] for row in profile['facts']] == ['甲曾任管理者并已离职']
    assert profile['outcome']['status'] == 'partial'
    assert JobExecution(reopened, saved).read_checkpoint('public_profile_outcome') == profile['outcome']
    assert '首位' not in str(facts(rig))
    for ref in profile['facts'][0]['refs']:
        part = reopened.evidence.get(rig.pid, ref['evidence_version_id'])['parts'][ref['part_index']]
        assert part['text'][ref['start']:ref['end']] == ref['quote']


def test_all_unsupported_http_result_retains_last_good_profile(rig, monkeypatch):
    run(rig)
    before, old = rig.store.get_public_profile(rig.pid), facts(rig)
    # The unleased fixture leaves its seed job queued; complete that seed only.
    with closing(rig.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE project_id=?", (rig.pid,))
    calls = transport_setup(rig, monkeypatch, '甲是首任总经理')
    rig.store.enqueue(rig.pid, 'public_profile')
    job = rig.store.claim()
    with pytest.raises(ModelError) as error:
        rig.pipeline.run(job)
    assert error.value.code == 'public_profile_no_output'
    after = rig.store.get_public_profile(rig.pid)
    assert {k: v for k, v in after.items() if k != 'dependency'} == {
        k: v for k, v in before.items() if k != 'dependency'}
    # Newly supplied evidence correctly makes the previous profile stale, but
    # does not overwrite its facts with an empty or unsupported profile.
    assert after['dependency']['status'] == 'stale' and facts(rig) == old
    assert JobExecution(rig.store, job).read_checkpoint('public_profile_outcome')['status'] == 'rejected'
    assert len(calls) == 1


def test_completed_person_response_replays_after_crash_without_second_http(rig, monkeypatch):
    calls = transport_setup(rig, monkeypatch, '甲曾任管理者并已离职，为首位职业经理人')
    rig.store.enqueue(rig.pid, 'public_profile')
    job = rig.store.claim()
    original = JobExecution.checkpoint
    def crash(self, stage, data):
        if stage == 'public_profile_outcome':
            raise KeyboardInterrupt('synthetic crash')
        return original(self, stage, data)
    monkeypatch.setattr(JobExecution, 'checkpoint', crash)
    with pytest.raises(KeyboardInterrupt):
        rig.pipeline.run(job)
    assert not facts(rig)
    with closing(rig.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?", (job['id'],))
    reopened = StudioStore(rig.path)
    retry = reopened.claim()
    assert retry['id'] == job['id'] and retry['lease_token'] != job['lease_token']
    monkeypatch.setattr(JobExecution, 'checkpoint', original)
    rig.pipeline.run(retry)
    assert len(calls) == 1
    assert JobExecution(reopened, retry).usage()['model'] == {
        'requests': 1, 'completed': 1, 'unknown': 0, 'cache_hits': 1}
    assert reopened.get_public_profile(rig.pid)['facts'][0]['text'] == '甲曾任管理者并已离职'
