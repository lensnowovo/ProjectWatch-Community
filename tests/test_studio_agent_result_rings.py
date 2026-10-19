"""Four-ring Agent output parity; synthetic data only."""
import copy
import hashlib
import json

import pytest
from test_studio_agent_results import payload, route
from test_studio_application_parity import headers, setup_app


@pytest.mark.parametrize('field,verdict', [('operations', 'improving'), ('chain', 'favorable')])
def test_ring_roundtrip_history_and_no_background_jobs(tmp_path, field, verdict):
    app, web, pid, grant = setup_app(tmp_path)
    app.state.studio_store.set_note(pid, 'Human note stays')
    body = payload(app.state.studio_store.get_project(pid)['config_version'])
    body['sources'].append({'id': 'R2', 'kind': 'public', 'title': 'Ring-only source',
                            'url': 'https://other.example/update', 'text': 'Synthetic new evidence'})
    body['deep'][field] = {'verdict': verdict, 'confidence': 'high', 'summary': 'Synthetic observation',
                           'supporting': [{'text': 'Synthetic factory opened', 'refs': ['R2']}],
                           'against': [], 'change_if': ['New evidence']}
    response = web.post(route(pid), headers=headers(grant), json=body)
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt['job_status'] == 'not_requested'
    assert web.post(route(pid), headers=headers(grant), json=body).json() == receipt
    result = web.get(f'/api/studio/projects/{pid}/insight').json()['record']['analysis']
    assert result[field]['verdict'] == verdict
    assert result[field]['confidence'] == 'low'  # A single URL cannot create high confidence.
    assert result[field]['for'][0]['refs'][0]['url'] == body['sources'][1]['url']
    assert any(r['code'] == 'R2' for r in result['cited'])
    assert len(web.get(route(pid), headers=headers(grant)).json()['items']) == 1
    assert not app.state.studio_store.jobs(pid)
    assert app.state.studio_store.get_project(pid)['note'] == 'Human note stays'


@pytest.mark.parametrize('field', ['operations', 'chain'])
def test_ring_evidence_gate_and_unknown_reference_atomicity(tmp_path, field):
    _, web, pid, grant = setup_app(tmp_path)
    body = payload()
    body['deep'][field] = {'verdict': 'steady', 'confidence': 'high'}
    response = web.post(route(pid), headers=headers(grant), json=body)
    assert response.status_code == 201, response.text
    ring = web.get(f'/api/studio/projects/{pid}/insight').json()['record']['analysis'][field]
    assert (ring['verdict'], ring['confidence']) == ('unclear', 'low')
    changed = copy.deepcopy(body)
    changed.update(expected_version=response.json()['version'], idempotency_key='invalid-ring')
    changed['deep'][field]['supporting'] = [{'text': 'Invalid', 'refs': ['missing']}]
    assert web.post(route(pid), headers=headers(grant), json=changed).status_code == 422
    assert len(web.get(route(pid), headers=headers(grant)).json()['items']) == 1


def test_absent_optional_rings_preserve_legacy_serialization_and_projection(tmp_path):
    from project_watch.studio.agent_result_contracts import MonitoringResults

    body = payload()
    parsed = MonitoringResults.model_validate(body)
    dumped = json.loads(parsed.model_dump_json())
    assert 'operations' not in dumped['deep'] and 'chain' not in dumped['deep']
    assert hashlib.sha256(parsed.model_dump_json().encode()).hexdigest() == (
        'cca1fd9a9afacccce014ddb0cd7f7f8b4250a8df84a8d24662dc4e8fc0178f15'
    )
    _, web, pid, grant = setup_app(tmp_path)
    assert web.post(route(pid), headers=headers(grant), json=body).status_code == 201
    analysis = web.get(f'/api/studio/projects/{pid}/insight').json()['record']['analysis']
    assert 'operations' not in analysis and 'chain' not in analysis


def test_internal_ring_remains_private(tmp_path):
    from project_watch.domain import FragmentInput

    app, web, pid, public = setup_app(tmp_path)
    private = app.state.studio_application.issue_grant(pid, 'write', 'synthetic-private', allow_internal=True)
    repo = app.state.intake_worker.repository
    source = repo.add_source(pid, kind='document', reference='synthetic.txt', content_sha256='b'*64)
    repo.enqueue_intake(pid, source.id)
    job = repo.claim_next_intake()
    repo.complete_intake(job.id, [FragmentInput(ordinal=1, locator='line 1', text='Synthetic private input')])
    body = payload()
    body['sources'].append({'id': 'D1', 'kind': 'internal', 'title': 'Synthetic private',
                            'source_id': source.id, 'locator': 'line 1'})
    body['deep']['operations'] = {'supporting': [{'text': 'Private observation', 'refs': ['D1']}]}
    assert web.post(route(pid), headers=headers(public), json=body).status_code == 403
    assert web.post(route(pid), headers=headers(private), json=body).status_code == 201
    assert web.get(route(pid)+'/current', headers=headers(public)).json()['record'] is None


@pytest.mark.parametrize('field,invalid', [('operations', 'favorable'), ('chain', 'improving')])
def test_ring_verdicts_are_not_interchangeable(field, invalid):
    from pydantic import ValidationError

    from project_watch.studio.agent_result_contracts import MonitoringResults

    body = payload()
    body['deep'][field] = {'verdict': invalid}
    with pytest.raises(ValidationError):
        MonitoringResults.model_validate(body)
