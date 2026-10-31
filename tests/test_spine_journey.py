"""One full round of the rewritten pipeline through the worker entry: collect → analyze → report.

Synthetic company and providers; the model is a synthetic endpoint answering by
system prompt over the real HTTP/journal path. The evidence reviewer is a
deterministic double (its JSON contract is covered by section analysis tests).
No network.
"""
import json
import socket
from contextlib import closing
from urllib.parse import unquote_plus, urlsplit

import pytest
from synthetic_providers import EMPTY_RSS, model_answer, paper, patent_html, patents, section

from project_watch.studio import analyze_job, dispatch, insight
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.llm import ModelError
from project_watch.studio.research_sources import research_sources
from project_watch.studio.settings import ModelConfig
from project_watch.studio.store import now

LEGAL = 'Synthetic Bio Inc.（合成生物，开曼注册主体）'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network prohibited'))
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    pipeline.test_app = app  # API tests drive the same state
    settings = pipeline.settings_store.load()
    settings.model = ModelConfig(base_url='https://model.example.invalid/v1', model='synthetic', api_key='synthetic')
    pipeline.settings_store.save(settings)
    project = pipeline.repository.create_project('合成生物')
    pipeline.store.upsert_project(project.id, {'legal_name': LEGAL, 'short_name': '合成生物'}, consent=True)
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        pipeline.objects.confirm_subject_identity(project.id, LEGAL, ['Synthetic Biologics (Suzhou) Co., Ltd.'], db=db)
        db.execute("UPDATE studio_jobs SET status='done' WHERE project_id=?", (project.id,))
    state = {'drafts': 0, 'sent': []}

    def send(method, url, headers, body, timeout):
        host, query = urlsplit(url).hostname, unquote_plus(urlsplit(url).query)
        state['sent'].append(host)
        if host == 'model.example.invalid':
            request = json.loads(body)
            system, user = request['messages'][0]['content'], request['messages'][1]['content']
            answer = model_answer(system, user, state)
            if answer == 500:
                return Response(500, {}, b'{}')
            return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(answer, ensure_ascii=False)},
                                                              'finish_reason': 'stop'}]}).encode())
        if host == 'www.ebi.ac.uk':
            data = patents() if 'SRC:PAT' in query else paper('Synthetic Biologics, Shanghai') if 'AFFILIATION' in query \
                else {'hitCount': 0, 'resultList': {'result': []}}
        elif host == 'api.openalex.org':
            data = {'results': [], 'meta': {'count': 0}}
        elif host == 'patents.google.com':
            number = urlsplit(url).path.split('/')[2]
            return Response(200, {}, patent_html(number, applicant='Synthetic Biologics Suzhou Co Ltd', family=None).encode())
        elif host == 'www.bing.com':
            return Response(200, {}, EMPTY_RSS)
        elif host == 'clinicaltrials.gov':
            data = {'studies': []}
        elif host == 'www.cninfo.com.cn':
            data = {'announcements': [], 'totalAnnouncement': 0}
        else:
            pytest.fail('unexpected provider: ' + host)
        return Response(200, {}, json.dumps(data).encode())

    pipeline.http_factory = lambda _: Http(send)

    def reviewer(_pipeline, _execution, _llm, guard):
        def review(system, payload):
            guard()
            if 'conflicts' in system:
                return dict(consistent=True, reason='synthetic', conflicts=[])
            quote = next(iter(payload['evidence'].values()))['context'][:12]
            return dict(checks=[dict(field=f, status='supported', reason='synthetic source match',
                                     evidence=[dict(ref=next(iter(payload['evidence'])), quote=quote)])
                                for f in payload['fields']])
        return review
    monkeypatch.setattr(analyze_job, '_reviewer', reviewer)
    monkeypatch.setattr('time.sleep', lambda _s: None)  # model retry back-off
    return pipeline, project.id, state


def work(pipeline, kind):
    job = pipeline.store.claim()
    assert job and job['kind'] == kind, job
    try:
        pipeline.run(job)
        pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    except Exception as exc:  # the worker records failures the same way
        pipeline.store.finish(job['id'], error=type(exc).__name__, lease_token=job['lease_token'])
        raise
    return JobExecution(pipeline.store, job)


