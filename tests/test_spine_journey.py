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
    job = pipeline.store.claim(lane='round')
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
    # 公司本体 is retired for new analyses: never drafted, never reported as kept for lack of material.
    assert result['status'] == 'partial' and set(result['published']) == {'chain'}
    publication = analysis.read_checkpoint(analyze_job.PUBLICATION)
    assert 'operations' not in publication['targets'] and publication['sections']['operations']['status'] == 'retired'
    report = work(pipeline, 'report')
    assert report.read_checkpoint('report_result_v1')['status'] == 'complete'
    # Papers and patents are reviewed and labelled; the company's own are marked as such.
    rows = research_sources(pipeline.store, pid, 'paper')['items'] + research_sources(pipeline.store, pid, 'patent')['items']
    assert rows and all(r['processing_status'] == 'organized' for r in rows)
    assert {r['extra'].get('research_relation') for r in rows} >= {'subject'}
    view = insight.load(pipeline.store, pid)
    assert view['analysis']['chain']['summary'] == section()['summary']
    assert not view['analysis']['operations']['summary']  # retired: nothing new is published
    # Analysis never collected: no provider request was made by the analyze job.
    with pipeline.store._connect() as db:
        kinds = {r[0] for r in db.execute('SELECT DISTINCT kind FROM studio_job_calls WHERE job_id=?', (analysis.job['id'],))}
    assert kinds == {'model'}


def test_historical_5xx_analysis_is_redone_from_saved_material_without_collecting(env):
    pipeline, pid, state = env
    state['fail_draft'] = 1  # the provider answers the first draft request with a 500
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    collected = work(pipeline, 'collect')
    with pytest.raises(ModelError, match='500'):
        work(pipeline, 'analyze')
    with pipeline.store._connect() as db:
        summary = db.execute("SELECT input_version,status FROM studio_jobs WHERE project_id=? AND kind='page_summary'",
                             (pid,)).fetchone()
        assert summary and summary['input_version'].startswith('analyze:') and summary['status'] == 'queued'
    # Simulate a receipt predating automatic recovery permission.
    with pipeline.store._connect() as db:
        db.execute('UPDATE studio_job_calls SET retryable=0 WHERE response_status=500')
        db.execute('DELETE FROM studio_provider_backoff')
        failed_calls = [tuple(row) for row in db.execute(
            'SELECT * FROM studio_job_calls WHERE response_status=500 ORDER BY job_id,call_key')]
    sent_before = list(state['sent'])
    # An automatic re-run may not resend the 5xx-answered draft request.
    pipeline.store.enqueue(pid, 'analyze', input_version='collect:' + collected.job['id'])  # e.g. a re-trigger
    held = work(pipeline, 'analyze')
    assert held.read_checkpoint(analyze_job.RESULT)['status'] == 'partial'
    from project_watch.studio.step_outcomes import CHECKPOINT
    assert held.read_checkpoint(CHECKPOINT)  # Failed request remains blocked; independent work can finish.
    with pipeline.store._connect() as db:
        assert [tuple(row) for row in db.execute(
            'SELECT * FROM studio_job_calls WHERE response_status=500 ORDER BY job_id,call_key')] == failed_calls
        assert not db.execute('SELECT 1 FROM studio_job_calls WHERE job_id=? AND call_key IN '
            '(SELECT call_key FROM studio_job_calls WHERE response_status=500)', (held.job['id'],)).fetchone()
    work(pipeline, 'report')  # The partial analysis now has a truthful report to finish.
    work(pipeline, 'brief')
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


def test_large_rounds_are_triaged_in_bounded_chunks():
    seen = []

    class Pipeline:
        def triage(self, pid, llm, project, plan, stage, *, item_ids):
            seen.append(item_ids)

    ids = {f'i{n:03}' for n in range(450)}
    analyze_job.triage_all(Pipeline(), 'p', None, {}, {}, lambda _t: None, ids)
    assert [len(chunk) for chunk in seen] == [200, 200, 50]
    assert set().union(*seen) == ids


