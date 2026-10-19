"""Synthetic Core workflows reused by source and physically stripped-wheel tests."""

import json
import re
from contextlib import closing
from datetime import UTC, datetime


def drain(app, *, schedule=True):
    worker = app.state.studio_worker
    if schedule:
        worker.schedule()
    worker._last_schedule = datetime.now(UTC)
    for _ in range(60):
        if not worker.step():
            break
    else:
        raise AssertionError('Synthetic Core workflow did not finish its bounded queue')


def material_model(base):
    def result(system, user):
        if '从商业计划书开头内容中找出这家公司的名称' in system:
            return {'name': '合成光学科技有限公司'}
        if '材料按片段给出' in system:
            rows = []
            points = []
            for marker, passage in re.findall(r'\[(F\d+)\](.*?)(?=\[F\d+\]|$)', user, flags=re.S):
                amount = re.search(r'营业收入(1000|1200)万元', passage)
                if not amount:
                    continue
                quote = '合成光学科技有限公司2026年营业收入' + amount[1] + '万元'
                rows.append({'name': '营业收入', 'period': '2026', 'value': int(amount[1]), 'unit': '万元',
                             'basis': 'actual', 'category': 'financial', 'subject': '合成光学科技有限公司',
                             'scope': '公司', 'ref': marker, 'quote': quote})
                points.append({'section': '经营进展', 'text': quote, 'ref': marker})
            return {'points': points, 'metrics': rows, 'risks': [], 'milestones': [], 'financing': []}
        if '公司一页纸档案' in system:
            amount = '1200' if '1200' in user else '1000'
            refs = list(dict.fromkeys(re.findall(r'\[(F\d+)', user)))
            return {'one_liner': '合成公司研发车载光学模组', 'industry': '车载光学', 'stage': '合成',
                    'summary': '合成公司营业收入' + amount + '万元',
                    'sections': [{'title': '经营进展', 'points': [{'text': '营业收入' + amount + '万元', 'refs': refs}]}],
                    'highlights': [], 'risks': [], 'milestones': [], 'questions': []}
        if '根据公开搜索摘录补充' in system:
            # This workflow asserts all tasks succeeded: supply a sourced public
            # row, not the base fixture's deliberately empty optional proposal.
            source = re.search(r'\[(P\d+)\][^\n]*\n([^\n]+)', user)
            if not source:
                return {'facts': []}
            quote = source[2].split('。')[0]
            return {'facts': [{'kind': 'milestone', 'label': '合成公开进展', 'text': quote,
                              'date': '', 'refs': [{'ref': source[1], 'quote': quote}]}]}
        return base(system, user)
    return result


def headers(grant):
    return {'Authorization': 'Bearer ' + grant['token'], 'X-ProjectWatch-Instance': grant['instance_id']}


def with_public_peer(transport):
    """Source-quoted company discovery, not an always-empty optional task fixture."""
    original_hit, original_model = transport.hit, transport.model_result
    quote = '合成星辰光学有限公司研发车载光学模组'
    def hit(query):
        title, url, snippet = original_hit(query)
        return title, url, snippet + '。' + quote
    def result(system, user):
        if '从给定公开来源摘录同行公司的候选名字' in system:
            sources = json.loads(user)['sources']
            return {'candidates': [{'name': '合成星辰光学有限公司', 'source': sources[0]['source'], 'quote': quote}]}
        return original_model(system, user)
    transport.hit, transport.model_result = hit, result


def assert_public_peer(app, pid):
    p = app.state.studio_pipeline
    peers = p.competition_research.saved(pid)
    assert peers and peers['candidate_count'] == 1 and not peers['analysis_available'], peers
    obj = p.objects.get(pid, peers['candidates'][0]['id'], include_internal=False)
    assert obj['name'] == '合成星辰光学有限公司' and obj['refs']
    # An ordinary company lead does not acquire a certified competing relation.
    assert not any(r.get('support') == 'supported' for r in p.relationships.view(pid, include_internal=False)['items'])


