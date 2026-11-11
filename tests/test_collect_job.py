"""The rewritten collection stage, through the real worker entry. Synthetic data; no network."""
import json
import socket
from contextlib import closing
from urllib.parse import unquote_plus, urlsplit

import pytest
from synthetic_providers import EMPTY_RSS, paper, patent_html, patents

from project_watch.studio import collect_job
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.research_sources import research_sources
from project_watch.studio.settings import ModelConfig
from project_watch.studio.store import now

LEGAL = 'Synthetic Bio Inc.（合成生物，开曼注册主体）'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network prohibited'))
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    settings = pipeline.settings_store.load()
    settings.model = ModelConfig(base_url='https://model.example.invalid/v1', model='synthetic', api_key='synthetic')
    pipeline.settings_store.save(settings)
    project = pipeline.repository.create_project('合成生物')
    pipeline.store.upsert_project(project.id, {'legal_name': LEGAL, 'short_name': '合成生物'}, consent=True)
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        pipeline.objects.confirm_subject_identity(project.id, LEGAL, ['Synthetic Biologics (Suzhou) Co., Ltd.'], db=db)
        db.execute("UPDATE studio_jobs SET status='done' WHERE project_id=?", (project.id,))
    sent, fail = [], set()

    def send(method, url, headers, body, timeout):
        sent.append(url)
        host, query = urlsplit(url).hostname, unquote_plus(urlsplit(url).query)
        if host in fail:
            return Response(500, {}, b'{}')
        if host == 'model.example.invalid':  # the one-time plan draft: nothing beyond confirmed names
            answer = json.dumps({'competitors': [], 'industry_queries': []})
            return Response(200, {}, json.dumps({'choices': [{'message': {'content': answer}, 'finish_reason': 'stop'}]}).encode())
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
    return pipeline, project.id, sent, fail


def run_collect(pipeline, pid):
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    job = pipeline.store.claim()
    assert job['kind'] == 'collect'
    pipeline.run(job)
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])  # as the worker does after run()
    return JobExecution(pipeline.store, job)


def queries(sent, host):
    return [unquote_plus(urlsplit(u).query) for u in sent if urlsplit(u).hostname == host]


def test_cleaned_names_patent_followup_and_immediate_visibility(env):
    pipeline, pid, sent, _ = env
    execution = run_collect(pipeline, pid)
    frozen = execution.read_checkpoint(collect_job.INPUT)
    assert frozen['names'][:2] == ['Synthetic Bio Inc.', '合成生物']  # the note is gone, the short name kept
    ebi = ' '.join(queries(sent, 'www.ebi.ac.uk'))
    assert 'AFFILIATION:"Synthetic Biologics"' in ebi and 'AFFILIATION:"Synthetic Bio"' in ebi
    assert '开曼注册主体' not in ebi and 'Inc.' not in ebi  # never the raw annotated field
    # Only the patent whose applicant field names a confirmed name gets a document fetch.
    google = [u for u in sent if urlsplit(u).hostname == 'patents.google.com']
    assert google == ['https://patents.google.com/patent/WO2099123456A1/en']
    papers = research_sources(pipeline.store, pid, 'paper')
    patent_rows = research_sources(pipeline.store, pid, 'patent')
    assert papers['total'] >= 1 and patent_rows['total'] == 3  # visible now, before any analysis
    assert all(r['processing_status'] == 'pending' for r in papers['items'])
    result = execution.read_checkpoint(collect_job.RESULT)
    assert result['patent_documents'] == ['WO2099123456A1'] and result['status'] in ('complete', 'partial')
    with pipeline.store._connect() as db:
        queued = db.execute("SELECT kind,input_version FROM studio_jobs WHERE project_id=? AND status='queued'", (pid,)).fetchall()
    assert [tuple(r) for r in queued] == [('analyze', 'collect:' + execution.job['id'])]


def test_one_failing_source_does_not_stop_the_others(env):
    pipeline, pid, _sent, fail = env
    fail.add('api.openalex.org')
    execution = run_collect(pipeline, pid)
    result = execution.read_checkpoint(collect_job.RESULT)
    assert result['status'] == 'partial' and result['outcomes'].get('failed')
    assert research_sources(pipeline.store, pid, 'patent')['total'] == 3


def test_restart_replays_the_same_plan_without_new_requests(env):
    pipeline, pid, sent, _ = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    job = pipeline.store.claim()
    pipeline.run(job)
    before = len(sent)
    pipeline.run(job)  # a crash-restart re-enters the same job before it is finished
    assert len(sent) == before


