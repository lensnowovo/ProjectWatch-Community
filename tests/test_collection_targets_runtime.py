"""Official DSH protocol, synthetic professional API and synthetic model only."""
import json
import os
from pathlib import Path

import pytest
import test_collection_targets as fixtures

from project_watch.studio.dsh_runtime import Gateway, run_dsh
from project_watch.studio.http import Http, Response


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_official_dsh_queries_reads_and_cites_specialist_record(tmp_path):
    research, collect_args, requests, _ = fixtures.research.__wrapped__(tmp_path)
    model_calls, source_ids = [], []

    def model(_method, _url, _headers, raw, _timeout):
        payload = json.loads(raw)
        if 'tools' not in payload:
            from test_studio_dsh_monitor import synthetic_review
            review = json.loads(payload['messages'][-1]['content'])
            assert review['purpose'] == 'public_record_admission'
            return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(synthetic_review(review))},
                'finish_reason': 'stop'}]}).encode())
        schema = next(t['function']['parameters'] for t in payload['tools']
                      if t['function']['name'] == 'pw_collect')
        assert 'clinical_term' in schema['properties']['adapter']['enum']
        assert 'not_applicable' not in schema['properties']['adapter']['enum']
        index = len(model_calls)
        assert index < 4
        if index == 0:
            name, args = 'pw_collect', collect_args
        elif index == 1:
            result = json.loads(payload['messages'][-1]['content'])
            assert result['collection']['outcome'] == 'success'
            source_ids.append(result['sources'][0]['id'])
            name, args = 'pw_read', {'source_id': source_ids[0]}
        elif index == 2:
            result = json.loads(payload['messages'][-1]['content'])
            assert result['source_type'] == 'structured_record' and result['read_scope'] == 'partial'
            name, args = 'pw_record', dict(kind='business', source_id=source_ids[0],
                quote='Synthetic Hospital', text='Synthetic Hospital', label='Synthetic registration source')
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
    assert model_calls == ['pw_collect', 'pw_read', 'pw_record', 'pw_finish']
    assert len(requests) == 1 and result['collection']['status'] == 'complete'
    [fact] = research.store.get_public_profile(research.project_id)['facts']
    assert fact['refs'][0]['source_type'] == 'structured_record'
    assert fact['refs'][0]['evidence_version_id'] == research.source(source_ids[0])['evidence_version_id']
    assert research.execution.usage()['open_data']['requests'] == 1
