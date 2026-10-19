"""Full permitted material coverage through normal extraction and question paths."""
import json
import socket

import pytest
from test_studio_competition_research import research_pipeline
from test_studio_evidence_versions import sample

from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.llm import ModelError
from project_watch.studio.monitoring_contracts import EvidencePart, LocatedRef


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('offline synthetic control')
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


def document(p, pid, texts, key='coverage'):
    return p.evidence.save(sample(pid).model_copy(update={
        'source_key': 'public-profile:' + key, 'visibility': 'public',
        'parts': tuple(EvidencePart(locator_kind='pdf_page', locator=str(i + 1), text=t) for i, t in enumerate(texts))}))


def test_all_versions_parts_and_long_tail_enter_business_and_details(tmp_path):
    p, pid, _, _ = research_pipeline(tmp_path)
    ids = {document(p, pid, [f'Synthetic Optics version {i} page {j}.' for j in range(6)], str(i))['id']
           for i in range(12)}
    tail = 'Synthetic Optics supplies Final Lens for screening.'
    long = document(p, pid, ['background ' * 1500 + tail], 'long')
    ids.add(long['id'])
    for inputs in (p.business_profiles.inputs(pid, binding=p.model_binding()),
                   p.structured_facts.inputs(pid, p.model_binding())):
        assert {row['evidence_version_id'] for row in inputs['parts']} == ids
        assert len(inputs['parts']) == 73
        assert inputs['parts'][0]['text'].endswith(tail)


@pytest.mark.parametrize('kind', ['business', 'details'])
def test_normal_job_batches_all_inputs_and_publishes_tail_refs(tmp_path, kind):
    p, pid, _, _ = research_pipeline(tmp_path)
    texts = [f'Synthetic Optics supplies Lens {i} for screening.' for i in range(25)]
    texts.append('background ' * 1500 + 'Synthetic Optics supplies Lens 25 for screening.')
    saved = document(p, pid, texts)
    calls = []

    def transport(method, url, headers, body, timeout):
        payload = json.loads(json.loads(body)['messages'][1]['content'])
        calls.append(payload)
        rows = []
        for source in payload['sources']:
            if 'Synthetic Optics supplies Lens' not in source['text']:
                continue
            quote = source['text'][source['text'].index('Synthetic Optics supplies Lens'):]
            number = quote.split('Lens ')[1].split()[0]
            if kind == 'business':
                rows.append({'company': 'Synthetic Optics', 'name': 'Lens ' + number,
                             'source': source['source'], 'quote': quote, 'use_case': 'screening',
                             'basis': 'actual', 'polarity': 'asserted'})
            else:
                rows.append({'source': source['source'], 'quote': quote, 'name': 'Lens ' + number,
                             'channel': 'commercial', 'role': 'actor', 'state': 'delivered',
                             'state_text': 'supplies', 'basis': 'actual'})
        result = {'products' if kind == 'business' else 'events': rows}
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(result)}}]}).encode())

    p.http_factory = lambda _: Http(transport)
    getattr(p, 'schedule_' + kind)(pid)
    job = p.store.claim()
    p.run(job)
    assert len(calls) >= 3
    assert all(sum(len(s['text']) for s in c['sources']) <= 48000 for c in calls)
    result = p.business_profiles.get(pid) if kind == 'business' else p.structured_facts.get(pid)
    ids = result['product_ids'] if kind == 'business' else result['fact_ids']
    assert len(ids) == 26
    if kind == 'business':
        rows = [p.objects.get(pid, value) for value in ids]
        refs = [ref for row in rows for ref in row['refs']]
    else:
        refs = [ref for row in p.objects.facts(pid, p.objects.ensure_subject(pid)['id']) for ref in row['refs']]
    last = [r for r in refs if r['evidence_version_id'] == saved['id'] and r['part_index'] == 25]
    assert last and max(r['start'] for r in last) >= 15000
    for ref in refs:
        p.evidence.resolve(pid, LocatedRef(**ref))
    sent = len(calls)
    p.run(job)
    assert len(calls) == sent


