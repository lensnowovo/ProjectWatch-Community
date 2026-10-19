"""Cold-start competitor leads in any domain: synthetic, not a live benchmark."""

import json
from types import SimpleNamespace


def lead_pipeline(tmp_path, *, industry='激光雷达研发制造', snippet='Alpha Lidar Co and Beta Sensing compete in lidar'):
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.http import Http, Response

    app = create_studio_app(tmp_path, start_worker=False)
    pipeline, store = app.state.studio_pipeline, app.state.studio_store
    pid = pipeline.repository.create_project('Synthetic Sensor').id
    store.upsert_project(pid, {'industry': industry}, consent=True)
    pipeline.settings_store.update({'model': {'protocol': 'openai', 'base_url': 'https://synthetic.example/v1',
        'model': 'fixture', 'api_key': 'k'}, 'search': {'provider': 'tavily', 'api_key': 's'}})
    pipeline.objects.ensure_subject(pid)
    calls = {'search': [], 'model': [], 'registry': []}

    def transport(method, url, headers, body, timeout):
        if 'clinicaltrials.gov' in url:
            calls['registry'].append(url)
            return Response(200, {}, b'{"studies": []}')
        data = json.loads(body)
        if 'api.tavily.com' in url:
            calls['search'].append(data['query'])
            return Response(200, {}, json.dumps({'results': [
                {'title': 'Lidar market', 'url': 'https://news.test/lidar', 'content': snippet}]}).encode())
        context = json.loads(data['messages'][1]['content'])
        calls['model'].append(context)
        answer = {'candidates': [{'name': n, 'source': 'D1', 'quote': snippet}
                                 for n in ('Alpha Lidar Co', 'Beta Sensing', 'Lidar Research Hospital') if n in snippet]} \
            if 'product_contexts' in context else {'products': []}
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}]}).encode())

    pipeline.http_factory = lambda _: Http(transport)
    pipeline.article_reader = SimpleNamespace(read=lambda url: {'status': 'unavailable', 'text': '', 'error_code': 'fetch_failed'})
    return pipeline, pid, calls


def test_project_without_products_still_gets_located_company_leads(tmp_path):
    from project_watch.studio.competition_research import research_inputs

    pipeline, pid, calls = lead_pipeline(tmp_path)
    inputs = research_inputs(pipeline, pid)
    assert inputs['mode'] == 'company_hint' and inputs['products'] == []
    assert inputs['queries'] == ['Synthetic Sensor 竞争对手', '激光雷达 厂商 竞争格局']
    job = pipeline.schedule_competition(pid)
    assert job
    claimed = pipeline.store.claim()
    pipeline.run(claimed)
    saved = pipeline.competition_research.saved(pid)
    names = [c['name'] for c in saved['candidates'] if c['origin'] == 'search']
    assert names == ['Alpha Lidar Co', 'Beta Sensing']
    assert saved['web_peer_research'] == 'skipped_company_hint_leads'
    assert calls['model'][0]['industry_hint'] == '激光雷达研发制造'
    # Two discovery searches (no product yet), one discovery model call; no per-peer searches or registry.
    assert len(calls['search']) == 2 and len(calls['model']) == 1 and calls['registry'] == []
    assert saved['supported_count'] == 0


def test_without_products_or_industry_nothing_is_scheduled(tmp_path):
    pipeline, pid, calls = lead_pipeline(tmp_path, industry='')
    assert pipeline.schedule_competition(pid) is None
    assert calls == {'search': [], 'model': [], 'registry': []}


def test_quotes_differing_only_in_whitespace_locate_the_source_span():
    snippet = '办公软件的厂家主要包括 微软 、苹果等。其中, 微软是最知名的厂家之一 ,其套件广泛应用'
    from project_watch.studio.competition_research import whitespace_span

    assert whitespace_span('微软是最知名的厂家之一,其套件广泛应用', snippet) is not None
    assert whitespace_span('微软是最出名的厂家之一,其套件广泛应用', snippet) is None  # wording must match
    assert whitespace_span('微软', snippet) == (snippet.index('微软'), snippet.index('微软') + 2)


def test_industry_core_keeps_the_leading_noun_phrase():
    from project_watch.studio.competition_research import industry_core

    assert industry_core('激光雷达及激光气体传感器研发制造') == '激光雷达'
    assert industry_core('办公软件开发') == '办公软件'
    assert industry_core('通信电子设备制造（天线、连接器相关）') == '通信电子设备 天线 连接器'
    assert industry_core('AI for Science / 人工智能与分子模拟') == 'AI for Science'
    assert industry_core('计算机、通信和其他电子设备制造业') == '计算机 通信 电子设备'