def test_one_update_reaches_sections_events_and_report(env):
    pipeline, pid, _state = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    work(pipeline, 'collect')
    analysis = work(pipeline, 'analyze')
    result = analysis.read_checkpoint(analyze_job.RESULT)
    assert result['status'] == 'partial' and set(result['published']) == {'operations', 'chain'}
    report = work(pipeline, 'report')
    assert report.read_checkpoint('report_result_v1')['status'] == 'complete'
    # Papers and patents are reviewed and labelled; the company's own are marked as such.
    rows = research_sources(pipeline.store, pid, 'paper')['items'] + research_sources(pipeline.store, pid, 'patent')['items']
    assert rows and all(r['processing_status'] == 'organized' for r in rows)
    assert {r['extra'].get('research_relation') for r in rows} >= {'subject'}
    view = insight.load(pipeline.store, pid)
    assert view['analysis']['operations']['summary'] == section()['summary']
    # Analysis never collected: no provider request was made by the analyze job.
    with pipeline.store._connect() as db:
        kinds = {r[0] for r in db.execute('SELECT DISTINCT kind FROM studio_job_calls WHERE job_id=?', (analysis.job['id'],))}
    assert kinds == {'model'}


def test_failed_analysis_is_redone_from_saved_material_without_collecting(env):
    pipeline, pid, state = env
    state['fail_draft'] = 1  # the provider answers the first draft request with a 500
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    collected = work(pipeline, 'collect')
    with pytest.raises(ModelError, match='500'):
        work(pipeline, 'analyze')
    sent_before = list(state['sent'])
    # An automatic re-run may not resend the 5xx-answered draft request.
    pipeline.store.enqueue(pid, 'analyze', input_version='collect:' + collected.job['id'])  # e.g. a re-trigger
    with pytest.raises(ModelError, match=r'请求已暂停'):  # definite "not sent", surfaced at once
        work(pipeline, 'analyze')
    # The owner's explicit redo is consent to resend; still no collection.
    pipeline.store.enqueue(pid, 'analyze', input_version=dispatch.REDO_PREFIX + collected.job['id'])
    redo = work(pipeline, 'analyze')
    assert redo.read_checkpoint(analyze_job.RESULT)['published']
    new = state['sent'][len(sent_before):]
    assert set(new) == {'model.example.invalid'}  # re-analysis sent model requests only


def test_an_old_unknown_request_does_not_stop_a_new_round(env):
    """A project that still holds a legacy unknown (e.g. a business extractor's) runs end to end."""
    pipeline, pid, _state = env
    legacy = pipeline.store.enqueue(pid, 'business', input_version='legacy')
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed' WHERE id=?", (legacy['id'],))
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                   "VALUES (?,?,?,?,?,?)", (legacy['id'], 'legacy-key', 'model', 'synthetic', 'unknown', now()))
        before = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=?', (legacy['id'],))]
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    work(pipeline, 'collect')
    analysis = work(pipeline, 'analyze')
    assert analysis.read_checkpoint(analyze_job.RESULT)['published']
    with pipeline.store._connect() as db:
        after = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=?', (legacy['id'],))]
    assert after == before  # the old request is untouched: not resent, not settled


def test_assumptions_warning_and_exploration_feed_the_next_round(env):
    from project_watch.studio import assumptions, collect_job

    pipeline, pid, _state = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    work(pipeline, 'collect')
    analysis = work(pipeline, 'analyze')
    follow = analysis.read_checkpoint(analyze_job.RESULT)['follow_through']
    assert follow['assumptions_draft'] == 'done' and follow['early_warning'] == 'done'
    drafted = assumptions.current(pipeline.store, pid)
    assert [a['author'] for a in drafted] == ['model', 'model']
    warning = assumptions.latest_warning(pipeline.store, pid)
    statuses = {a['id']: a['status'] for a in warning['assumptions']}
    # A cited signal is kept; a signal citing an item that was never given is dropped
    # and the claim falls back to "unclear" rather than an unsupported "weakened".
    assert statuses[drafted[0]['id']] == 'strengthened' and statuses[drafted[1]['id']] == 'unclear'
    assert [u['text'] for u in warning['unforeseen']] == ['出现新技术路线']
    # The owner edits one assumption; the model never rewrites it.
    edited = assumptions.edit(pipeline.store, pid, assumption_id=drafted[0]['id'], text='核心管线 2027 年前进入三期')
    assert edited['user_override'] and edited['version'] == 2
    assert assumptions.draft_if_missing(pipeline, None, pid) is None  # never redrafted once any exist
    # The proposed exploration query is used once, by the next collection.
    work(pipeline, 'report')
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE kind IN ('brief','landscape') AND project_id=?", (pid,))
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    second = work(pipeline, 'collect')
    frozen = second.read_checkpoint(collect_job.INPUT)
    assert frozen['exploration'] == ['Synthetic Rival 新靶点']
    assert 'Synthetic Rival 新靶点' in frozen['public_plan']['industry_queries']
    assert assumptions.take_exploration(pipeline.store, pid, 'some-later-job') == []