@pytest.mark.parametrize('kind', ['business', 'details'])
def test_later_batch_unknown_preserves_old_profile_and_does_not_resend(tmp_path, kind):
    p, pid, _, _ = research_pipeline(tmp_path)
    document(p, pid, ['Synthetic Optics ordinary passage.' for _ in range(25)])
    old = p.business_profiles.get(pid) if kind == 'business' else p.structured_facts.get(pid)
    calls = []

    def transport(*args):
        calls.append(1)
        if len(calls) == 2:
            raise HttpError('connection_failed')
        value = {'products': []} if kind == 'business' else {'events': []}
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(value)}}]}).encode())

    p.http_factory = lambda _: Http(transport)
    getattr(p, 'schedule_' + kind)(pid)
    job = p.store.claim()
    with pytest.raises(ModelError) as failure:
        p.run(job)
    assert failure.value.code == 'request_outcome_unknown'
    current = p.business_profiles.get(pid) if kind == 'business' else p.structured_facts.get(pid)
    assert current == old
    with pytest.raises(ModelError) as repeat:
        p.run(job)
    assert repeat.value.code == 'request_outcome_unknown'
    assert len(calls) == 2


def test_every_relevant_material_window_reaches_question_assessment(tmp_path):
    from test_studio_question_materials import intake, setup
    papp, web, p, pid, watches = setup(tmp_path)
    text = '合成光电原计划交付10台。' + '背景说明。' * 600 + '合成光电于2026年10月完成交付20台。'
    old_history = p.questions.history(watches[0]['id'])
    intake(papp, web, p, pid, text)
    rows = p.questions.new_materials(pid, watches[0]['id'], limit=None)
    assert any('完成交付20台' in row['ref']['quote'] for row in rows)
    assert all(len(row['ref']['quote']) <= 12000 for row in rows)
    for row in rows:
        p.evidence.resolve(pid, LocatedRef(**row['ref']))
    refs, _, ids = p._fragments(pid)
    assert p.questions.route_materials(pid, refs, set(ids), binding=p.model_binding()) == 0
    assert p.questions.history(watches[0]['id']) == old_history
    assert p.questions.new_materials(pid, watches[1]['id'], limit=None) == []

    class Advisor:
        def assess(self, llm, project, questions):
            sent.extend(m['ref']['quote'] for q in questions for m in q['materials'])
            return []
    sent = []
    from types import SimpleNamespace
    p.questions.assess(Advisor(), SimpleNamespace(ready=True), pid, p.store.get_project(pid))
    assert any('完成交付20台' in quote for quote in sent)


def test_legacy_material_hit_migration_preserves_cited_history(tmp_path):
    from test_studio_question_materials import intake, setup

    from project_watch.studio.questions import Assessment, QuestionLedger
    papp, web, p, pid, watches = setup(tmp_path)
    text = '合成光电原计划交付10台。' + '背景说明。' * 600 + '合成光电完成交付20台。'
    intake(papp, web, p, pid, text)
    watch = watches[0]
    hit = p.questions.new_materials(pid, watch['id'], limit=None)[-1]
    # Reproduce the old table and its one short, already cited hit.
    with p.store._connect() as db, db:
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='studio_question_material_hits'").fetchone()[0]
        db.execute('ALTER TABLE studio_question_material_hits RENAME TO hit_backup')
        db.execute(schema.replace('part_index,ref)', 'part_index)'))
        db.execute('INSERT INTO studio_question_material_hits SELECT * FROM hit_backup WHERE id=?', (hit['id'],))
        ref = {**hit['ref'], 'start': 0, 'end': 300, 'quote': text[:300]}
        db.execute('UPDATE studio_question_material_hits SET ref=? WHERE id=?', (json.dumps(ref, ensure_ascii=False), hit['id']))
        db.execute('DROP TABLE hit_backup')
    p.questions.record(pid, watch['id'], 'human', Assessment(status='insufficient', note='保留早期记录', material_ids=(hit['id'],)))
    before = p.questions.history(watch['id'])
    ledger = QuestionLedger(p.store, p.monitor, binding_provider=p.model_binding)
    assert ledger.history(watch['id']) == before
    refs, _, ids = p._fragments(pid)
    assert ledger.route_materials(pid, refs, set(ids), binding=p.model_binding()) > 0
    rows = ledger.new_materials(pid, watch['id'], limit=None)
    # The human assessment already consumed this hit; it remains stored and
    # cited in history, but must not reappear in the pending-material queue.
    with p.store._connect() as db:
        retained = db.execute('SELECT ref FROM studio_question_material_hits WHERE id=?', (hit['id'],)).fetchone()
    assert json.loads(retained['ref']) == ref
    assert all(r['id'] != hit['id'] for r in rows)
    assert any('完成交付20台' in r['ref']['quote'] for r in rows)
    assert ledger.history(watch['id']) == before
    assert ledger.route_materials(pid, refs, set(ids), binding=p.model_binding()) == 0
