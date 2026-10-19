"""Offline normal worker/material/question integration, not live quality scores."""

import json
import socket
from datetime import UTC, datetime

import pytest
from test_studio_question_materials import CORE, SUPPORT, intake, material, setup

from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.monitoring_contracts import LocatedRef
from project_watch.studio.question_advisor import ModelQuestionAdvisor


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Material/question worker controls must remain offline')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


def drain(app):
    # Real claim/run/finish, but no timer-created second monitoring round.
    worker = app.state.studio_worker
    for _ in range(60):
        worker._last_schedule = datetime.now(UTC)
        if not worker.step():
            return
    raise AssertionError('Synthetic worker queue did not become idle')


def advisor_responses(transport, outcome, calls):
    original = transport.model_result

    def result(system, user):
        if '下面每个问题附带了本轮新命中的事件' in system:
            questions = json.loads(user)['questions']
            calls.append(questions)
            if outcome == 'unknown':
                raise HttpError('connection_failed')
            if outcome == 'malformed':
                return {'unexpected': []}
            return {'results': [
                {'id': q['id'], 'status': 'insufficient',
                 'note': '资料记录交付10台，日期未知',
                 'material_ids': [q['materials'][0]['id']]}
                for q in questions if q['materials']
            ]}
        if '最值得持续跟踪的' in system:
            return {'questions': []}
        return original(system, user)

    transport.model_result = result


@pytest.mark.parametrize('outcome', ['success', 'malformed', 'unknown'])
def test_normal_worker_materials_survive_optional_question_failure_and_reopen(tmp_path, outcome):
    app, web, pipeline, pid, watches = setup(tmp_path)
    pipeline.question_advisor = ModelQuestionAdvisor()
    calls = []
    advisor_responses(app.state.synthetic_transport, outcome, calls)
    original = pipeline.questions.history(watches[0]['id'])
    # Intake uses the actual parser. Unlike the older direct-ledger tests, the
    # internal analysis is leased and completed by StudioWorker.step().
    source = intake(app, web, pipeline, pid, '合成光电交付10台光学模组。', run=False)
    internal = pipeline.store.enqueue(pid, 'internal')
    drain(app)
    finished_internal = next(j for j in pipeline.store.jobs(pid) if j['id'] == internal['id'])
    assert finished_internal['status'] == ('done' if outcome == 'success' else 'failed')
    [hit] = material(pipeline, pid, watches[0]['id'])
    assert hit['source_id'] == source and hit['status'] == ('assessed' if outcome == 'success' else 'pending')
    assert material(pipeline, pid, watches[1]['id']) == []
    assert len(calls) == 1
    assert [q['id'] for q in calls[0] if q['materials']] == [watches[0]['id']]
    assert [m['id'] for q in calls[0] for m in q['materials']] == [hit['id']]
    assert {r['id'] for r in original} <= {r['id'] for r in pipeline.questions.history(watches[0]['id'])}
    if outcome == 'unknown':
        assert finished_internal['error'] == '模型请求结果不明，未重复发送'
        assert finished_internal['retry_after'] == '9999-12-31'
        assert JobExecution(pipeline.store, finished_internal).usage()['model']['unknown'] == 1

    # Reopen the application/store, not just a second ledger over the old store.
    reopened, transport = SUPPORT['make_app'](tmp_path)
    transport.model_result = CORE['material_model'](transport.model_result)
    new_calls = []
    advisor_responses(transport, 'success', new_calls)
    current = reopened.state.studio_pipeline
    [saved] = material(current, pid, watches[0]['id'])
    assert saved['id'] == hit['id']
    assert saved['status'] == ('assessed' if outcome == 'success' else 'pending')
    current.evidence.resolve(pid, LocatedRef(**saved['ref']))
    latest = current.questions.view(pid)[0]['latest']
    assert latest['human']['note'] == '人的原判断'
    assert latest['agent']['note'] == 'Agent 原判断'
    if outcome == 'success':
        assert latest['system']['visibility'] == 'internal'
        assert latest['system']['material_ids'] == [hit['id']]
    else:
        assert 'system' not in latest
        assert current.questions.history(watches[0]['id']) == original
    drain(reopened)
    assert new_calls == []  # Idle/reopen does not replay paid work.

    if outcome == 'unknown':
        reopened.state.studio_worker.schedule()
        drain(reopened)
        assert new_calls == []  # The normal scheduler also honors the hold.
        held = next(j for j in current.store.jobs(pid) if j['id'] == internal['id'])
        assert held['status'] == 'failed' and held['retry_after'] == '9999-12-31'
        assert material(current, pid, watches[0]['id'])[0]['status'] == 'pending'

    if outcome == 'malformed':
        # A deliberately initiated *new* monitoring round can assess the still
        # pending material; this is not an automatic retry of the failed reply.
        current.store.enqueue(pid, 'external')
        drain(reopened)
        assert len(new_calls) == 1
        assert material(current, pid, watches[0]['id'])[0]['status'] == 'assessed'
        assert current.questions.view(pid)[0]['latest']['human']['note'] == '人的原判断'

