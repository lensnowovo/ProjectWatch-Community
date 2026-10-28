"""Pinned official DSH consumes a model-requested host supplement end to end."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_collection_supplements as fixtures

from project_watch.studio.dsh_runtime import Gateway, run_dsh
from project_watch.studio.http import Http, Response


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
@pytest.mark.parametrize('view', ['record', 'page'])
def test_official_dsh_supplement_search_read_record_finish(tmp_path, view):
    research, searches = fixtures.research.__wrapped__(tmp_path)
    model_calls = []
    source_ids = []
    pages = []

    def read(url):
        pages.append(url)
        return {'status': 'article_body', 'text': 'Synthetic original article evidence'}

    research.reader = SimpleNamespace(read=read)
    research.collectors.deepseek_fetch = lambda _: None

    def model(_method, _url, _headers, raw, _timeout):
        payload = json.loads(raw)
        if 'tools' not in payload:
            from test_studio_dsh_monitor import synthetic_review
            review = json.loads(payload['messages'][-1]['content'])
            assert review['purpose'] == 'public_record_admission'
            return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(synthetic_review(review))},
                'finish_reason': 'stop'}]}).encode())
        read_schema = next(t['function']['parameters'] for t in payload['tools']
                           if t['function']['name'] == 'pw_read')
        assert read_schema['properties']['view']['enum'] == ['record', 'page']
        index = len(model_calls)
        assert index < 4
        if index == 0:
            name, args = 'pw_search', {'query': 'extra', 'category': 'company'}
        elif index == 1:
            result = json.loads(payload['messages'][-1]['content'])
            assert result['collection']['outcome'] == 'success'
            source_ids.append(result['sources'][0]['id'])
            name, args = 'pw_read', {'source_id': source_ids[0], 'view': view}
        elif index == 2:
            result = json.loads(payload['messages'][-1]['content'])
            assert result['read_scope'] == ('full' if view == 'page' else 'summary')
            assert result['collection_matches']
            source_ids.append(result['id'])
            quote = 'Synthetic original article evidence' if view == 'page' else 'Public source excerpt'
            name, args = 'pw_record', {'kind': 'business', 'source_id': source_ids[-1],
                'quote': quote, 'text': quote, 'label': 'Synthetic source'}
        else:
            name, args = 'pw_finish', {'gaps': {}}
        model_calls.append(name)
        return Response(200, {}, json.dumps({'choices': [{'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [{'id': f'synthetic-{index}', 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args)}}]}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())

    config = research.pipeline.settings_store.load().model
    gateway = Gateway(research, config, research.execution.http(Http(model), lambda _: ('model', 'synthetic')))
    result = run_dsh(research, gateway, Path(os.environ['PW_DSH_TEST_RUNTIME']), tmp_path / 'sdk', timeout=120)
    assert searches == ['base', 'extra']
    assert model_calls == ['pw_search', 'pw_read', 'pw_record', 'pw_finish']
    assert result['collection']['status'] == 'complete' and result['status'] == 'partial'
    [fact] = research.store.get_public_profile(research.project_id)['facts']
    saved = research.source(source_ids[-1])
    assert fact['refs'][0]['evidence_version_id'] == saved['evidence_version_id']
    assert research.execution.usage()['search']['requests'] == 2
    assert len(pages) == (1 if view == 'page' else 0)
    assert research.source(source_ids[0])['read_scope'] == 'summary'
