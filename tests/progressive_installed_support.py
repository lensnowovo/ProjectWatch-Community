"""Installed-product material -> event -> fixed insight acceptance, no network."""
import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from project_watch.studio import dsh_runtime, event_insight, insight
from project_watch.studio.app import create_studio_app
from project_watch.studio.collection_view import discovered_sources
from project_watch.studio.http import Http, Response
from project_watch.studio.pipeline import StudioWorker
from project_watch.studio.settings import ModelConfig, SearchConfig

TEXT = 'Synthetic Host develops SYN-ALPHA.'


def run(home):
    pipeline = create_studio_app(home, start_worker=False).state.studio_pipeline
    settings = pipeline.settings_store.load()
    settings.model = ModelConfig(base_url='https://model.example.invalid/v1', model='synthetic', api_key='synthetic')
    settings.search = SearchConfig('tavily', 'synthetic')
    pipeline.settings_store.save(settings)
    project = pipeline.repository.create_project('Synthetic Host')
    pipeline.store.upsert_project(project.id, {'legal_name': project.name}, consent=True)
    pipeline.store.touch(project.id, 'last_external_at')
    seen = {'body': 0, 'materials': 0, 'reviews': 0, 'analysis': 0}
    active = {}

    def tool(name, args):
        return {'role': 'assistant', 'tool_calls': [{'id': 'synthetic', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(args)}}]}

    def send(_method, url, _headers, raw, _timeout):
        host = urlsplit(url).hostname
        if host == 'api.tavily.com':
            data = {'results': [{'title': 'Synthetic public source', 'url': 'https://public.example.invalid/item',
                                 'content': 'Short public discovery summary'}]}
        elif host == 'api.openalex.org':
            data = {'results': [], 'meta': {'count': 0}}
        elif host == 'www.cninfo.com.cn':
            data = {'announcements': [], 'totalAnnouncement': 0}
        else:
            assert host == 'model.example.invalid', host
            body = json.loads(raw)
            names = {t['function']['name'] for t in body.get('tools', [])}
            if not names:
                seen['reviews'] += 1
                payload = json.loads(body['messages'][-1]['content'])
                if payload.get('phase') == 'consistency':
                    review = {'consistent': True, 'reason': 'Synthetic fixture', 'conflicts': []}
                else:
                    checks = []
                    for path in payload['fields']:
                        refs = payload.get('bound_evidence', {}).get(path, payload['evidence'])
                        ref = next(r for r in refs if payload['evidence'][r].get('context'))
                        checks.append(dict(field=path, status='supported', reason='Synthetic fixture',
                                           evidence=[dict(ref=ref, quote=payload['evidence'][ref]['context'])]))
                    review = {'checks': checks}
                message = {'role': 'assistant', 'content': json.dumps(review)}
            elif 'pw_collect' in names:
                message = tool('pw_finish', {'gaps': {}})
            elif names == {'pw_submit_records'}:
                seen['materials'] += 1
                material = json.loads(active['task'])
                ref, part = next((r, p) for r, p in material['refs'].items() if TEXT in p['quote'])
                start = part['quote'].index(TEXT)
                message = tool('pw_submit_records', {'observations': [dict(kind='company', subject='Synthetic Host',
                    label='Product development', text=TEXT, refs=[ref], start=start, end=start + len(TEXT))]})
            else:
                assert names == set(event_insight.TOOLS), names
                seen['analysis'] += 1
                contexts = [json.loads(m['content']) for m in body['messages'] if m['role'] == 'tool']
                context = next((c for c in contexts if 'numbered' in c), None)
                message = tool('pw_context', {}) if context is None else tool('pw_judge', {'analysis_json': json.dumps(
                    dict(headline=TEXT, operations=dict(verdict='improving', summary=TEXT, **{'for': [dict(text=TEXT, refs=['N1'])]})))})
            data = {'choices': [{'message': message, 'finish_reason': 'tool_calls' if names else 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}
        return Response(200, {}, json.dumps(data).encode())

    def read(_url):
        assert discovered_sources(pipeline.store, project.id)['total'] == 1
        assert seen['materials'] == 0
        seen['body'] += 1
        return {'status': 'article_body', 'text': TEXT}

    def role(research, gateway, *_args, **_kwargs):
        active['task'] = research.task
        messages = [{'role': 'user', 'content': research.task}]
        for _ in range(6):
            response = gateway.model_call({'model': gateway.model.model, 'messages': messages,
                'tools': [{'function': {'name': name}} for name in research.tools]})
            message = response['choices'][0]['message']
            messages.append(message)
            for call in message.get('tool_calls', []):
                function = call['function']
                result = research.tool(function['name'], json.loads(function['arguments']))
                messages.append(dict(role='tool', name=function['name'], tool_call_id=call['id'], content=json.dumps(result)))
            if research.state['finished']:
                return research.state['finished']
        raise AssertionError('synthetic role did not finish')

    pipeline.http_factory = lambda _: Http(send)
    pipeline.reader = lambda: SimpleNamespace(read=read)
    worker = StudioWorker(pipeline)
    worker.schedule = lambda: None
    with patch.dict('os.environ', {'PROJECT_WATCH_DSH_RUNTIME': 'synthetic-installed-runtime', 'PROJECT_WATCH_DSH_MONITOR': '1'}), patch.object(dsh_runtime, 'run_dsh', role):
        pipeline.store.enqueue(project.id, 'external')
        assert worker.step()
        assert pipeline.store.list_events(project_id=project.id) and not insight.load(pipeline.store, project.id)
        assert worker.step()
    with closing(pipeline.store._connect()) as db:
        jobs = [dict(r) for r in db.execute('SELECT kind,status,error FROM studio_jobs ORDER BY rowid')]
    assert [(j['kind'], j['status']) for j in jobs] == [('external', 'done'), ('insight', 'done')], jobs
    result = insight.load(pipeline.store, project.id)
    assert result['status'] == 'current' and result['review']['passed'] and result['input']['event_versions']
    assert seen['body'] == seen['materials'] == 1 and seen['reviews'] >= 2
    return {'jobs': jobs, 'counts': seen, 'immutable_insight': result['id']}
