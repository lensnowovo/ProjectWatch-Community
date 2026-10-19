from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_metric_series import SOURCE, observation

from project_watch.api import create_app
from project_watch.monitor_briefing import build_briefing, is_publishable_update


def rows():
    return [observation('old', '2024', '80', basis='actual'),
            observation('new', '2025', '100', basis='actual')]


@pytest.mark.parametrize('patch', [
    {'project_id': 'other'}, {'change_kind': 'file_received'},
    {'status': 'withdrawn'}, {'status': 'rejected'}, {'evidence_refs': []},
])
def test_publish_gate_rejects_non_changes(patch):
    item = dict(project_id='p1', view='internal', headline='change',
                change_kind='financial', status='confirmed', evidence_refs=['ref'])
    assert is_publishable_update(item, 'p1')
    assert not is_publishable_update({**item, **patch}, 'p1')


def test_financial_change_is_exact_located_and_stable():
    result = build_briefing('p1', rows(), [SOURCE])
    assert result == build_briefing('p1', list(reversed(rows())), [SOURCE])
    item = result['items'][0]
    assert item['headline'] == '营业收入较上年增加 25.00%'
    assert item['comparison']['previous'] == '80'
    assert item['comparison']['current'] == '100'
    assert item['comparison']['absolute'] == '20'
    assert len(item['evidence_refs']) == 2
    assert item['evidence_refs'][0]['source_sha256'] == 'a' * 64
    assert item['ai_interpretation'] is None
    assert item['occurred_at'] is None
    assert item['unknowns']


@pytest.mark.parametrize('patch', [
    {'status': 'rejected'}, {'status': 'candidate'}, {'project_id': 'p2'},
    {'basis': 'forecast'}, {'scope': '单体'}, {'unit': '元'},
    {'source_locator': ''}, {'period': '2026'},
])
def test_noncomparable_values_do_not_produce_highlights(patch):
    before, after = rows()
    assert build_briefing('p1', [before, replace(after, **patch)], [SOURCE])['items'] == []


def test_conflict_missing_source_and_zero_change_are_not_highlights():
    before, after = rows()
    assert not build_briefing('p1', [before, after, replace(after, id='conflict')], [SOURCE])['items']
    assert not build_briefing('p1', rows(), [])['items']
    assert not build_briefing('p1', [before, replace(after, number=before.number)], [SOURCE])['items']
    assert not build_briefing('other', rows(), [SOURCE])['items']


def test_endpoint_empty_is_not_a_no_risk_claim(tmp_path):
    client = TestClient(create_app(tmp_path / 'watch.sqlite3'))
    project = client.post('/api/projects', json={'name': 'Synthetic Briefing'}).json()
    path = f"/api/workspace/projects/{project['id']}/briefing"
    result = client.get(path)
    assert result.status_code == 200
    assert result.json()['items'] == []
    assert result.json()['coverage']['financial'] == 'no_comparable_change'
    assert client.get('/api/workspace/projects/missing/briefing').status_code == 404


def test_watch_change_requires_current_accepted_version_and_new_live_evidence():
    before, after = rows()
    watch = SimpleNamespace(id='w', project_id='p1', status='watching', version=3,
                            observation_id='old', current_observation_id='new', title='客户回款')
    change = SimpleNamespace(id='c', project_id='p1', watch_item_id='w', status='accepted',
                             base_version=2, evidence_observation_id='new', target_status='watching',
                             updated_at=after.created_at, reason='已取得新的客户资料')

    def result(w=watch, c=change, observations=None):
        return [i for i in build_briefing('p1', observations or [before, after], [SOURCE],
                                         [w], [c])['items'] if i['change_kind'] == 'watch_evidence']

    assert len(result()) == 1
    for patch in ({'project_id': 'p2'}, {'status': 'candidate'}, {'base_version': 1},
                  {'evidence_observation_id': 'old'}, {'evidence_observation_id': None}):
        assert not result(c=SimpleNamespace(**{**vars(change), **patch}))
    assert not result(w=SimpleNamespace(**{**vars(watch), 'status': 'rejected'}))
    assert not result(observations=[before, replace(after, status='rejected')])
    prior = SimpleNamespace(**{**vars(change), 'id': 'prior', 'base_version': 1})
    assert not [i for i in build_briefing('p1', [before, after], [SOURCE], [watch],
                                        [prior, change])['items'] if i['change_kind'] == 'watch_evidence']


def test_api_comparison_survives_restart_and_recomputes_with_review(tmp_path):
    path = tmp_path / 'watch.sqlite3'
    client = TestClient(create_app(path))
    project = client.post('/api/projects', json={'name': 'Synthetic Annual'}).json()
    root = f"/api/projects/{project['id']}"
    source = client.post(f'{root}/sources', json=dict(kind='document', reference='synthetic.txt',
                                                     content_sha256='a'*64)).json()
    created = []
    for period, number in [('2024', '80'), ('2025', '100')]:
        o = client.post(f'{root}/observations', json=dict(source_id=source['id'], subject='财务表现',
            statement='营业收入', number=number, unit='万元', period=period, scope='合并',
            basis='actual', source_locator=f'line {period}')).json()
        created.append(o)
        response = client.post(f"/api/observations/{o['id']}/confirm",
                               json=dict(reviewer='synthetic-reviewer', expected_version=1))
        assert response.status_code == 200, response.text
    endpoint = f"/api/workspace/projects/{project['id']}/briefing"
    first = client.get(endpoint).json()
    assert first['items'][0]['comparison']['percent'] == '25.00'
    restarted = TestClient(create_app(path))
    assert restarted.get(endpoint).json() == first
    # Existing review decisions are immutable; the projection must not bypass this.
    response = restarted.post(f"/api/observations/{created[-1]['id']}/reject",
                              json=dict(reviewer='synthetic-reviewer', expected_version=2))
    assert response.status_code == 409, response.text
    conflicting = restarted.post(f'{root}/observations', json=dict(source_id=source['id'],
        subject='财务表现', statement='营业收入', number='110', unit='万元', period='2025',
        scope='合并', basis='actual', source_locator='line conflict')).json()
    accepted = restarted.post(f"/api/observations/{conflicting['id']}/confirm",
                             json=dict(reviewer='synthetic-reviewer', expected_version=1))
    assert accepted.status_code == 200, accepted.text
    assert restarted.get(endpoint).json()['items'] == []
