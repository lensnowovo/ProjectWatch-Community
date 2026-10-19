"""Two-phase monitoring against isolated synthetic stores, never live services."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from project_watch.studio import progress
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.store import StudioStore


def setup(tmp_path, monkeypatch):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Monitor').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    llm = SimpleNamespace(ready=False)
    settings = pipeline.settings_store.load()
    calls = []

    class Collectors:
        has_search = False
        has_registry = False

        def bing_news(self, query):
            # Bing News with direct publisher links is the default news source (v30).
            calls.append(('bing', query))
            return [{'key': f'{query}-{n}', 'title': f'{query} original update {n}',
                     'url': f'https://publisher.example/{query}/{n}', 'source': 'Synthetic',
                     'snippet': 'A synthetic original update.', 'published_at': None}
                    for n in range(20)]

        def openalex(self, query, **kwargs):
            calls.append(('paper', query))
            return []

        def cninfo(self, query, **kwargs):
            calls.append(('filing', query))
            return []

    collectors = Collectors()
    monkeypatch.setattr(pipeline, 'tools', lambda: (settings, None, llm, collectors))
    plan = {'company_queries': ['Synthetic Monitor', 'Synthetic product'], 'people': [],
            'competitors': [{'name': 'Synthetic Peer', 'reason': 'fixture'}],
            'industry_queries': ['Synthetic industry'], 'policy_queries': ['Synthetic policy'],
            'paper_queries': ['Synthetic paper'], 'affiliation_names': [],
            'trial_sponsors': [], 'patent_assignees': [], 'is_biomed': False}
    monkeypatch.setattr(pipeline, 'build_plan', lambda *_a: plan.copy())
    monkeypatch.setattr(pipeline, '_check_sites', lambda *_a: calls.append(('site', '')))
    monkeypatch.setattr(pipeline, '_collect_monitor', lambda *_a: {})
    monkeypatch.setattr(pipeline, 'run_public_profile', lambda *_a: calls.append(('profile', '')))
    pipeline.article_reader = SimpleNamespace(read=lambda url: {
        'status': 'article_body', 'text': 'Synthetic original public evidence.', 'resolved_url': url})
    pipeline.store.enqueue(pid, 'external')
    return pipeline, pid, calls


def first(pipeline):
    job = pipeline.store.claim()
    assert job['kind'] == 'external'
    pipeline.run(job)
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    return job


def followup(pipeline):
    brief = pipeline.store.claim()
    assert brief['kind'] == 'brief'
    pipeline.run(brief)
    pipeline.store.finish(brief['id'], lease_token=brief['lease_token'])
    job = pipeline.store.claim()
    assert job['kind'] == 'external_followup'
    return job


def test_first_result_yields_before_slow_sources_and_preserves_pending_items(tmp_path, monkeypatch):
    pipeline, pid, calls = setup(tmp_path, monkeypatch)
    first(pipeline)
    assert calls == [('bing', 'Synthetic Monitor')]
    assert pipeline.store.list_events(project_id=pid)
    assert pipeline.store.untriaged(pid)  # Remaining headlines are not marked skipped/triaged.
    assert len(pipeline.store.items_with_body(pid)) <= 6
    assert pipeline.store.get_project(pid)['last_external_at'] is None
    state = progress.read(pipeline.store, pid)
    assert state['state'] == 'partial' and state['first_result_at']
    assert pid not in pipeline.store.due_external_projects(24)
    assert pid not in pipeline.store.missing_public_profile_projects()


def test_followup_collects_remaining_families_once_and_completes_round(tmp_path, monkeypatch):
    pipeline, pid, calls = setup(tmp_path, monkeypatch)
    first(pipeline)
    job = followup(pipeline)
    pipeline.run(job)
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    assert calls.count(('bing', 'Synthetic Monitor')) == 1
    for entry in [('bing', 'Synthetic product'), ('bing', 'Synthetic Peer'),
                  ('bing', 'Synthetic industry'), ('bing', 'Synthetic policy'),
                  ('paper', 'Synthetic paper'), ('filing', 'Synthetic Monitor'), ('site', ''), ('profile', '')]:
        assert entry in calls
    assert pipeline.store.untriaged(pid) == []
    assert pipeline.store.get_project(pid)['last_external_at']
    assert progress.read(pipeline.store, pid)['state'] == 'observed'
    assert pipeline.store.get_plan(pid)['last_report']['company']['checked'] == 2  # Bing per query; Google off (v30)


def test_paused_followup_survives_store_restart_and_resumes(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    first(pipeline)
    pipeline.store.set_status(pid, 'paused')
    pipeline.store = StudioStore(pipeline.store.db_path)
    assert pipeline.store.claim() is None  # pause includes model-backed briefs
    pipeline.store.set_status(pid, 'active')
    brief = pipeline.store.claim()
    assert brief['kind'] == 'brief'
    pipeline.store.finish(brief['id'], lease_token=brief['lease_token'])
    job = pipeline.store.claim()
    assert job['kind'] == 'external_followup'
    pipeline.run(job)
    assert pipeline.store.get_project(pid)['last_external_at']


def test_followup_failure_retains_first_result_and_never_claims_silence(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    first(pipeline)
    events = pipeline.store.list_events(project_id=pid)
    job = followup(pipeline)
    monkeypatch.setattr(pipeline, 'collect', lambda *_a, **_k: (_ for _ in ()).throw(HttpError('request_outcome_unknown')))
    with pytest.raises(HttpError):
        pipeline.run(job)
    pipeline.store.finish(job['id'], error='request_outcome_unknown', lease_token=job['lease_token'])
    assert pipeline.store.list_events(project_id=pid) == events
    assert progress.read(pipeline.store, pid)['state'] == 'failed'
    assert pipeline.store.get_project(pid)['last_external_at'] is None


@pytest.mark.parametrize('change', ['identity', 'model'])
def test_changed_inputs_hold_followup_before_any_collection(tmp_path, monkeypatch, change):
    pipeline, pid, calls = setup(tmp_path, monkeypatch)
    first(pipeline)
    if change == 'identity':
        pipeline.store.upsert_project(pid, {'legal_name': 'Different Synthetic Company'}, consent=False)
    else:
        pipeline.settings_store.update({'model': {'provider': 'openai', 'base_url': 'https://api.openai.com/v1',
                                                 'model': 'synthetic-model', 'api_key': 'synthetic-key'}})
    calls.clear()
    job = followup(pipeline)
    with pytest.raises(ValueError, match='监控依据已变化'):
        pipeline.run(job)
    assert calls == []
    assert pipeline.store.get_project(pid)['last_external_at'] is None


def test_crash_after_handoff_replays_without_duplicate_children_or_search(tmp_path, monkeypatch):
    pipeline, pid, calls = setup(tmp_path, monkeypatch)
    job = pipeline.store.claim()
    pipeline.run(job)
    before = pipeline.store.jobs(pid)
    count = len(calls)
    with pipeline.store._connect() as db:
        db.execute("DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage='dispatch_completed'", (job['id'],))
    pipeline.run(job)
    # Re-saving the missing dispatch checkpoint advances the progress clock,
    # but must not create jobs or repeat a search.
    def stable(rows):
        return [{k: v for k, v in row.items() if k != 'progress_at'} for row in rows]
    assert stable(pipeline.store.jobs(pid)) == stable(before) and len(calls) == count
    checkpoint = JobExecution(pipeline.store, job).read_checkpoint('external_first_ready')
    assert checkpoint['followup_id'] in {j['id'] for j in before}


def test_refresh_reuses_pending_followup_instead_of_starting_a_second_round(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    first(pipeline)
    pending = next(j for j in pipeline.store.jobs(pid) if j['kind'] == 'external_followup')
    assert pipeline.store.enqueue(pid, 'external')['id'] == pending['id']


def test_unknown_collection_never_creates_a_followup_or_completes_first(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    collectors = pipeline.tools()[3]
    collectors.bing_news = lambda *_a: (_ for _ in ()).throw(HttpError('request_outcome_unknown'))
    job = pipeline.store.claim()
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        pipeline.run(job)
    assert not any(j['kind'] == 'external_followup' for j in pipeline.store.jobs(pid))


def test_child_cannot_steal_parent_lease_and_handoff_is_atomic(tmp_path, monkeypatch):
    pipeline, _pid, _ = setup(tmp_path, monkeypatch)
    parent = pipeline.store.claim()
    pipeline.run(parent)
    brief = pipeline.store.claim()
    assert brief['kind'] == 'brief'
    pipeline.store.finish(brief['id'], lease_token=brief['lease_token'])
    assert pipeline.store.claim() is None
    pipeline.store.finish(parent['id'], lease_token=parent['lease_token'])
    assert pipeline.store.claim()['kind'] == 'external_followup'


def test_material_analysis_precedes_first_company_round(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    pipeline.store.enqueue(pid, 'internal')
    assert pipeline.store.claim()['kind'] == 'internal'
    assert pipeline.store.claim() is None


def test_known_failure_explicit_retry_keeps_the_original_job_and_receipts(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    first(pipeline)
    job = followup(pipeline)
    execution = JobExecution(pipeline.store, job)
    execution.checkpoint('synthetic_saved', {'read': True})
    pipeline.store.finish(job['id'], error='known synthetic refusal', lease_token=job['lease_token'])
    retried = pipeline.store.retry_external_followup(pid)
    assert retried['id'] == job['id'] and retried['status'] == 'queued'
    assert JobExecution(pipeline.store, retried).read_checkpoint('synthetic_saved') == {'read': True}


def test_unknown_followup_is_held_and_not_retried_by_scheduler_or_owner(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    first(pipeline)
    job = followup(pipeline)
    pipeline.store.finish(job['id'], error='request_outcome_unknown', lease_token=job['lease_token'])
    assert pid not in pipeline.store.due_external_projects(0)
    with pytest.raises(ValueError, match='不会自动重发'):
        pipeline.store.retry_external_followup(pid)


def test_first_brief_has_saved_content_before_followup_starts(tmp_path, monkeypatch):
    pipeline, pid, calls = setup(tmp_path, monkeypatch)
    settings, http, _, collectors = pipeline.tools()

    class Model:
        ready = True

        def json(self, *_a, **_k):
            return {'results': [], 'headline': 'Synthetic first brief',
                    'summary': 'Synthetic sourced first-round overview.', 'changes': [], 'watch': []}

    monkeypatch.setattr(pipeline, 'tools', lambda: (settings, http, Model(), collectors))
    first(pipeline)
    job = pipeline.store.claim()
    assert job['kind'] == 'brief'
    pipeline.run(job)
    assert pipeline.store.get_brief(pid)['headline'] == 'Synthetic first brief'
    assert ('paper', 'Synthetic paper') not in calls and ('profile', '') not in calls


def test_unknown_auxiliary_first_call_cannot_release_a_completed_handoff(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    job = pipeline.store.claim()

    def uncertain_merge(*_a):
        with pipeline.store._connect() as db:
            db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                       "VALUES (?,?,'model','','unknown','2026-01-01T00:00:00+00:00')",
                       (job['id'], 'synthetic-unknown-merge'))

    monkeypatch.setattr(pipeline, 'cluster', uncertain_merge)
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        pipeline.run(job)
    assert not any(j['kind'] == 'external_followup' for j in pipeline.store.jobs(pid))
    assert pipeline.store.get_project(pid)['last_external_at'] is None


@pytest.mark.parametrize('phase', ['first', 'followup'])
def test_identity_change_during_processing_cannot_publish_round_completion(tmp_path, monkeypatch, phase):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    if phase == 'followup':
        first(pipeline)
        job = followup(pipeline)
    else:
        job = pipeline.store.claim()

    def changed_identity(*_a):
        pipeline.store.upsert_project(pid, {'legal_name': 'Different Synthetic Company'}, consent=False)

    monkeypatch.setattr(pipeline, 'cluster', changed_identity)
    with pytest.raises(ValueError, match='监控依据已变化'):
        pipeline.run(job)
    assert pipeline.store.get_project(pid)['last_external_at'] is None
    if phase == 'first':
        assert not any(j['kind'] == 'external_followup' for j in pipeline.store.jobs(pid))


def test_completed_new_round_clears_old_input_hold_from_regular_monitoring(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    first(pipeline)
    job = followup(pipeline)
    pipeline.store.finish(job['id'], error='监控依据已变化', lease_token=job['lease_token'])
    assert pid not in pipeline.store.due_external_projects(0)
    pipeline.store.enqueue(pid, 'external')  # Explicit owner action with current inputs.
    first(pipeline)
    job = followup(pipeline)
    pipeline.run(job)
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    assert pid in pipeline.store.due_external_projects(0)


@pytest.mark.parametrize('outcome', ['known', 'unknown', 'paused'])
def test_owner_followup_retry_api_preserves_receipts_and_respects_holds(tmp_path, monkeypatch, outcome):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    first(pipeline)
    job = followup(pipeline)
    JobExecution(pipeline.store, job).checkpoint('synthetic_saved', {'read': True})
    pipeline.store.finish(job['id'], error='request_outcome_unknown' if outcome == 'unknown' else 'known synthetic refusal',
                          lease_token=job['lease_token'])
    if outcome == 'paused':
        pipeline.store.set_status(pid, 'paused')
    app = create_studio_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        response = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external_followup'})
    if outcome == 'known':
        assert response.status_code == 202 and response.json()['id'] == job['id']
        assert response.json()['status'] == 'queued'
    else:
        assert response.status_code == 409
        children = [j for j in pipeline.store.jobs(pid) if j['kind'] == 'external_followup']
        assert len(children) == 1 and children[0]['id'] == job['id']
        assert children[0]['status'] == 'failed'
    assert JobExecution(pipeline.store, job).read_checkpoint('synthetic_saved') == {'read': True}


def test_a_new_material_profile_rebuilds_the_monitoring_plan_next_round(tmp_path, monkeypatch):
    """BP entry: analysed materials must reach the external queries, not wait for an entity edit."""
    pipeline, pid, _calls = setup(tmp_path, monkeypatch)
    built = []
    original = pipeline.build_plan
    monkeypatch.setattr(pipeline, 'build_plan', lambda *a: built.append(1) or original(*a))
    first(pipeline)
    assert len(built) == 1
    rest = followup(pipeline)
    pipeline.run(rest)
    pipeline.store.finish(rest['id'], lease_token=rest['lease_token'])
    pipeline.store.put_profile(pid, {'summary': '合成公司生产工业检测设备。', 'sections': []})
    pipeline.store.enqueue(pid, 'external')
    while (job := pipeline.store.claim()) and job['kind'] != 'external':
        pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    assert job
    pipeline.run(job)
    assert len(built) == 2
    assert pipeline.store.get_plan(pid)['profile_version'] == pipeline.store.get_profile(pid)['updated_at']
