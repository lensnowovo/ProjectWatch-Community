"""Frozen material traversal and durable batch progress; synthetic, no network."""
import json
import sqlite3
from types import SimpleNamespace

from project_watch.studio import analyze_job as a
from project_watch.studio import section_analysis
from project_watch.studio.llm import ModelError


def test_collection_plan_and_explicit_patent_matches_keep_the_tail():
    from project_watch.studio import collect_job
    plan = {'competitors': [], 'policy_queries': []}
    stored = {'competitors': [{'name': f'Peer{i}'} for i in range(12)],
              'policy_queries': [f'Confirmed{i}' for i in range(12)]}
    collect_job._merge_stored_plan(plan, stored)
    assert len(plan['competitors']) == len(plan['policy_queries']) == 12
    execution = SimpleNamespace(read_checkpoint=lambda _: {'names': ['Synthetic Biologics Ltd']})
    records = [{'extra': {'provider_affiliation': 'Synthetic Biologics Ltd',
                         'patent_number': f'US20260000{i:02}', 'patent_kind': 'A1'}} for i in range(12)]
    assert len(collect_job._patent_followups(execution, records)) == 12


def selection(tmp_path):
    path = tmp_path / 'selection.db'
    def connect():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        return db
    with connect() as db:
        db.executescript('CREATE TABLE studio_item_evidence(evidence_version_id TEXT,item_id TEXT); '
                        'CREATE TABLE studio_items(id TEXT,project_id TEXT,relevant INT,triaged INT,importance TEXT,published_at TEXT,collected_at TEXT,category TEXT);')
        for i in range(43):
            db.execute('INSERT INTO studio_item_evidence VALUES (?,?)', (str(i), str(i)))
            db.execute('INSERT INTO studio_items VALUES (?,?,?,?,?,?,?,?)',
                       (str(i), 'p', 1, int(i != 42), 'major' if i < 40 else 'routine', '2026-11-01', '',
                        'company' if i < 40 else 'policy'))
    materials = [dict(evidence_version_id=str(i), source_key=str(i), read_scope='full',
                      evidence=dict(parts=[dict(text='A'*4100+'TAIL'), dict(text='SECOND')])) for i in range(43)]
    context = dict(id='snapshot', materials=materials)
    return a.select(SimpleNamespace(_connect=connect), 'p', context), context


def test_tail_directions_and_later_parts_are_not_lost(tmp_path):
    selected, original = selection(tmp_path)
    assert len(original['materials']) == 43
    assert selected['coverage']['selected_materials'] == 42
    assert selected['coverage']['directions'] == {'company': 40, 'policy': 2}
    assert selected['coverage']['excluded'] == [dict(evidence_version_id='42', reason='unreviewed')]
    for i in range(42):
        parts = [m for m in selected['materials'] if m['evidence_version_id'] == str(i)]
        assert ''.join(m['evidence']['parts'][0]['text'] for m in parts) == 'A'*4100+'TAILSECOND'
    # Policy is encountered in the first batch, not starved behind 40 company stories.
    batches = list(a.source_batches(section_analysis.numbered(selected)))
    assert any(s['evidence_version_id'] == '40' for s in batches[0].values())
    assert sum(map(len, batches)) == 126
    assert all(sum(len(s['text']) for s in b.values()) <= a.BATCH_CHARS for b in batches)


def test_empty_material_is_explained_not_counted_as_processed(tmp_path):
    _, original = selection(tmp_path)
    original['materials'] = [{**original['materials'][0], 'evidence': {'parts': [{'text': '  '}]}}]
    def connect():
        db = sqlite3.connect(tmp_path / 'selection.db')
        db.row_factory = sqlite3.Row
        return db
    selected = a.select(SimpleNamespace(_connect=connect), 'p', original)
    assert selected['materials'] == []
    assert selected['coverage']['selected_materials'] == 0
    assert selected['coverage']['excluded'] == [{'evidence_version_id': '0', 'reason': 'empty_text'}]


