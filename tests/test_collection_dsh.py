"""Host evidence is usable by real DSH tools, with no live model or transport."""
import json
import os
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_studio_dsh_monitor as fixtures
from test_collection_ingest import trial
from test_collection_plan import candidate, plan, success

from project_watch.studio import collection_dsh
from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.dsh_bootstrap import PublicBootstrap


@pytest.fixture
def loop(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path)


def imported(loop, items=None):
    p = plan([candidate() | {'source': 'clinical_term', 'category': 'trial'}],
             round_id=loop.execution.job['id'], project_id=loop.project_id)
    ledger = CollectionLedger(loop.execution, p)
    items = [trial()] if items is None else items
    retained = CollectionIngest(loop.execution)(p['units'][0], items)
    ledger.record(success(p, result_count=len(items), **retained))
    return ledger


def test_saved_structured_source_is_read_and_cited_without_network(loop):
    imported(loop)
    ctx = loop.tool('pw_context', {})
    entry = ctx['collected_sources']['sources'][0]
    assert ctx['collection']['status'] == 'complete'
    assert entry['matches'][0]['direction'] == 'trial'
    source = loop.tool('pw_read', {'source_id': entry['id']})
    assert source['source_type'] == 'structured_record' and source['read_scope'] == 'partial'
    assert 'RECRUITING' in source['excerpt'] and source['next_offset'] is None
    result = loop.tool('pw_record', {'kind': 'business', 'source_id': entry['id'],
                                    'quote': 'Synthetic source metadata',
                                    'label': 'Synthetic metadata', 'text': 'Synthetic source metadata'})
    assert result['retained']
    [ref] = loop.state['records'][0]['refs']
    evidence = loop.pipeline.evidence.get(loop.project_id, ref['evidence_version_id'])
    assert ref['source_type'] == 'structured_record'
    assert evidence['parts'][ref['part_index']]['text'][ref['start']:ref['end']] == ref['quote']
    assert not loop.execution.usage()


def test_source_directory_paginates_without_silent_180_source_cap(loop):
    items = [trial(url=f'https://example.org/study/{n}') | {
        'extra': {'source_identity': f'synthetic:{n}', 'source_record': {'title': f'Synthetic {n}'}}}
        for n in range(181)]
    imported(loop, items)
    ids, offset = [], 0
    while True:
        page = loop.tool('pw_status', {'collection_offset': offset})['collected_sources']
        ids.extend(row['id'] for row in page['sources'])
        offset = page['next_offset']
        if offset is None:
            break
    assert len(set(ids)) == page['total'] == 181
    assert loop.state['sources'] == []  # independent of the old ad hoc search cap
    with pytest.raises(ValueError):
        loop.tool('pw_status', {'collection_offset': -1})


def test_later_evidence_part_quote_offsets_are_exact(loop):
    item = trial()
    item['extra']['source_record'] = {'padding': 'x' * 24000, 'result': 'Synthetic later evidence'}
    imported(loop, [item])
    [entry] = collection_dsh.catalog(loop)['sources']
    first = loop.tool('pw_read', {'source_id': entry['id'], 'offset': 20000})
    assert 'Synthetic later evidence' in first['excerpt']
    loop.tool('pw_record', {'kind': 'business', 'source_id': entry['id'],
                           'quote': 'Synthetic later evidence', 'text': 'Synthetic later evidence',
                           'label': 'Synthetic result'})
    [ref] = loop.state['records'][0]['refs']
    assert ref['part_index'] == 1
    record = loop.pipeline.evidence.get(loop.project_id, ref['evidence_version_id'])
    assert record['parts'][1]['text'][ref['start']:ref['end']] == ref['quote']


def test_pending_collection_prevents_monitor_complete_even_after_judgment(loop):
    _watch, _ctx, ref = fixtures.collect(loop)
    fixtures.judge(loop, ref)
    loop.tool('pw_revise', {'changes_json': '[]', 'reason': 'Synthetic unchanged scope'})
    p = plan(round_id=loop.execution.job['id'], project_id=loop.project_id)
    CollectionLedger(loop.execution, p)
    result = loop.tool('pw_finish', {'reason': 'Synthetic complete claim'})
    assert result['status'] == result['collection']['status'] == 'partial'
    assert result['collection']['unresolved_ids'] == [p['units'][0]['id']]


def test_empty_host_result_can_close_without_fake_model_search(loop):
    imported(loop, [])
    research = PublicBootstrap(loop.pipeline, loop.execution, loop.collectors, None)
    result = research.tool('pw_finish', {'gaps': {}})
    assert result['collection']['status'] == 'complete'
    assert result['records'] == 0 and result['status'] == 'partial'
    assert not research.state['searches']


def test_collection_source_cannot_cross_project_or_current_round(loop):
    ledger = imported(loop)
    state = {'plan': ledger.plan | {'project_id': 'another-project'}, 'receipts': ledger.receipts}
    loop.execution.checkpoint_chunked('collection_round_v1', state)
    with pytest.raises(ValueError, match='outside research round'):
        loop.tool('pw_status', {})


