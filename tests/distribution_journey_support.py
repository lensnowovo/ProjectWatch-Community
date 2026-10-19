"""Deterministic fake HTTP boundary shared by source and installed-wheel smoke.

Only fictional companies, made-up keys, and in-memory provider responses.
Unknown endpoints fail closed; the real pipeline/worker/store are not stubbed.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

from project_watch.studio.app import create_studio_app
from project_watch.studio.http import Http, HttpError, Response

MODEL_KEY = 'synthetic-model-key-distribution-only'
SEARCH_KEY = 'synthetic-search-key-distribution-only'
COMPANY = '合成光学科技有限公司'
PEER = '合成星辰光学有限公司'
PLAN = {'company_queries': [COMPANY], 'people': [],
        'competitors': [{'name': PEER, 'reason': '合成测试的同用途公司', 'query': PEER}],
        'industry_queries': ['合成车载光学行业'], 'policy_queries': ['合成光学产业政策'],
        'paper_queries': [], 'affiliation_names': [], 'is_biomed': False,
        'trial_sponsors': [], 'patent_assignees': []}


def json_response(data, status=200):
    return Response(status, {'content-type': 'application/json'}, json.dumps(data, ensure_ascii=False).encode())


class SyntheticTransport:
    def __init__(self):
        self.calls = []
        self.search_failure = ''
        self.model_failure = ''
        self.news_failure = False

    def __call__(self, method, url, headers, body, timeout):
        host = urlsplit(url).hostname
        payload = json.loads(body) if body else {}
        self.calls.append({'host': host, 'method': method})  # Never log headers or bodies.
        if host == 'open.bigmodel.cn':
            if self.model_failure == 'invalid':
                return json_response({'error': {'message': MODEL_KEY}}, 401)
            if self.model_failure == 'refusal':
                return json_response({'error': {'code': '1301', 'message': MODEL_KEY}}, 400)
            if self.model_failure == 'offline':
                raise HttpError('connection_failed', detail=MODEL_KEY)
            system, user = [m['content'] for m in payload['messages']]
            result = self.model_result(system, user)
            content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return json_response({'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                                  'usage': {'prompt_tokens': 100, 'completion_tokens': 50}})
        if host == 'api.bochaai.com':
            if self.search_failure == 'invalid':
                return json_response({'message': SEARCH_KEY}, 401)
            if self.search_failure == 'quota':
                return json_response({'message': 'You do not have enough money or package quota'}, 403)
            if self.search_failure == 'offline':
                raise HttpError('connection_failed', detail=SEARCH_KEY)
            query = payload['query']
            title, link, text = self.hit(query)
            return json_response({'data': {'webPages': {'value': [{'name': title, 'url': link,
                'summary': text, 'siteName': '合成发布方', 'datePublished': '2026-01-05'}]}}})
        if host == 'www.bing.com':
            if self.news_failure:
                if self.news_failure == 'offline':
                    raise HttpError('connection_failed')
                return json_response({}, 503)
            query = parse_qs(urlsplit(url).query)['q'][0]
            title, link, text = self.hit(query)
            xml = ('<rss><channel><item><title>' + escape(title) + '</title><link>' + escape(link)
                   + '</link><description>' + escape(text) + '</description>'
                   + '<pubDate>Mon, 05 Jan 2026 08:00:00 GMT</pubDate></item></channel></rss>')
            return Response(200, {}, xml.encode())
        if host == 'www.cninfo.com.cn':
            return json_response({'announcements': []})
        if host == 'api.openalex.org':
            return json_response({'results': []})
        raise AssertionError(f'Unscripted endpoint (no network permitted): {host}')

    @staticmethod
    def hit(query):
        title = f'{query} 发布合成进展'
        digest = hashlib.sha256(query.encode()).hexdigest()[:12]
        text = f'{query} 发布合成进展。本文仅用于测试安装与监控，不代表任何真实公司或政策。' * 6
        return title, f'https://synthetic.example/{digest}', text

    @staticmethod
    def model_result(system, user):
        if '你是连接测试' in system:
            return 'OK'
        if '准确工商主体' in system:
            return {'candidates': [{'legal_name': COMPANY, 'short_name': '合成光学',
                                    'industry': '车载光学', 'description': '研发合成车载光学模组',
                                    'confidence': 90, 'reason': '合成来源',
                                    'evidence': ['https://synthetic.example/company']}], 'notes': ''}
        if '制定持续的公开信息监控方案' in system:
            return PLAN.copy()
        if '选择值得读取正文' in system:
            return {'results': [{'id': i['id'], 'read': True} for i in json.loads(user)['items']]}
        if '负责筛选与被监控公司' in system:
            return {'results': [{'id': key, 'relevant': True, 'category': category,
                'importance': 'notable', 'summary': '合成来源记录了本轮进展', 'impact': '合成分析：关注后续公开进展'}
                for key, category in re.findall(r'id=(\S+) \| 来源类别=(\w+)', user)]}
        if '最新动态简报' in system:
            return {'headline': '合成公司首轮进展', 'summary': '合成来源已汇总', 'changes': [], 'watch': []}
        if '根据公开搜索摘录补充' in system:
            return {'facts': []}
        if '具体产品/服务' in system:
            return {'products': []}
        if '允许主体的商业关系' in system:
            return {'relations': [], 'policies': [], 'events': [], 'commitments': [], 'milestones': [], 'funds': []}
        if '候选' in system and 'candidates' in system:
            return {'candidates': []}
        if '同一事件' in system:
            return {'results': []}
        # Optional analysis receives valid empty proposals, not invented facts.
        return {'results': [], 'candidates': [], 'products': [], 'facts': [], 'relations': [],
                'policies': [], 'events': [], 'commitments': [], 'milestones': [], 'funds': [],
                'funding': [], 'companies': [], 'metrics': [], 'implications': [], 'changes': []}


def make_app(data_dir):
    app = create_studio_app(data_dir, start_worker=False)
    transport = SyntheticTransport()
    pipeline = app.state.studio_pipeline
    pipeline.http_factory = lambda _settings: Http(transport)

    class Reader:
        def read(self, url):
            if urlsplit(url).hostname != 'synthetic.example':
                raise AssertionError('Unscripted article endpoint')
            return {'status': 'article_body', 'text': '这是一条合成公开进展。仅用于安装旅程验收。' * 12,
                    'resolved_url': url}

    pipeline.article_reader = Reader()
    return app, transport


def configure(client):
    saved = client.put('/api/studio/settings', json={
        'model': {'protocol': 'openai', 'base_url': 'https://open.bigmodel.cn/api/coding/paas/v4',
                  'model': 'synthetic-model', 'api_key': MODEL_KEY},
        'search': {'provider': 'bocha', 'api_key': SEARCH_KEY},
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()['model']['ready'] and saved.json()['search']['ready']
    return saved


def run_name_journey(client, app):
    resolved = client.post('/api/studio/resolve', json={'name': COMPANY})
    assert resolved.status_code == 200, resolved.text
    entity = resolved.json()['candidates'][0]
    response = client.post('/api/studio/projects', json={'entity': entity, 'consent': True})
    assert response.status_code == 201, response.text
    pid = response.json()['id']
    worker = app.state.studio_worker
    # Keep the real scheduler/worker. Avoid the periodic five-second window
    # reopening a new round while draining this single initial queue.
    worker.schedule()
    for _ in range(30):
        worker._last_schedule = datetime.now(UTC)
        if not worker.step():
            break
    else:
        raise AssertionError('Synthetic first queue did not become idle in 30 steps')
    detail = client.get(f'/api/studio/projects/{pid}')
    assert detail.status_code == 200, detail.text
    return pid, detail.json()