def bp_incremental_history(client, app, transport):
    """Real draft parser, real intake, consent, summaries, T1 update, scoped Agent."""
    transport.model_result = material_model(transport.model_result)
    root = '/api/studio'
    draft = client.post(root + '/drafts', files={
        'file': ('Synthetic-BP.txt', '合成光学科技有限公司研发车载光学模组。合成光学科技有限公司2026年营业收入1000万元'.encode(), 'text/plain')})
    assert draft.status_code == 200 and draft.json()['readable'], draft.text
    resolved = client.post(root + '/resolve', json={'name': '', 'draft_id': draft.json()['draft_id']})
    assert resolved.status_code == 200 and resolved.json()['candidates'], resolved.text
    created = client.post(root + '/projects', json={'entity': resolved.json()['candidates'][0], 'consent': True,
                                                   'draft_ids': [draft.json()['draft_id']]})
    assert created.status_code == 201, created.text
    pid = created.json()['id']
    pipeline, service, repo = app.state.studio_pipeline, app.state.studio_application, app.state.intake_worker.repository
    route = root + '/projects/' + pid
    parsed = app.state.intake_worker.process_one()
    assert parsed is not None and parsed.status == 'completed', parsed
    assert repo.list_fragments(parsed.source_id)
    pipeline.snapshot_ready_documents()

    def authorize(source_id, key):
        version = service.project(pid)['config_version']
        response = client.post(route + '/sources/' + source_id + '/authorize-analysis',
                               json={'expected_version': version, 'idempotency_key': key})
        assert response.status_code == 200, response.text
    authorize(parsed.source_id, 'core-bp-consent')
    drain(app)
    t0 = client.get(route).json()
    assert t0['profile']['summary'] == '合成公司营业收入1000万元', t0['jobs']
    assert any(m['source_id'] == parsed.source_id for m in t0['metrics'])
    assert not any(j['status'] == 'failed' for j in t0['jobs']), t0['jobs']

    watch = pipeline.monitor.add_watchpoint(pid, '量产进展', '量产', ['合成量产'], '量产', False, visibility='public')
    human = client.post(route + '/questions/' + watch['id'] + '/assessments',
                        json={'status': 'setback', 'note': 'SYNTHETIC_PRIVATE_HUMAN', 'idempotency_key': 'human-t0'})
    assert human.status_code == 201
    grant = service.issue_grant(pid, 'write', 'synthetic-public-agent')
    agent_root = root + '/agent/projects/' + pid
    body = {'status': 'insufficient', 'note': '公开助手记录', 'idempotency_key': 'agent-t0'}
    written = client.post(agent_root + '/questions/' + watch['id'] + '/assessments', json=body, headers=headers(grant))
    assert written.status_code == 201, written.text
    assert client.post(agent_root + '/questions/' + watch['id'] + '/assessments', json=body, headers=headers(grant)).json() == written.json()
    denied = client.get(agent_root + '/questions', headers={**headers(grant), 'X-ProjectWatch-Instance': 'wrong'})
    assert denied.status_code == 403
    assert client.get(root + '/agent/projects/foreign-project/questions', headers=headers(grant)).status_code == 403
    public = client.get(agent_root + '/questions', headers=headers(grant))
    assert public.status_code == 200 and 'SYNTHETIC_PRIVATE_HUMAN' not in public.text
    assert client.get(agent_root + '/sources/' + parsed.source_id, headers=headers(grant)).status_code == 403

    uploaded = client.post(route + '/files', files={
        'file': ('Synthetic-T1.txt', '合成光学科技有限公司2026年营业收入1200万元'.encode(), 'text/plain')})
    assert uploaded.status_code == 202, uploaded.text
    next_parse = app.state.intake_worker.process_one()
    assert next_parse.status == 'completed'
    pipeline.snapshot_ready_documents()
    authorize(next_parse.source_id, 'core-t1-consent')
    drain(app)
    t1 = client.get(route).json()
    assert t1['profile']['summary'] == '合成公司营业收入1200万元', t1['jobs']
    assert {parsed.source_id, next_parse.source_id} <= {m['source_id'] for m in t1['metrics']}
    assert t1['doc_changes'], t1
    assert not any(j['status'] == 'failed' for j in t1['jobs']), t1['jobs']
    history = client.get(route + '/questions/' + watch['id'] + '/history').json()
    assert {human.json()['id'], written.json()['id']} <= {r['id'] for r in history}
    assert {r['actor'] for r in history} == {'human', 'agent'}
    assert client.patch(route, json={'status': 'paused'}).status_code == 200
    drain(app)
    assert pipeline.health.read(pid)['monitoring'] == 'paused'
    assert client.patch(route, json={'status': 'active'}).status_code == 200
    before = len(history)
    drain(app)
    assert len(client.get(route + '/questions/' + watch['id'] + '/history').json()) == before
    assert pipeline.questions.history(watch['id'])[0]['id'] in {r['id'] for r in history}
    return {'project_id': pid, 'watch_id': watch['id'], 'history_ids': sorted(r['id'] for r in history),
            'source_ids': [parsed.source_id, next_parse.source_id]}


def unknown_outcome_not_retried(client, app, transport, entity):
    """Provider uncertainty stays held; ordinary worker steps do not repeat it."""
    response = client.post('/api/studio/projects', json={'entity': entity, 'consent': True})
    assert response.status_code == 201
    pid = response.json()['id']
    transport.model_failure = 'offline'
    worker = app.state.studio_worker
    worker._last_schedule = datetime.now(UTC)
    before = sum(c['host'] == 'open.bigmodel.cn' for c in transport.calls)
    assert worker.step()
    failed = app.state.studio_store.jobs(pid)[0]
    assert failed['status'] == 'failed' and failed['error']
    after = sum(c['host'] == 'open.bigmodel.cn' for c in transport.calls)
    assert after == before + 1
    assert not worker.step()
    assert sum(c['host'] == 'open.bigmodel.cn' for c in transport.calls) == after
    transport.model_failure = None
    return {'project_id': pid, 'job_id': failed['id'], 'model_requests': 1}


def recover_after_restart(client, app, failure):
    route = f"/api/studio/projects/{failure['project_id']}/jobs/{failure['job_id']}/recovery"
    preview = client.get(route)
    assert preview.status_code == 200 and preview.json()['can_recover'], preview.text
    body = {'expected_version': preview.json()['version'], 'idempotency_key': 'synthetic-recovery',
            'accept_duplicate_cost': False}
    assert client.post(route, json=body).status_code == 409
    body['accept_duplicate_cost'] = True
    accepted = client.post(route, json=body)
    assert accepted.status_code == 202, accepted.text
    assert client.post(route, json=body).json() == accepted.json()
    drain(app, schedule=False)
    job = next(j for j in app.state.studio_store.jobs(failure['project_id']) if j['id'] == failure['job_id'])
    assert job['status'] == 'done', job
    with closing(app.state.studio_store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE job_id=? AND status='unknown'",
                          (job['id'],)).fetchone()[0] == 1
    assert not client.get(route).json()['can_recover']