def test_triage_binding_tolerates_form_but_not_content():
    from project_watch.studio.pipeline import _quote_bound, _title_bound

    source = 'Moderna 宣布：mRNA-4157 联合疗法在 III 期试验中达到主要终点，复发风险降低 49%。公司将于 2026 年提交上市申请。'
    # Same words, different spacing / punctuation / width / case: bound.
    assert _quote_bound('mRNA-4157联合疗法在III期试验中达到主要终点', source, 10)
    assert _quote_bound('ＭＯＤＥＲＮＡ 宣布 mRNA 4157', source, 10)
    # Verbatim pieces joined by an ellipsis, in order: bound; out of order or invented: not.
    assert _quote_bound('达到主要终点……公司将于 2026 年提交上市申请', source, 10)
    assert not _quote_bound('公司将于 2026 年提交上市申请……达到主要终点', source, 10)
    assert not _quote_bound('复发风险降低 60%', source, 10)
    assert not _quote_bound('试验成功，前景广阔', source, 10)  # paraphrase
    # Titles: equal in form, cut short (>=85%), or the lead of a patent result line.
    assert _title_bound('Moderna Q3 results &amp; outlook', 'Moderna Q3 results & outlook')
    assert _title_bound('Systems and methods for seizure prediction - CN115089196A (en) *',
                        'Systems and methods for seizure prediction - CN115089196A (en) * | 2022-08-22 | Synthetic Co')
    assert not _title_bound('Moderna Q3', 'Moderna Q3 results and full-year outlook')
    assert not _title_bound('BioNTech Q3 results', 'Moderna Q3 results')


def test_a_round_reports_its_own_quality(env):
    from project_watch.studio import round_quality, spine_api

    pipeline, pid, _state = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    work(pipeline, 'collect')
    work(pipeline, 'analyze')
    quality = spine_api.round_view(pipeline.store, pid)['quality']
    current = quality['current']
    assert current['items'] > 0 and current['screened'] == current['items']
    assert current['sections']['published'] == 1 and current['sections']['unchanged'] == []  # a first round redoes all
    assert current['unclear_requests'] == 0 and quality['previous'] is None
    yields = round_quality.unit_yield(pipeline.store, current['collect_job_id'])
    assert sum(u['collected'] for u in yields) >= current['items'] and all(u['source'] and 'query' in u for u in yields)


def test_a_query_that_never_brings_anything_relevant_is_named(monkeypatch):
    from project_watch.studio import round_quality

    monkeypatch.setattr(round_quality, '_collect_jobs', lambda db, pid, limit: [
        {'id': f'c{i}', 'created_at': f'2026-10-0{i}', 'finished_at': ''} for i in range(1, 4)])
    rows = {'openalex_topic': dict(source='openalex_topic', category='paper', query='"Synthetic Tool"', collected=3, screened=3, relevant=0),
            'search': dict(source='search', category='company', query='合成公司', collected=3, screened=3, relevant=1)}
    monkeypatch.setattr(round_quality, 'unit_yield', lambda store, job: list(rows.values()))
    store = type('S', (), {'_connect': lambda self: __import__('sqlite3').connect(':memory:')})()
    stale = round_quality.stale_queries(store, 'p')
    assert [(s['source'], s['query'], s['screened']) for s in stale] == [('openalex_topic', '"Synthetic Tool"', 9)]
    rows['openalex_topic']['collected'] = rows['openalex_topic']['screened'] = 1  # 3 screened over 3 rounds: too few to judge
    assert round_quality.stale_queries(store, 'p') == []


def test_verdicts_that_do_not_match_their_source_are_screened_again(env):
    pipeline, pid, state = env
    state['misalign_triage'] = 1
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    work(pipeline, 'collect')
    work(pipeline, 'analyze')
    with pipeline.store._connect() as db:
        statuses = [json.loads(r[0] or '{}').get('triage_binding', {}).get('status')
                    for r in db.execute('SELECT extra FROM studio_items WHERE project_id=? AND triaged=1', (pid,))]
    assert statuses and all(s == 'bound' for s in statuses)  # the misaligned batch was screened again
    assert state['triage_calls'] >= 2



def test_an_automatic_round_redoes_only_the_sections_its_new_material_touches(env):
    from project_watch.studio import analyze_job

    pipeline, pid, _state = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    collect = work(pipeline, 'collect')
    work(pipeline, 'analyze')
    work(pipeline, 'report')
    # This collection's new relevant material was papers and patents only.
    assert analyze_job.affected_sections(pipeline.store, pid, collect.job['id']) <= {'technology', 'position'}
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE kind IN ('brief','landscape') AND project_id=?", (pid,))
    # A second automatic round brings nothing new: published sections are kept, not redrafted.
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    work(pipeline, 'collect')
    drafts = _state['drafts']
    second = work(pipeline, 'analyze')
    outcomes = second.read_checkpoint(analyze_job.PUBLICATION)['sections']
    assert outcomes['operations']['status'] == 'retired' and outcomes['chain']['status'] == 'unchanged'
    assert _state['drafts'] in (drafts, drafts + 1)  # only never-published sections may be drafted


