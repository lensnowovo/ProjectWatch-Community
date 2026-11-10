"""One synthetic round (collect → analyze → report) against an installed or source build.

Used by scripts/installed_wheel_smoke.py --full-journey and by test_release_ci.
Synthetic company and providers; the model is a synthetic endpoint answering by
system prompt over the real HTTP/journal path; the evidence reviewer is a
deterministic double. No network, no pytest.
"""
import json
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synthetic_providers import EMPTY_RSS, model_answer, paper, patent_html, patents

from project_watch.studio import analyze_job
from project_watch.studio.app import create_studio_app
from project_watch.studio.http import Http, Response
from project_watch.studio.research_sources import research_sources
from project_watch.studio.settings import ModelConfig

LEGAL = 'Synthetic Bio Inc.'
CONFIRMED = ['Synthetic Biologics (Suzhou) Co., Ltd.']


def _send(state):
    def send(method, url, headers, body, timeout):
        host, query = urlsplit(url).hostname, unquote_plus(urlsplit(url).query)
        state['sent'].append(host)
        if host == 'model.example.invalid':
            request = json.loads(body)
            answer = model_answer(request['messages'][0]['content'], request['messages'][1]['content'], state)
            return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(answer, ensure_ascii=False)},
                                                              'finish_reason': 'stop'}]}).encode())
        if host == 'www.ebi.ac.uk':
            data = patents() if 'SRC:PAT' in query else paper('Synthetic Biologics, Shanghai') if 'AFFILIATION' in query \
                else {'hitCount': 0, 'resultList': {'result': []}}
        elif host == 'patents.google.com':
            number = urlsplit(url).path.split('/')[2]
            return Response(200, {}, patent_html(number, applicant='Synthetic Biologics Suzhou Co Ltd', family=None).encode())
        elif host == 'api.openalex.org':
            data = {'results': [], 'meta': {'count': 0}}
        elif host == 'www.bing.com':
            return Response(200, {}, EMPTY_RSS)
        elif host == 'clinicaltrials.gov':
            data = {'studies': []}
        elif host == 'www.cninfo.com.cn':
            data = {'announcements': [], 'totalAnnouncement': 0}
        else:
            raise ConnectionError('unexpected provider in synthetic journey: ' + str(host))
        return Response(200, {}, json.dumps(data).encode())
    return send


def _reviewer(_pipeline, _execution, _llm, guard):
    def review(system, payload):
        guard()
        if 'conflicts' in system:
            return dict(consistent=True, reason='synthetic', conflicts=[])
        quote = next(iter(payload['evidence'].values()))['context'][:12]
        return dict(checks=[dict(field=f, status='supported', reason='synthetic source match',
                                 evidence=[dict(ref=next(iter(payload['evidence'])), quote=quote)])
                            for f in payload['fields']])
    return review


def run(home):
    pipeline = create_studio_app(home, start_worker=False).state.studio_pipeline
    settings = pipeline.settings_store.load()
    settings.model = ModelConfig(base_url='https://model.example.invalid/v1', model='synthetic', api_key='synthetic')
    pipeline.settings_store.save(settings)
    project = pipeline.repository.create_project('合成生物')
    pipeline.store.upsert_project(project.id, {'legal_name': LEGAL, 'short_name': '合成生物'}, consent=True)
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        pipeline.objects.confirm_subject_identity(project.id, LEGAL, CONFIRMED, db=db)
        db.execute("UPDATE studio_jobs SET status='done' WHERE project_id=?", (project.id,))
    state = {'drafts': 0, 'sent': []}
    pipeline.http_factory = lambda _: Http(_send(state))
    original, analyze_job._reviewer = analyze_job._reviewer, _reviewer
    try:
        from project_watch.studio import spine

        spine.request_round(pipeline.store, project.id, 'manual')
        kinds = []
        for expected in spine.ROUND_KINDS:
            job = pipeline.store.claim(lane='round')
            if not job or job['kind'] != expected:
                raise AssertionError(f'expected {expected} in the round queue')
            kinds.append(job['kind'])
            pipeline.run(job)
            pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    finally:
        analyze_job._reviewer = original
    round_view = __import__('project_watch.studio.spine_api', fromlist=['round_view']).round_view(pipeline.store, project.id)
    return {'jobs': kinds,
            'collect': round_view['collect']['result']['status'],
            'analyze': round_view['analyze']['result']['status'],
            'sections': sorted(round_view['analyze']['result']['published']),
            'materials': {k: research_sources(pipeline.store, project.id, k)['total'] for k in ('paper', 'patent')}}