def test_an_unclear_request_in_an_earlier_round_is_kept_and_a_new_round_asks_again(env):
    pipeline, pid, _sent, _ = env
    first = run_collect(pipeline, pid)
    with pipeline.store._connect() as db:
        row = db.execute("SELECT call_key FROM studio_job_calls WHERE job_id=? AND kind='open_data' ORDER BY rowid LIMIT 1",
                         (first.job['id'],)).fetchone()
        db.execute("UPDATE studio_job_calls SET status='unknown' WHERE job_id=? AND call_key=?", (first.job['id'], row[0]))
        db.execute("UPDATE studio_jobs SET status='done' WHERE kind='analyze' AND project_id=?", (pid,))
    second = run_collect(pipeline, pid)
    assert second.read_checkpoint(collect_job.RESULT)['status'] == 'complete'
    with pipeline.store._connect() as db:
        # The old receipt stays unknown (counted, never settled); the new round is its own request.
        assert db.execute('SELECT status FROM studio_job_calls WHERE job_id=? AND call_key=?',
                          (first.job['id'], row[0])).fetchone()[0] == 'unknown'
        assert db.execute('SELECT status FROM studio_job_calls WHERE job_id=? AND call_key=?',
                          (second.job['id'], row[0])).fetchone()[0] == 'completed'
    assert research_sources(pipeline.store, pid, 'patent')['total'] == 3


def _model_answers(pipeline, answer):
    """Route model requests to a fixed answer; every other provider keeps the fixture's behaviour."""
    inner = pipeline.http_factory(None).transport
    calls = []

    def send(method, url, headers, body, timeout):
        if urlsplit(url).hostname != 'model.example.invalid':
            return inner(method, url, headers, body, timeout)
        calls.append(json.loads(body))
        content = json.dumps(answer)
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]}).encode())
    pipeline.http_factory = lambda _: Http(send)
    return calls


def test_old_paper_topics_are_rewritten_once_and_kept_for_audit(env):
    pipeline, pid, sent, _ = env
    pipeline.store.put_plan(pid, {'competitors': [], 'paper_queries': ['STATE', '合成生物平台']})
    calls = _model_answers(pipeline, {'paper_queries': ['virtual cell perturbation model', 'X-Suite']})
    run_collect(pipeline, pid)
    plan = pipeline.store.get_plan(pid)
    assert plan['paper_queries'] == ['virtual cell perturbation model']  # what fails the topic rules is dropped
    assert plan['paper_queries_previous'] == ['STATE', '合成生物平台'] and plan['paper_topics_version'] == collect_job.PAPER_TOPICS_VERSION
    assert 'virtual cell perturbation model' in ' '.join(queries(sent, 'api.openalex.org'))
    assert not any('STATE' in q for q in queries(sent, 'api.openalex.org'))
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE kind='analyze' AND project_id=?", (pid,))
    before = len(calls)
    run_collect(pipeline, pid)
    assert len(calls) == before  # rewritten once, not every round


def test_topics_that_already_pass_are_kept_without_a_model_request(env):
    pipeline, pid, _sent, _ = env
    pipeline.store.put_plan(pid, {'competitors': [], 'paper_queries': ['synthetic antibody conjugate']})
    calls = _model_answers(pipeline, {'paper_queries': ['should not be used']})
    run_collect(pipeline, pid)
    plan = pipeline.store.get_plan(pid)
    assert plan['paper_queries'] == ['synthetic antibody conjugate'] and plan['paper_topics_version'] == collect_job.PAPER_TOPICS_VERSION
    assert not calls


def test_a_plan_without_research_topics_gets_some(env):
    pipeline, pid, sent, _ = env
    pipeline.store.put_plan(pid, {'competitors': []})
    _model_answers(pipeline, {'paper_queries': ['synthetic antibody conjugate']})
    run_collect(pipeline, pid)
    assert pipeline.store.get_plan(pid)['paper_queries'] == ['synthetic antibody conjugate']
    assert 'synthetic antibody conjugate' in ' '.join(queries(sent, 'api.openalex.org'))