@pytest.mark.parametrize('draft_protocol', [False, True])
def test_legacy_unknown_batch_cannot_be_bypassed_by_new_draft_protocol(env, draft_protocol):
    pipeline, pid, state = env
    old = pipeline.store.enqueue(pid, 'analyze', input_version='collect:legacy-batch')
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed' WHERE id=?", (old['id'],))
        if draft_protocol:
            db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                       (old['id'], analyze_job.PROTOCOL, json.dumps({'version': 2})))
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) VALUES (?,?,?,?,?,?)",
                   (old['id'], 'old-payload-hash', 'model', 'synthetic', 'unknown', now()))
        before = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls')]
    pipeline.store.enqueue(pid, 'analyze', input_version='collect:legacy-batch')
    with pytest.raises(ModelError, match='旧分析请求结果不明'):
        work(pipeline, 'analyze')
    assert state['sent'] == []
    with pipeline.store._connect() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls')] == before


def test_saved_analysis_snapshot_resumes_without_reentering_collection_preparation(env, monkeypatch):
    from project_watch.studio import event_fold

    pipeline, pid, state = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    work(pipeline, 'collect')
    execution = work(pipeline, 'analyze')
    snapshot = execution.read_checkpoint(analyze_job.SNAPSHOT)
    assert snapshot and snapshot['id']
    sent, drafts = list(state['sent']), state['drafts']
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE status='queued'")
        db.execute("UPDATE studio_jobs SET status='queued',lease_token='',lease_until=NULL WHERE id=?",
                   (execution.job['id'],))
        # Model a stop after publishing sections but before the final task receipt.
        db.execute('DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                   (execution.job['id'], analyze_job.RESULT))
        calls = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls ORDER BY job_id,call_key')]
    def already_completed(*args, **kwargs):
        pytest.fail('saved snapshot must not re-enter completed triage, clustering or folding')
    monkeypatch.setattr(pipeline, 'triage', already_completed)
    monkeypatch.setattr(pipeline, 'cluster', already_completed)
    monkeypatch.setattr(event_fold, 'fold', already_completed)
    resumed = work(pipeline, 'analyze')
    assert resumed.read_checkpoint(analyze_job.SNAPSHOT) == snapshot
    assert resumed.read_checkpoint(analyze_job.RESULT)
    assert state['sent'] == sent and state['drafts'] == drafts
    with pipeline.store._connect() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls ORDER BY job_id,call_key')] == calls

def test_rate_limit_resumes_same_analysis_after_worker_restart(env, monkeypatch):
    from project_watch.studio.http import HttpError
    from project_watch.studio.llm import LLM
    from project_watch.studio.pipeline import StudioWorker
    pipeline, pid, _ = env
    job = pipeline.store.enqueue(pid, 'analyze', input_version='collect:synthetic')
    sent = []
    def run(job):
        execution = JobExecution(pipeline.store, job)
        def transport(*args):
            sent.append(json.loads(args[3])['messages'][-1]['content'])
            if sent == ['one', 'two']:
                return Response(429, {}, b'{}')
            return Response(200, {}, b'{"choices":[{"message":{"content":"{}"}}]}')
        http = execution.http(Http(transport), lambda url: ('model','synthetic'))
        llm = LLM(pipeline.settings_store.load().model, http)
        for part in ('one','two'):
            llm.json('test', part)
        def fail():
            raise HttpError('connection_failed', failure_kind='timeout', transport_phase='connect_or_headers')
        with pytest.raises(HttpError):
            execution.call('model','uncertain',fail)
        with pytest.raises(dispatch.NotSent, match='unknown_not_resent'):
            execution.call('model','uncertain',lambda: pytest.fail('unexpected resend'))
    monkeypatch.setattr(pipeline, 'run', run)
    worker = StudioWorker(pipeline, lane='round', schedules=False)
    assert worker.step()
    with closing(pipeline.store._connect()) as db:
        saved = db.execute('select * from studio_jobs where id=?',(job['id'],)).fetchone()
        assert saved['status']=='queued' and saved['retry_after'] > now(), dict(saved)
    restarted = StudioWorker(pipeline, lane='round', schedules=False)
    assert not restarted.step()
    with closing(pipeline.store._connect()) as db, db:
        db.execute("update studio_jobs set retry_after=? where id=?", (now(),job['id']))
        db.execute("update studio_provider_backoff set retry_after=?", (now(),))
    assert restarted.step()
    assert sent == ['one','two','two']
    with closing(pipeline.store._connect()) as db:
        assert db.execute('select status from studio_jobs where id=?',(job['id'],)).fetchone()[0]=='done'
        assert db.execute("select count(*) from studio_job_calls where status='unknown' and job_id=?", (job['id'],)).fetchone()[0]==1

def test_an_analysis_that_started_before_the_retirement_keeps_its_section_list(env):
    """A shorter list is a different request; an analysis already under way must not switch."""
    pipeline, pid, _state = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    work(pipeline, 'collect')
    job = pipeline.store.claim(lane='round')
    assert job['kind'] == 'analyze'
    JobExecution(pipeline.store, job).checkpoint(analyze_job.PROTOCOL, {'version': 2})  # began before the upgrade
    pipeline.run(job)
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    execution = JobExecution(pipeline.store, job)
    assert execution.read_checkpoint(analyze_job.SECTION_SCOPE) == {'retired': []}
    assert 'operations' in execution.read_checkpoint(analyze_job.PUBLICATION)['targets']


def _redo(pipeline, pid, collect_id):
    with pipeline.store._connect() as db, db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE project_id=? AND status='queued' AND kind='report'", (pid,))
    pipeline.store.enqueue(pid, 'analyze', input_version=dispatch.REDO_PREFIX + collect_id)
    return work(pipeline, 'analyze')


def test_a_redo_inherits_the_original_list_of_its_collection_batch(env):
    pipeline, pid, _state = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    collect = work(pipeline, 'collect')
    first = work(pipeline, 'analyze')
    with pipeline.store._connect() as db, db:  # as if the first analysis predates the retirement
        db.execute("DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage=?",
                   (first.job['id'], analyze_job.SECTION_SCOPE))
    redo = _redo(pipeline, pid, collect.job['id'])
    assert redo.read_checkpoint(analyze_job.SECTION_SCOPE) == {'retired': []}
    assert 'operations' in redo.read_checkpoint(analyze_job.PUBLICATION)['targets']


def test_a_redo_of_a_retired_batch_stays_retired(env):
    pipeline, pid, _state = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    collect = work(pipeline, 'collect')
    work(pipeline, 'analyze')
    redo = _redo(pipeline, pid, collect.job['id'])
    assert redo.read_checkpoint(analyze_job.SECTION_SCOPE) == {'retired': ['operations']}
    assert 'operations' not in redo.read_checkpoint(analyze_job.PUBLICATION)['targets']


def test_progress_reads_a_retired_section_as_retired_from_any_checkpoint(env):
    """A partial run leaves only the in-progress delivery record, which lists it as unchanged."""
    from project_watch.studio import section_progress, spine_api
    from project_watch.studio.judgment_delivery import CURRENT

    pipeline, pid, _state = env
    pipeline.store.enqueue(pid, 'collect', input_version='manual:' + now())
    work(pipeline, 'collect')
    analysis = work(pipeline, 'analyze')
    with pipeline.store._connect() as db, db:
        db.execute('DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                   (analysis.job['id'], analyze_job.PUBLICATION))
        stale = {'status': 'partial', 'published': [], 'sections': {'operations': {'status': 'unchanged'}}}
        db.execute("INSERT OR REPLACE INTO studio_job_checkpoints VALUES (?,?,?)",
                   (analysis.job['id'], CURRENT, json.dumps(stale)))
        job = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (analysis.job['id'],)).fetchone())
        states, _ = section_progress.states(db, job)
    assert states['operations'] == 'retired'
    with pipeline.store._connect() as db, db:  # a final record written before the retired mark
        db.execute("INSERT OR REPLACE INTO studio_job_checkpoints VALUES (?,?,?)",
                   (analysis.job['id'], analyze_job.PUBLICATION, json.dumps(stale)))
    sections = spine_api.round_view(pipeline.store, pid)['quality']['current']['sections']
    assert 'operations' not in sections['unchanged']


def test_a_retired_section_reads_as_retired_not_as_a_failure():
    from project_watch.studio.section_progress import outcome_state
    assert outcome_state({'status': 'retired', 'reason': 'section no longer drafted'}) == 'retired'
