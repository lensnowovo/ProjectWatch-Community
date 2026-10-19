"""Synthetic transaction controls, not model entailment/accuracy tests."""
import socket
from contextlib import closing
from types import SimpleNamespace

import pytest

from project_watch.repository import ProjectRepository
from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart
from project_watch.studio.pipeline import Pipeline
from project_watch.studio.settings import SettingsStore
from project_watch.studio.store import StudioStore

BODY = '合成研究公司开发光学模组。甲任研发负责人。乙任市场负责人。'
URL = 'https://synthetic.example/company'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('atomic public profile controls remain offline')
    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic.sqlite3'
    repo = ProjectRepository(path)
    project = repo.create_project('合成研究公司')
    store = StudioStore(path)
    store.upsert_project(project.id, {'legal_name': '合成研究公司'}, consent=False)
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / 'unused-settings.json'))
    state = {'texts': ['开发光学模组'], 'hook': lambda: None}

    class Model:
        ready = True
        def json(self, *args, **kwargs):
            state['hook']()
            return {'facts': [{'kind': 'person' if '负责人' in text else 'business', 'text': text,
                              'refs': [{'ref': 'P1', 'quote': BODY}]} for text in state['texts']]}

    monkeypatch.setattr(pipeline, 'tools', lambda: (None, None, Model(), SimpleNamespace(has_search=False)))
    monkeypatch.setattr(store, 'list_items', lambda **kwargs: [{'title': '合成研究公司', 'url': URL,
                           'body': BODY, 'published_at': '2025-03-12'}])
    return SimpleNamespace(path=path, repo=repo, pid=project.id, store=store, pipeline=pipeline, state=state)


def run(rig):
    rig.pipeline.run_public_profile(rig.store.enqueue(rig.pid, 'public_profile'))


def facts(rig):
    with closing(rig.store._connect()) as db:
        return [row[0] for row in db.execute('SELECT record FROM studio_fact_records ORDER BY id')]


def revise(rig, *, source_key='public-profile:' + URL, visibility='public', project_id=None):
    return rig.store.evidence.save(EvidenceInput(project_id=project_id or rig.pid,
        source_key=source_key, source_type='search_summary', extraction_version='synthetic-revision',
        visibility=visibility, read_scope='summary', parts=[EvidencePart(locator_kind='stored_text',
        locator='synthetic', text='合成研究公司新材料：负责人已变更。')]))


@pytest.mark.parametrize('change', ['revised', 'withdrawn', 'expanded'])
def test_late_profile_cannot_add_facts_when_input_changed(rig, change):
    run(rig)
    before_profile = rig.store.get_public_profile(rig.pid)['facts']
    before_facts = facts(rig)
    before_briefs = [j['id'] for j in rig.store.jobs(rig.pid) if j['kind'] == 'brief']
    rig.state['texts'] = ['甲任研发负责人']
    def change_during_model():
        if change == 'withdrawn':
            old = before_profile[0]['refs'][0]['evidence_version_id']
            rig.store.evidence.dependencies.withdraw(rig.pid, old, 'synthetic withdrawal', actor='user')
        else:
            revise(rig, source_key='public-profile:' + URL if change == 'revised' else 'public-profile:https://synthetic.example/new')
    rig.state['hook'] = change_during_model
    run(rig)
    assert rig.store.get_public_profile(rig.pid)['facts'] == before_profile
    assert facts(rig) == before_facts
    assert [j['id'] for j in rig.store.jobs(rig.pid) if j['kind'] == 'brief'] == before_briefs
    assert rig.store.get_public_profile(rig.pid)['dependency']['status'] == 'stale'


def test_first_stale_generation_does_not_create_subject_or_facts(rig):
    rig.state['hook'] = lambda: revise(rig)
    run(rig)
    assert facts(rig) == []
    assert rig.pipeline.objects.page(rig.pid) == []
    assert rig.store.get_public_profile(rig.pid) is None
    assert not [j for j in rig.store.jobs(rig.pid) if j['kind'] == 'brief']


