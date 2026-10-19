"""Explicit paid recovery never erases a dispatch or widens its consent."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace

import pytest
from test_external_analysis_jobs import allow, sample

from project_watch.analysis_failures import AnalysisFailure, failure_code
from project_watch.external_analysis_jobs import ExternalAnalysisStore, ExternalAnalysisWorker
from project_watch.reading_jobs import ReadingDenied


def failed_sample(tmp_path, calls=5):
    s = sample(tmp_path)
    grant = allow(s, calls=calls)
    def fail():
        raise AnalysisFailure('incomplete_response')
    s[4].after = fail
    job = s[7].process_one()
    s[4].after = None
    return s, grant, job


def retry(s, job):
    store, model = s[6], s[4]
    viewed = store.retry_preview(s[1].id, job['id'], model.authorization_id())
    return store.retry(s[1].id, job['id'], model.authorization_id(), viewed['disclosure_digest'])


def test_failure_is_safe_durable_and_retry_is_explicit_idempotent(tmp_path):
    s, grant, original = failed_sample(tmp_path)
    store, worker, model = s[6], s[7], s[4]
    assert original['state'] == 'uncertain'
    failures = store.failures(s[1].id)
    assert failures[0]['code'] == 'incomplete_response'
    assert worker.process_one() is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        children = list(pool.map(lambda _: retry(s, original), range(2)))
    assert children[0]['id'] == children[1]['id'] != original['id']
    reboot = ExternalAnalysisWorker(ExternalAnalysisStore(s[0]), s[5])
    assert reboot.process_one()['state'] == 'succeeded'
    assert reboot.process_one() is None
    assert model.calls == 2 and store.status(grant['id'])['requests_used'] == 2
    assert store.get(original['id'])['state'] == 'uncertain'
    assert store.failures(s[1].id) == []
    assert retry(s, original)['id'] == children[0]['id']
    assert len(store.read(s[1].id)['items']) == 1
    with closing(s[0]._connect()) as db:
        assert db.execute('SELECT count(*) FROM external_analysis_failures').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM external_analysis_dispatches').fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE external_analysis_failures SET code='unknown'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM external_analysis_retries')


@pytest.mark.parametrize('change', ['paused', 'revoked', 'source', 'plan', 'receiver', 'budget', 'digest', 'project'])
def test_retry_rechecks_scope_budget_and_disclosure(tmp_path, change):
    s, grant, job = failed_sample(tmp_path, calls=1 if change == 'budget' else 5)
    repo, project, source, _, model, _, store, _, plans, _ = s
    config, pid, fingerprint = model.authorization_id(), project.id, '0'*64
    if change != 'budget':
        fingerprint = store.retry_preview(pid, job['id'], config)['disclosure_digest']
    if change in {'paused', 'revoked'}:
        store.set_status(grant['id'], change, actor='synthetic')
    elif change == 'source':
        repo.get_source = lambda _: replace(source, content_sha256='e'*64)
    elif change == 'plan':
        repo.set_monitoring_plan_enabled(plans[0], False)
    elif change == 'receiver':
        config = 'c'*64
    elif change == 'digest':
        fingerprint = 'f'*64
    elif change == 'project':
        pid = repo.create_project('Other').id
    with pytest.raises(ReadingDenied):
        store.retry(pid, job['id'], config, fingerprint)
    assert model.calls == 1
    with closing(repo._connect()) as db:
        assert db.execute('SELECT count(*) FROM external_analysis_retries').fetchone()[0] == 0


def test_unknown_errors_do_not_store_private_exception_text(tmp_path, caplog):
    s = sample(tmp_path)
    allow(s)
    def fail():
        raise RuntimeError('private-bp-secret-token')
    s[4].after = fail
    s[7].process_one()
    data = s[6].failures(s[1].id)
    assert data[0]['code'] == 'unknown'
    assert 'private-bp-secret-token' not in json.dumps(data) + caplog.text
    assert failure_code(ValueError('private-bp-secret-token')) == 'unknown'


def test_old_failed_job_gets_unknown_reason_without_fabricated_history(tmp_path):
    s = sample(tmp_path)
    grant = allow(s)
    job = s[6].enqueue(grant['id'], s[3])
    with closing(s[0]._connect()) as db, db:
        db.execute("UPDATE external_analysis_jobs SET state='uncertain' WHERE id=?", (job['id'],))
    assert s[6].failures(s[1].id)[0]['code'] == 'legacy_unknown'
    ExternalAnalysisStore(s[0])
    with closing(s[0]._connect()) as db:
        assert db.execute('SELECT count(*) FROM external_analysis_failures').fetchone()[0] == 0


def test_retry_api_protects_consent_project_and_replays(tmp_path, monkeypatch):
    from test_external_analysis_api import HEADERS, consent, preview, setup
    s, app, client, route, draft = setup(tmp_path, monkeypatch)
    client.post(route+'/authorization', json=consent(draft, preview(client, route, draft)), headers=HEADERS)
    def fail():
        raise AnalysisFailure('evidence_validation')
    s[4].after = fail
    job = app.state.external_analysis_worker.process_one()
    s[4].after = None
    url = route + '/jobs/' + job['id']
    assert client.post(url+'/retry-preview').status_code == 403
    assert client.post(url+'/retry-preview', headers={**HEADERS, 'Origin': 'https://evil.test'}).status_code == 403
    viewed = client.post(url+'/retry-preview', headers=HEADERS).json()
    payload = dict(consent=True, disclosure_digest=viewed['disclosure_digest'])
    assert client.post(url+'/retry', json=payload).status_code == 403
    assert client.post(url+'/retry', json={**payload, 'consent': False}, headers=HEADERS).status_code == 422
    other = s[0].create_project('Other')
    assert client.post(url.replace(s[1].id, other.id)+'/retry', json=payload, headers=HEADERS).status_code == 409
    accepted = client.post(url+'/retry', json=payload, headers=HEADERS)
    assert accepted.status_code == 200
    assert app.state.external_analysis_worker.process_one()['state'] == 'succeeded'
    assert client.post(url+'/retry', json=payload, headers=HEADERS).json() == {
        'id': accepted.json()['id'], 'state': 'succeeded'}
    assert client.get(route).json()['failures'] == []
    assert client.get(route).json()['processing']['counts'] == {'succeeded': 1}
    assert s[4].calls == 2


def test_dsh_validation_is_classified_without_retaining_output(tmp_path, monkeypatch):
    from test_external_analysis_compact import compact_output as selection_output

    from project_watch.dsh_executor import DshProposalModel, parse_proposal_stream
    s = sample(tmp_path)
    model = DshProposalModel(s[4].config, 'synthetic-key')
    packet = s[3]
    for raw, code in [({}, 'output_schema'), (selection_output(packet), 'citation_mismatch')]:
        if raw:
            raw['source_statements'][0]['evidence'][0] = 'not in source'
        monkeypatch.setattr(model, '_run', lambda *a, result=raw, **kw: result)
        with pytest.raises(AnalysisFailure) as caught:
            model.analyze_external(packet, before_send=lambda body: None)
        assert failure_code(caught.value) == code
    for body, code in [
        (b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}', 'incomplete_response'),
        (b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}', 'invalid_json'),
    ]:
        try:
            parse_proposal_stream(body, 'anthropic-messages')
        except Exception as exc:
            assert failure_code(exc) == code


@pytest.mark.parametrize('change', ['paused', 'revoked', 'source', 'receiver'])
def test_queued_retry_is_rechecked_at_dispatch(tmp_path, change):
    s, grant, job = failed_sample(tmp_path)
    child = retry(s, job)
    def before():
        if change in {'paused', 'revoked'}:
            s[6].set_status(grant['id'], change, actor='synthetic')
        elif change == 'receiver':
            s[5].model = None
        else:
            s[0].get_source = lambda _: replace(s[2], content_sha256='e'*64)
    s[4].before = before
    assert s[7].process_one()['id'] == child['id']
    assert s[4].calls == 1 and s[6].status(grant['id'])['requests_used'] == 1


def test_retry_remains_bound_to_original_budget_even_with_new_consent(tmp_path):
    s, grant, job = failed_sample(tmp_path)
    child = retry(s, job)
    renewed = allow(s, key='synthetic-another-grant')
    assert s[6].enqueue(renewed['id'], s[3])['id'] == child['id']
    assert s[6].get(child['id'])['authorization_id'] == grant['id']
    assert s[7].process_one()['state'] == 'succeeded'
    assert s[6].status(grant['id'])['requests_used'] == 2
    assert s[6].status(renewed['id'])['requests_used'] == 0


def test_recovery_tables_survive_backup_restore_and_reinitialize(tmp_path):
    from project_watch.repository import ProjectRepository
    s, _, job = failed_sample(tmp_path)
    child = retry(s, job)
    target = tmp_path/'recovery-backup.sqlite3'
    with closing(s[0]._connect()) as db, closing(sqlite3.connect(target)) as backup:
        db.backup(backup)
    repo = ProjectRepository(target)
    store = ExternalAnalysisStore(repo)
    assert store.get(job['id'])['state'] == 'uncertain'
    assert store.get(child['id'])['state'] == 'queued'
    assert repo.get_project(s[1].id) == s[1]
    assert ExternalAnalysisWorker(store, s[5]).process_one()['state'] == 'succeeded'
    with closing(repo._connect()) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_prompt_and_validator_require_evidence_for_each_object(tmp_path):
    from test_external_analysis import output

    from project_watch.external_analysis import (
        AnalysisEvidenceError,
        external_analysis_prompt,
        validate_external_analysis,
    )
    packet = sample(tmp_path)[3]
    prompt = external_analysis_prompt(packet)
    assert 'each object\'s selected excerpts' in prompt and 'require both internal and external IDs' in prompt
    raw = output(packet)
    raw['relationship']['evidence'] = [c for c in raw['relationship']['evidence'] if c['ref_id'].startswith('news:')]
    with pytest.raises(AnalysisEvidenceError) as caught:
        validate_external_analysis(packet, raw)
    assert caught.value.code == 'evidence_scope'