def test_the_targets_own_product_alias_is_not_a_lead(tmp_path):
    pipeline, pid, _ = lead_pipeline(tmp_path, snippet='Sensorix Pro and Beta Sensing compete in lidar')
    pipeline.store.upsert_project(pid, {'industry': '激光雷达研发制造', 'aliases': ['Sensorix Pro']}, consent=True)
    import json as _json
    original = pipeline.http_factory(None).transport
    def transport(method, url, headers, body, timeout):
        if 'synthetic.example' in url:
            context = _json.loads(_json.loads(body)['messages'][1]['content'])
            if 'product_contexts' in context:
                from project_watch.studio.http import Response
                quote = 'Sensorix Pro and Beta Sensing compete in lidar'
                answer = {'candidates': [{'name': n, 'source': 'D1', 'quote': quote} for n in ('Sensorix Pro', 'Beta Sensing')]}
                return Response(200, {}, _json.dumps({'choices': [{'message': {'content': _json.dumps(answer)}}]}).encode())
        return original(method, url, headers, body, timeout)
    from project_watch.studio.http import Http
    pipeline.http_factory = lambda _: Http(transport)
    pipeline.schedule_competition(pid)
    pipeline.run(pipeline.store.claim())
    saved = pipeline.competition_research.saved(pid)
    assert [c['name'] for c in saved['candidates'] if c['origin'] == 'search'] == ['Beta Sensing']
    assert saved['candidate_rejections'] == {'excluded_or_unquoted_name': 1}


def test_alias_names_the_company_when_no_short_name_and_leads_persist(tmp_path):
    from project_watch.studio.competition_research import research_inputs

    pipeline, pid, _ = lead_pipeline(tmp_path)
    pipeline.store.upsert_project(pid, {'industry': '激光雷达研发制造', 'short_name': '', 'aliases': ['SenseCo', '600001']},
                                  consent=True)
    assert research_inputs(pipeline, pid)['queries'][0] == 'SenseCo 竞争对手'
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    pipeline.run(job)
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    first = [c['name'] for c in pipeline.competition_research.saved(pid)['candidates']]
    # A later round that finds nothing keeps the earlier located leads.
    pipeline.store.upsert_project(pid, {'industry': '激光雷达设备', 'short_name': '', 'aliases': ['SenseCo']}, consent=True)
    import json as _json

    from project_watch.studio.http import Http, Response
    def empty(method, url, headers, body, timeout):
        data = _json.loads(body)
        if 'api.tavily.com' in url:
            return Response(200, {}, b'{"results": []}')
        return Response(200, {}, _json.dumps({'choices': [{'message': {'content': _json.dumps(
            {'candidates': []} if 'product_contexts' in _json.loads(data['messages'][1]['content']) else {'products': []})}}]}).encode())
    pipeline.http_factory = lambda _: Http(empty)
    assert pipeline.schedule_competition(pid)
    pipeline.run(pipeline.store.claim())
    again = pipeline.competition_research.saved(pid)['candidates']
    assert [c['name'] for c in again] == first and all(c.get('carried_from_previous_round') for c in again)


def test_product_mode_uses_the_industry_phrase_and_company_query(tmp_path):
    from test_studio_competition_research import research_pipeline

    from project_watch.studio.competition_research import research_inputs

    pipeline, pid, _, _ = research_pipeline(tmp_path)
    assert research_inputs(pipeline, pid)['queries'][1] == 'drug screening pharma 竞品'  # no industry: second use query
    pipeline.store.upsert_project(pid, {'industry': '药物筛选设备制造'}, consent=True)
    queries = research_inputs(pipeline, pid)['queries']
    assert queries == ['Lens A drug screening pharma 竞品', '药物筛选设备 厂商 竞争格局', 'Synthetic Optics 竞争对手']



def test_a_lead_name_found_in_two_sources_is_listed_once(tmp_path):
    from project_watch.studio.http import Http, Response

    pipeline, pid, _ = lead_pipeline(tmp_path)
    snippet = 'Alpha Lidar Co and Beta Sensing compete in lidar'

    def transport(method, url, headers, body, timeout):
        data = json.loads(body)
        if 'api.tavily.com' in url:
            # Each query returns its own page, so the same names are located twice.
            return Response(200, {}, json.dumps({'results': [{'title': 'Lidar', 'content': snippet,
                'url': f"https://news.test/{abs(hash(data['query']))}"}]}).encode())
        context = json.loads(data['messages'][1]['content'])
        answer = {'candidates': [{'name': n, 'source': s['source'], 'quote': snippet}
                                 for s in context.get('sources', []) for n in ('Alpha Lidar Co', 'Beta Sensing')]} \
            if 'product_contexts' in context else {'products': []}
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}]}).encode())

    pipeline.http_factory = lambda _: Http(transport)
    pipeline.schedule_competition(pid)
    pipeline.run(pipeline.store.claim())
    saved = pipeline.competition_research.saved(pid)
    assert saved['candidate_count'] == 4  # provenance objects stay one per source
    assert [c['name'] for c in saved['candidates']] == ['Alpha Lidar Co', 'Beta Sensing']