def test_second_fact_failure_rolls_back_first_fact_and_profile(rig, monkeypatch):
    run(rig)
    before = facts(rig)
    old = rig.store.get_public_profile(rig.pid)['facts']
    rig.state['texts'] = ['甲任研发负责人', '乙任市场负责人']
    real = rig.pipeline.objects.save_fact
    def reject_second(project_id, item, **kwargs):
        if item.text.startswith('乙'):
            raise ValueError('synthetic second fact failure')
        return real(project_id, item, **kwargs)
    monkeypatch.setattr(rig.pipeline.objects, 'save_fact', reject_second)
    with pytest.raises(ValueError, match='synthetic second fact failure'):
        run(rig)
    assert facts(rig) == before
    assert rig.store.get_public_profile(rig.pid)['facts'] == old


def test_profile_sql_failure_rolls_back_facts_and_new_subject(rig):
    with closing(rig.store._connect()) as db, db:
        db.execute("CREATE TRIGGER synthetic_reject_profile BEFORE INSERT ON studio_public_profile "
                   "BEGIN SELECT RAISE(ABORT,'synthetic profile failure'); END")
    with pytest.raises(Exception, match='synthetic profile failure'):
        run(rig)
    assert facts(rig) == []
    assert rig.pipeline.objects.page(rig.pid) == []
    assert rig.store.get_public_profile(rig.pid) is None


@pytest.mark.parametrize('change', ['none', 'internal', 'unrelated_public', 'other_project'])
def test_current_or_unrelated_generation_preserves_normal_fact_and_date_refs(rig, change):
    if change == 'internal':
        rig.state['hook'] = lambda: revise(rig, source_key='synthetic-private', visibility='internal')
    elif change == 'unrelated_public':
        rig.state['hook'] = lambda: revise(rig, source_key='other-research-task:synthetic')
    elif change == 'other_project':
        other = rig.repo.create_project('另一合成公司')
        rig.store.upsert_project(other.id, {}, consent=False)
        rig.state['hook'] = lambda: revise(rig, project_id=other.id)
    rig.state['texts'] = ['甲任研发负责人', '乙任市场负责人']
    run(rig)
    profile = rig.store.get_public_profile(rig.pid)
    assert profile['dependency']['status'] == 'current'
    assert len(profile['facts']) == len(facts(rig)) == 2
    stored = rig.pipeline.objects.facts(rig.pid, rig.pipeline.objects.ensure_subject(rig.pid)['id'])
    assert {f['id'] for f in stored} == {f['fact_record_id'] for f in profile['facts']}
    assert all([r['part_index'] for r in f['refs']] == [0, 1] for f in stored)


def test_identical_successful_retry_is_fact_idempotent(rig):
    run(rig)
    before = facts(rig)
    run(rig)
    assert facts(rig) == before


def test_explicit_fresh_round_can_publish_after_stale_result(rig):
    rig.state['hook'] = lambda: revise(rig, source_key='public-profile:https://synthetic.example/new')
    run(rig)
    assert facts(rig) == []
    assert rig.store.evidence.dependencies.get(rig.pid, 'public_profile')['status'] == 'stale'
    rig.state['hook'] = lambda: None
    run(rig)
    reopened = StudioStore(rig.path)
    assert reopened.get_public_profile(rig.pid)['dependency']['status'] == 'current'
    assert len(facts(rig)) == 1


def test_fact_and_profile_share_the_guarded_write_connection(rig, monkeypatch):
    connection = []
    original_profile = rig.store.put_compiled_profile
    original_fact = rig.pipeline.objects.save_fact
    def captured_profile(*args, **kwargs):
        callback = kwargs['on_current']
        def captured_callback(db):
            assert db.in_transaction
            connection.append(db)
            callback(db)
        return original_profile(*args, **{**kwargs, 'on_current': captured_callback})
    def captured_fact(*args, **kwargs):
        assert kwargs.get('db') is connection[0]
        return original_fact(*args, **kwargs)
    monkeypatch.setattr(rig.store, 'put_compiled_profile', captured_profile)
    monkeypatch.setattr(rig.pipeline.objects, 'save_fact', captured_fact)
    run(rig)
    assert len(connection) == len(facts(rig)) == 1