def test_publishing_imported_news_preserves_item_and_does_not_save_json_as_body(loop):
    imported(loop)
    [entry] = collection_dsh.catalog(loop)['sources']
    loop.tool('pw_read', {'source_id': entry['id']})
    loop.tool('pw_record', {'kind': 'company', 'source_id': entry['id'],
                           'quote': 'Synthetic source metadata', 'text': 'Synthetic source metadata',
                           'label': 'Synthetic metadata'})
    loop.tool('pw_context', {})
    with closing(loop.store._connect()) as db:
        rows = db.execute('SELECT id,category,body_status FROM studio_items WHERE project_id=?',
                          (loop.project_id,)).fetchall()
    assert len(rows) == 1 and rows[0]['id'] == entry['matches'][0]['item_id']
    assert rows[0]['category'] == 'trial'
    assert rows[0]['body_status'] not in ('article_body', 'partial_body')


def test_professional_record_page_read_preserves_category_and_original_fields(loop):
    imported(loop)
    [entry] = collection_dsh.catalog(loop)['sources']
    loop.collectors.deepseek_fetch = lambda _: None
    loop.reader = SimpleNamespace(read=lambda _: {
        'status': 'partial_body', 'text': 'Synthetic registry page evidence'})
    page = loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'})
    assert page['source_type'] == 'public_page' and page['read_scope'] == 'partial'
    loop.tool('pw_record', {'kind': 'company', 'source_id': page['id'],
                           'quote': page['excerpt'], 'text': page['excerpt'], 'label': 'Synthetic registry'})
    loop.tool('pw_context', {})
    with closing(loop.store._connect()) as db:
        rows = db.execute('SELECT id,category,body_status FROM studio_items WHERE project_id=?',
                          (loop.project_id,)).fetchall()
    assert len(rows) == 1 and rows[0]['id'] == entry['matches'][0]['item_id']
    assert rows[0]['category'] == 'trial' and rows[0]['body_status'] == 'partial_body'
    original = loop.tool('pw_read', {'source_id': entry['id']})
    assert original['source_type'] == 'structured_record' and 'RECRUITING' in original['excerpt']
    assert original['evidence_version_id'] == entry['evidence_version_id']


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_official_dsh_reads_host_evidence_and_saves_version_bound_fact(loop, tmp_path):
    from project_watch.studio.dsh_runtime import Gateway, run_dsh
    from project_watch.studio.http import Http, Response
    from project_watch.studio.settings import ModelConfig

    imported(loop)
    research = PublicBootstrap(loop.pipeline, loop.execution, loop.collectors, None)
    [entry] = collection_dsh.catalog(research)['sources']
    operations = [('pw_status', {'collection_offset': 0}),
                  ('pw_read', {'source_id': entry['id']}),
                  ('pw_record', {'kind': 'business', 'label': 'Synthetic metadata',
                                 'source_id': entry['id'], 'quote': 'Synthetic source metadata',
                                 'text': 'Synthetic source metadata'}),
                  ('pw_finish', {'gaps': {}})]
    calls = []

    def scripted(method, url, headers, raw, timeout):
        payload = json.loads(raw)
        if 'tools' not in payload:
            review = json.loads(payload['messages'][-1]['content'])
            assert review['purpose'] == 'public_record_admission'
            return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(fixtures.synthetic_review(review))},
                'finish_reason': 'stop'}]}).encode())
        status_schema = next(t['function']['parameters'] for t in payload['tools']
                             if t['function']['name'] == 'pw_status')
        assert 'collection_offset' in status_schema['properties']
        index = len(calls)
        assert index < len(operations)
        if index == 1:
            directory = json.loads(payload['messages'][-1]['content'])['collected_sources']
            assert directory['sources'][0]['id'] == entry['id']
        if index == 2:
            evidence = json.loads(payload['messages'][-1]['content'])
            assert evidence['source_type'] == 'structured_record'
            assert evidence['evidence_version_id'] == entry['evidence_version_id']
        calls.append(payload)
        name, args = operations[index]
        return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': f'synthetic-{index}', 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(args)}}]},
            'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())

    model = ModelConfig(api_key='synthetic', model='synthetic', base_url='https://example.invalid/v1')
    gateway = Gateway(research, model, loop.execution.http(Http(scripted), lambda _: ('model', 'synthetic')))
    result = run_dsh(research, gateway, Path(os.environ['PW_DSH_TEST_RUNTIME']), tmp_path / 'sdk', timeout=120)
    assert len(calls) == 4 and result['collection']['status'] == 'complete'
    assert result['status'] == 'partial'  # Collection is not a complete company assessment.
    [fact] = loop.store.get_public_profile(loop.project_id)['facts']
    assert fact['refs'][0]['evidence_version_id'] == entry['evidence_version_id']
    assert set(loop.execution.usage()) == {'model'}
