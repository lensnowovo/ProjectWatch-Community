"""Synthetic context, no providers; exact storage and final transport bounds."""
import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_collection_host as fixtures

from project_watch.studio import bootstrap_collection as collection
from project_watch.studio import collection_context, dsh_runtime
from project_watch.studio.collection_context import Index, fit, wire
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response


class Memory:
    def __init__(self):
        self.saved = {}

    def read_checkpoint(self, stage):
        return deepcopy(self.saved.get(stage))

    def checkpoint_chunked(self, stage, value):
        assert len(stage) <= 60
        self.saved[stage] = deepcopy(value)


def body():
    sources = [{'id': f'C{i}', 'evidence_version_id': f'version-{i}',
                'read_scope': 'structured_record', 'title': f'Synthetic record {i}',
                'excerpt': ('Exact "中🧬\\\n' * 1400) + f'final statement {i}',
                'collection_matches': [{'item_id': f'item-{i}', 'category': 'trial'}]}
               for i in range(47)]
    return {'model': 'synthetic', 'messages': [
        {'role': 'system', 'content': 'Collect evidence; do not assert ownership.'},
        {'role': 'user', 'content': '确认主体：Synthetic。' + json.dumps({'sources': sources[:8]}, ensure_ascii=False)},
        {'role': 'assistant', 'content': 'thinking ' * 800, 'tool_calls': [
            {'id': 'one', 'type': 'function', 'function': {'name': 'pw_collect', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'one', 'content': json.dumps({'sources': sources,
         'collection': {'outcome': 'success', 'result_count': 47}, 'untrusted_data': True}, ensure_ascii=False)}],
        'tools': [{'function': {'name': n}} for n in collection.TOOLS], 'stream': False, 'max_tokens': 2000}


def test_whole_body_budget_and_lossless_refs_not_just_one_tool():
    execution, original = Memory(), body()
    before = deepcopy(original)
    packed = fit(original, execution)
    assert len(wire(original)) > 600000 and len(wire(packed)) <= 32768
    assert original == before
    assert packed['messages'][0] == original['messages'][0]
    assert packed['messages'][2] == original['messages'][2]
    assert packed['tools'] == original['tools']
    assert packed['messages'][1]['content'].startswith('确认主体：Synthetic。')
    indexes = next(v for k, v in execution.saved.items() if k.startswith('cc-fit:'))['indexes']
    assert {v['message'] for v in indexes} == {1, 3}
    reader = Index(execution)
    for item in indexes:
        assert reader.load(item['raw_message']['context_ref']) == original['messages'][item['message']]['content']
    assert fit(original, execution) == packed
    assert packed['messages'][-1]['tool_call_id'] == 'one'


def test_page_all_47_versions_and_exact_late_quote_after_reopen(tmp_path):
    pipeline, execution = fixtures.setup.__wrapped__(tmp_path)
    reader = Index(execution)
    sources = json.loads(body()['messages'][-1]['content'])['sources']
    ref = reader.save(sources)['context_ref']
    reopened = Index(JobExecution(pipeline.store, execution.job))
    offset, rows = 0, []
    while offset is not None:
        page = reopened.read(ref, offset)
        assert len(wire(page)) <= 4096
        rows.extend(page['value'])
        offset = page['next_offset']
    assert [(r['id'], r['evidence_version_id']) for r in rows] == [(s['id'], s['evidence_version_id']) for s in sources]
    assert reopened.load(rows[-1]['context_ref']) == sources[-1]
    fields = reopened.read(rows[-1]['context_ref'])['value']
    excerpt_ref = fields['excerpt']['context_ref']
    offset, parts = 0, []
    while offset is not None:
        page = reopened.read(excerpt_ref, offset)
        assert len(wire(page)) <= 4096 and page['offset_unit'] == 'characters'
        parts.append(page['value'])
        offset = page['next_offset']
    assert ''.join(parts) == sources[-1]['excerpt']
    assert ''.join(parts).endswith('final statement 46')
    # The archive contains no synthetic network journal entries.
    assert execution.budget_used('model') == execution.budget_used('open_data') == 0


def test_corrupt_missing_cross_job_and_invalid_pages_fail_closed(tmp_path):
    pipeline, execution = fixtures.setup.__wrapped__(tmp_path)
    index = Index(execution)
    ref = index.save('abcdef')['context_ref']
    other = Index(JobExecution(pipeline.store, {'id': 'another-job'}))
    with pytest.raises(ValueError, match='missing_or_changed'):
        other.read(ref)
    for offset in (-1, True, 7):
        with pytest.raises(ValueError, match='offset'):
            index.read(ref, offset)
    with pytest.raises(ValueError, match='invalid_collection_context_ref'):
        index.read('context:../settings')
    execution.checkpoint_chunked('cc:' + ref.split(':')[1][:48], {'value': 'changed'})
    with pytest.raises(ValueError, match='missing_or_changed'):
        index.read(ref)


def test_irreducible_body_returns_explicit_stop_without_clipping():
    original = body()
    original['messages'][0]['content'] = 'x' * 40000
    execution = Memory()
    assert fit(original, execution) is None
    receipt = next(v for k, v in execution.saved.items() if k.startswith('cc-fit:'))
    assert not receipt['fits'] and receipt['wire_bytes'] > 32768
    assert original['messages'][0]['content'] == 'x' * 40000


def test_escaped_text_windows_respect_wire_bytes_and_cover_every_character():
    index = Index(Memory())
    text = '\u0000' * 1800 + '中🧬"\\\n' * 200
    ref = index.save(text)['context_ref']
    offset, parts = 0, []
    while offset is not None:
        page = index.read(ref, offset)
        assert len(wire(page)) <= 4096 and page['value']
        parts.append(page['value'])
        offset = page['next_offset']
    assert ''.join(parts) == text


def gateway(tmp_path, monkeypatch):
    pipeline, execution = fixtures.setup.__wrapped__(tmp_path)
    role = SimpleNamespace(lock=threading.RLock(), store=pipeline.store, execution=execution,
        guard=lambda: None, guard_input=lambda: None, state={'finished': None},
        attempt_stage=collection.Collection.attempt_stage, reserve=lambda: 0,
        limits=dict(turns=collection.MAX_TURNS, actions=collection.MAX_ACTIONS, pages=collection.MAX_PAGES))
    role.defer = lambda op, reason: execution.checkpoint('test_deferred', {'reason': reason})
    role.finish = lambda reason: role.state.update(finished={'reason': reason})
    g = collection.CollectionGateway(role, pipeline.settings_store.load().model, None)
    monkeypatch.setattr(g, 'received', lambda b: False)
    sent = []
    monkeypatch.setattr(g, '_send_model', lambda b: sent.append(deepcopy(b)) or {'cached_or_sent': True})
    payload = body()
    payload['model'] = g.model.model
    return g, payload, sent, execution


def test_gateway_completed_legacy_and_compacted_cache_keep_wire_identity(tmp_path, monkeypatch):
    g, payload, sent, execution = gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(g, 'received', lambda b: True)
    g.model_call(payload)
    assert sent == [payload]  # Historical completed request goes to exact journal replay.
    assert not execution.read_checkpoint(g.research.attempt_stage)
    sent.clear()
    monkeypatch.setattr(g, 'received', lambda b: len(wire(b)) <= 32768)
    g.model_call(payload)
    assert len(wire(sent[0])) <= 32768
    assert not execution.read_checkpoint(g.research.attempt_stage)


def test_gateway_preserves_attempts_and_unknown_and_withdrawal_guards(tmp_path, monkeypatch):
    g, payload, sent, execution = gateway(tmp_path, monkeypatch)
    execution.checkpoint(g.research.attempt_stage, ['old-completed', 'old-preflight-failed'])
    g.model_call(payload)
    attempts = execution.read_checkpoint(g.research.attempt_stage)
    assert attempts[:2] == ['old-completed', 'old-preflight-failed'] and len(attempts) == 3
    assert len(wire(sent[0])) <= 32768
    for field, code in [('guard', 'request_outcome_unknown'), ('guard_input', 'source_withdrawn')]:
        def fail(code=code):
            raise HttpError(code)
        with monkeypatch.context() as patch:
            patch.setattr(g.research, field, fail)
            with pytest.raises(HttpError, match=code):
                g.model_call(payload)
        assert len(sent) == 1
    assert execution.read_checkpoint(g.research.attempt_stage) == attempts


def test_context_read_cannot_dispatch_or_bypass_source_guard(tmp_path):
    pipeline, execution = fixtures.setup.__wrapped__(tmp_path)
    role = collection.Collection.__new__(collection.Collection)
    role.execution, role.store, role.lock = execution, pipeline.store, threading.RLock()
    role.state = {'finished': None}
    role.guard = lambda: None
    role.guard_input = lambda: None
    ref = Index(execution).save(['exact source text'])['context_ref']
    assert role.tool('pw_read', {'source_id': ref})['value'] == ['exact source text']
    with pytest.raises(ValueError, match='saved_only'):
        role.tool('pw_read', {'source_id': ref, 'view': 'page'})
    def withdrawn():
        raise HttpError('source_withdrawn')
    role.guard_input = withdrawn
    with pytest.raises(HttpError, match='source_withdrawn'):
        role.tool('pw_read', {'source_id': ref})


def test_gateway_irreducible_limit_keeps_material_handoff_possible(tmp_path, monkeypatch):
    g, payload, sent, execution = gateway(tmp_path, monkeypatch)
    payload['messages'][0]['content'] = 'x' * 40000
    reply = g.model_call(payload)
    assert not sent and reply['choices'][0]['finish_reason'] == 'stop'
    assert g.research.state['finished']['reason'] == 'bounded_context_stop'
    assert execution.read_checkpoint('test_deferred')['reason'] == 'collection_context_limit'
    assert not execution.read_checkpoint(g.research.attempt_stage)


def test_native_same_job_resumes_history_and_reads_saved_index(tmp_path, monkeypatch):
    runtime = os.environ.get('PW_DSH_TEST_RUNTIME')
    if not runtime:
        pytest.skip('requires explicitly verified native DSH runtime')
    pipeline, execution = fixtures.setup.__wrapped__(tmp_path)
    role = SimpleNamespace(lock=threading.RLock(), store=pipeline.store, execution=execution,
        pipeline=pipeline, project={'name': 'Synthetic Host', 'legal_name': 'Synthetic Host'},
        project_id=execution.job['project_id'], session_id='collect-' + execution.job['id'],
        task=json.dumps({'sources': []}), prompt=collection.PROMPT, tools=collection.TOOLS,
        guard=lambda: None, guard_input=lambda: None, hold=lambda code: None,
        state={'finished': None}, attempt_stage=collection.Collection.attempt_stage, reserve=lambda: 0,
        limits=dict(turns=collection.MAX_TURNS, actions=collection.MAX_ACTIONS, pages=collection.MAX_PAGES))
    results = json.loads(body()['messages'][-1]['content'])
    seen, snapshots = [], []
    def tool(name, args):
        if name == 'pw_status':
            prior = execution.read_checkpoint('test_collection_result')
            if prior is None:
                execution.checkpoint_chunked('test_collection_result', results)
            return prior or results
        if name == 'pw_read':
            page = Index(execution).read(args['source_id'], args.get('offset', 0))
            snapshots.append(page)
            return page
        assert name == 'pw_finish'
        role.state['finished'] = {'status': 'collected'}
        return role.state['finished']
    role.tool = tool
    role.defer = lambda op, reason: pytest.fail(reason)
    role.finish = lambda reason: pytest.fail(reason)
    def transport(method, url, headers, raw, timeout):
        sent = json.loads(raw)
        seen.append(sent)
        if len(seen) == 1:
            name, args = 'pw_status', {}
        elif len(seen) == 2:
            page = next(json.loads(m['content'])['context_index'] for m in sent['messages']
                        if m['role'] == 'tool' and 'context_index' in m['content'])
            name, args = 'pw_read', {'source_id': page['value']['sources']['context_ref']}
        else:
            assert len(seen) == 3 and snapshots
            name, args = 'pw_finish', {'gaps': {}}
        msg = {'role': 'assistant', 'tool_calls': [{'id': 'call-' + str(len(seen)), 'type': 'function',
               'function': {'name': name, 'arguments': json.dumps(args)}}]}
        return Response(200, {}, wire({'choices': [{'message': msg, 'finish_reason': 'tool_calls'}]}))
    journal = execution.http(Http(transport), lambda url: ('model', 'synthetic'))
    def gate(method, url, headers, raw, timeout):
        if len(raw) > 32768:
            raise HttpError('request_budget')  # As in the original pre-journal gate.
        return journal.transport(method, url, headers, raw, timeout)
    model = pipeline.settings_store.load().model
    directory = tmp_path / 'native'
    with monkeypatch.context() as old:
        old.setattr(collection_context, 'fit', lambda body, execution: body)
        g = collection.CollectionGateway(role, model, Http(gate))
        with pytest.raises(HttpError):
            dsh_runtime.run_dsh(role, g, Path(runtime), directory, timeout=60)
    assert len(seen) == 1 and execution.budget_used('model') == 1
    prior_attempts = execution.read_checkpoint(role.attempt_stage)
    assert len(prior_attempts) == 2
    g = collection.CollectionGateway(role, model, Http(gate))
    assert dsh_runtime.run_dsh(role, g, Path(runtime), directory, timeout=60, resume=True)['status'] == 'collected'
    assert len(seen) == 3 and execution.budget_used('model') == 3
    assert all(len(wire(request)) <= 32768 for request in seen)
    assert execution.read_checkpoint(role.attempt_stage)[:2] == prior_attempts
    assert execution.read_checkpoint('test_collection_result') == results
    assert {s['evidence_version_id'] for s in snapshots[0]['value']} == {f'version-{i}' for i in range(len(snapshots[0]['value']))}