class Execution:
    def __init__(self):
        self.saved = {}
    def read_checkpoint(self, key):
        return self.saved.get(key)
    def checkpoint_chunked(self, key, value):
        self.saved[key] = json.loads(json.dumps(value))


def test_batch_resume_reuses_saved_results_and_keeps_tail(tmp_path):
    selected, _ = selection(tmp_path)
    sources = section_analysis.numbered(selected)
    execution, calls = Execution(), []
    def json_call(system, user, **kwargs):
        payload = json.loads(user)
        calls.append(list(payload['materials']))
        return {'operations': {'summary': 'synthetic', 'for': list(payload['materials'])}}
    llm = SimpleNamespace(json=json_call)
    first = a.draft_batches(execution, llm, 'system', {}, sources, selected, ['operations'])
    count = len(calls)
    assert count > 1
    assert a.draft_batches(execution, llm, 'system', {}, sources, selected, ['operations']) == first
    assert len(calls) == count
    assert [c for batch in calls for c in batch] == list(sources)
    assert execution.saved[a.COVERAGE]['remaining_fragments'] == 0


def test_unknown_stops_before_next_batch_and_keeps_completed_checkpoint(tmp_path):
    selected, _ = selection(tmp_path)
    sources, execution, calls = section_analysis.numbered(selected), Execution(), []
    def json_call(system, user, **kwargs):
        calls.append(user)
        if len(calls) == 2:
            raise ModelError('synthetic unknown', code='request_outcome_unknown')
        return {'operations': {'summary': 'saved'}}
    result = a.draft_batches(execution, SimpleNamespace(json=json_call), 'system', {}, sources, selected, ['operations'])
    assert result == {'operations': {'summary': 'saved'}}
    assert a.draft_batches(execution, SimpleNamespace(json=json_call), 'system', {}, sources, selected, ['operations']) == result
    assert len(calls) == 2
    assert len([k for k in execution.saved if k.startswith(a.DRAFT)]) == 1
    assert execution.saved[a.COVERAGE]['remaining_fragments'] > 0


def test_draft_batches_follow_the_frozen_batch_size():
    sources = {f'S{i}': {'text': 'x' * 3000} for i in range(60)}
    legacy = list(a.source_batches(sources))
    wide = list(a.source_batches(sources, a.WIDE_BATCH))
    assert [len(b) for b in legacy] == [10] * 6  # 32,000 characters per request
    assert [len(b) for b in wide] == [21, 21, 18]  # 64,000 characters per request
    assert sum(map(len, wide)) == 60
    seen = []

    class Execution:
        def read_checkpoint(self, key):
            return a.WIDE_BATCH if key == a.DRAFT_BATCH else None

        def checkpoint_chunked(self, key, value):
            pass

    def drafted(*args, **kwargs):
        seen.append(len(json.loads(args[1])['materials']))
        return {'draft': len(seen)}
    a.draft_batches(Execution(), SimpleNamespace(json=drafted), 'system', {}, sources,
                    {'id': 'snapshot', 'coverage': {}}, ['industry'])
    assert seen == [21, 21, 18]


def test_a_job_drafted_before_the_upgrade_replays_its_saved_drafts_without_new_requests():
    sources = {f'S{i}': {'text': 'x' * 3000} for i in range(60)}
    saved, sent = {}, []

    class Execution:
        def read_checkpoint(self, key):
            return saved.get(key)

        def checkpoint(self, key, value):
            saved[key] = value

        checkpoint_chunked = checkpoint

    def drafted(*args, **kwargs):
        sent.append(1)
        return {'draft': len(sent)}
    llm = SimpleNamespace(json=drafted)
    context = {'id': 'snapshot', 'coverage': {}}
    first = a.draft_batches(Execution(), llm, 'system', {}, sources, context, ['industry'])
    assert len(sent) == 6  # drafted with the original size, before the batch size was frozen
    a.freeze_draft_batch(Execution(), None, fresh=False)  # the upgraded job resumes
    assert saved[a.DRAFT_BATCH] == a.LEGACY_BATCH
    assert a.draft_batches(Execution(), llm, 'system', {}, sources, context, ['industry']) == first
    assert len(sent) == 6