def test_a_stale_paper_topic_sits_a_round_out(env, monkeypatch):
    from project_watch.studio import round_quality

    pipeline, pid, sent, _ = env
    pipeline.store.put_plan(pid, {'competitors': [], 'paper_queries': ['synthetic antibody conjugate', 'synthetic noisy topic'],
                                  'paper_topics_version': collect_job.PAPER_TOPICS_VERSION})
    monkeypatch.setattr(round_quality, 'stale_queries', lambda store, project: [
        {'source': 'openalex_topic', 'query': 'synthetic noisy topic', 'category': 'paper', 'collected': 9, 'screened': 9, 'rounds': 3}])
    execution = run_collect(pipeline, pid)
    frozen = execution.read_checkpoint(collect_job.INPUT)
    assert frozen['public_plan']['resting_paper_queries'] == ['synthetic noisy topic']
    sent_topics = ' '.join(queries(sent, 'api.openalex.org'))
    assert 'synthetic antibody conjugate' in sent_topics and 'synthetic noisy topic' not in sent_topics


def test_resume_keeps_frozen_manifest_after_scheduler_upgrade(env, monkeypatch):
    pipeline, pid, sent, _ = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    job = pipeline.store.claim()
    pipeline.run(job)
    execution = JobExecution(pipeline.store, job)
    before = execution.read_checkpoint(collect_job.STAGE)
    calls = len(sent)
    monkeypatch.setattr(collect_job, 'build_collection_plan',
                        lambda **kwargs: pytest.fail('saved round must not be replanned'))
    pipeline.run(job)
    assert execution.read_checkpoint(collect_job.STAGE) == before
    assert len(sent) == calls


def test_resume_rejects_manifest_bound_to_different_inputs(env):
    pipeline, pid, _sent, _ = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    job = pipeline.store.claim()
    pipeline.run(job)
    execution = JobExecution(pipeline.store, job)
    saved = execution.read_checkpoint(collect_job.STAGE)
    saved['plan']['binding'] = 'wrong-binding'
    execution.checkpoint_chunked(collect_job.STAGE, saved)
    with pytest.raises(ValueError, match='frozen inputs'):
        pipeline.run(job)


def test_a_blocked_news_feed_fails_only_its_own_source(env):
    pipeline, pid, sent, _ = env
    original = pipeline.http_factory(None).transport

    def send(method, url, headers, body, timeout):
        if urlsplit(url).hostname == 'www.bing.com':  # an HTML challenge page instead of RSS
            sent.append(url)
            return Response(200, {}, b'<html><body>verify you are human</body></html>')
        return original(method, url, headers, body, timeout)
    pipeline.http_factory = lambda _: Http(send)
    execution = run_collect(pipeline, pid)
    result = execution.read_checkpoint(collect_job.RESULT)
    assert result['status'] == 'partial'
    assert any(r['source'] == 'search' and r['reason'] == 'invalid_feed' for r in result['not_collected'])
    assert research_sources(pipeline.store, pid, 'patent')['total'] == 3  # the other sources still ran


def test_a_round_that_raises_still_waits_for_its_cadence(env, monkeypatch):
    from project_watch.studio import signals

    pipeline, pid, sent, _ = env
    # The crash comes after the paid and public requests of this round were sent.
    monkeypatch.setattr(collect_job, 'read_bodies', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic crash')))
    started = now()
    pipeline.store.enqueue(pid, 'collect', input_version='scheduled:' + now())
    job = pipeline.store.claim()
    with pytest.raises(RuntimeError):
        pipeline.run(job)
    pipeline.store.finish(job['id'], error='内部错误：RuntimeError', lease_token=job['lease_token'])
    assert sent and pipeline.store.get_state('external_attempt:' + pid) >= started
    # The scheduler does not start a replacement round with a fresh request journal.
    assert pid not in signals.due_external(pipeline.store, pipeline.monitor, 24)


def _offline(pipeline, sent, hosts=None):
    from project_watch.studio.http import HttpError

    original = pipeline.http_factory(None).transport

    def send(method, url, headers, body, timeout):
        if hosts is None or urlsplit(url).hostname in hosts:
            sent.append(url)
            raise HttpError('connection_not_sent', transport_phase='connect_or_headers', failure_kind='transport_error')
        return original(method, url, headers, body, timeout)
    pipeline.http_factory = lambda _: Http(send)
    return original


def test_an_offline_round_waits_in_the_same_job_and_finishes_when_back_online(env):
    from project_watch.studio.http import HttpError

    pipeline, pid, sent, _ = env
    original = _offline(pipeline, sent)
    pipeline.store.enqueue(pid, 'collect', input_version='scheduled:' + now())
    job = pipeline.store.claim()
    with pytest.raises(HttpError, match='connection_not_sent'):
        pipeline.run(job)
    assert pipeline.store.defer_recoverable(job, 'connection_not_sent')  # as the worker does
    with pipeline.store._connect() as db:
        row = db.execute('SELECT status,retry_after FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()
        assert row['status'] == 'queued' and row['retry_after'] > now()
        assert not db.execute("SELECT 1 FROM studio_job_calls WHERE job_id=? AND status NOT IN ('not_sent')",
                              (job['id'],)).fetchone()  # nothing was transmitted or counted
        assert not db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND kind='analyze'", (pid,)).fetchone()
    # Back online: the same job resumes on its frozen plan, not a new round a cycle later.
    pipeline.http_factory = lambda _: Http(original)
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_provider_backoff SET retry_after='2000-01-01'")
        db.execute("UPDATE studio_jobs SET retry_after=NULL WHERE id=?", (job['id'],))
    resumed = pipeline.store.claim()
    assert resumed['id'] == job['id']
    pipeline.run(resumed)
    result = JobExecution(pipeline.store, resumed).read_checkpoint(collect_job.RESULT)
    assert result['status'] in ('complete', 'partial') and not result['outcomes'].get('failed')
    assert research_sources(pipeline.store, pid, 'patent')['total'] == 3
    with pipeline.store._connect() as db:
        assert db.execute("SELECT input_version FROM studio_jobs WHERE project_id=? AND kind='analyze'",
                          (pid,)).fetchone()[0] == 'collect:' + job['id']


def test_one_unreachable_provider_does_not_hold_back_the_rest(env):
    pipeline, pid, sent, _ = env
    _offline(pipeline, sent, hosts={'api.openalex.org'})
    execution = run_collect(pipeline, pid)
    result = execution.read_checkpoint(collect_job.RESULT)
    assert result['status'] == 'partial'
    reasons = {r['reason'] for r in result['not_collected'] if r['source'].startswith('openalex')}
    assert reasons and reasons <= {'connection_not_sent', 'provider_backoff'}
    assert research_sources(pipeline.store, pid, 'patent')['total'] == 3
    with pipeline.store._connect() as db:
        assert db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND kind='analyze'", (pid,)).fetchone()


def test_a_pass_that_only_meets_connection_backoffs_still_waits(env):
    """Another project's round (or this job after a yield) left only backoffs to meet."""
    from project_watch.studio import spine
    from project_watch.studio.http import HttpError

    pipeline, pid, sent, _ = env
    _offline(pipeline, sent)
    pipeline.store.enqueue(pid, 'collect', input_version='scheduled:' + now())
    job = pipeline.store.claim()
    with pytest.raises(HttpError):
        pipeline.run(job)
    assert pipeline.store.defer_recoverable(job, 'connection_not_sent')
    with pipeline.store._connect() as db:
        assert db.execute('SELECT stage FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()[0].startswith('网络连接未建立')
    attempts = len(sent)
    # The owner's 更新 resumes the waiting round now and clears only connection backoffs.
    assert spine.request_round(pipeline.store, pid, 'manual')['id'] == job['id']
    with pipeline.store._connect() as db:
        assert db.execute('SELECT retry_after FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()[0] is None
        assert not db.execute('SELECT 1 FROM studio_provider_backoff WHERE status=0').fetchone()
        # Re-create the shared backoffs another project's offline round would leave behind.
        db.execute("INSERT INTO studio_provider_backoff SELECT DISTINCT provider,'2999-01-01',0 FROM studio_job_calls")
    again = pipeline.store.claim()
    assert again['id'] == job['id']
    with pytest.raises(HttpError, match='connection_not_sent'):
        pipeline.run(again)  # every unit met a backoff; still offline, not a failed round
    assert len(sent) == attempts  # backoffs sent nothing


def test_a_long_outage_settles_the_round_as_before(env, monkeypatch):
    from project_watch.studio import collection_runner

    pipeline, pid, sent, _ = env
    _offline(pipeline, sent)
    monkeypatch.setattr(collection_runner, 'OFFLINE_WAIT_HOURS', -1)
    execution = run_collect(pipeline, pid)
    result = execution.read_checkpoint(collect_job.RESULT)
    assert result['status'] == 'failed'
    assert {r['reason'] for r in result['not_collected']} <= {'connection_not_sent', 'provider_backoff'}
