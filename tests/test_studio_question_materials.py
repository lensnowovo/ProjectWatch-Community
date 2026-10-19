"""M3B: fictional local intake, scoped material packs and judgement history."""

import runpy
import socket
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_studio_application_parity import headers

from project_watch.studio.llm import ModelError
from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart, LocatedRef
from project_watch.studio.questions import Assessment

SUPPORT = runpy.run_path(str(Path(__file__).with_name('distribution_journey_support.py')))
CORE = runpy.run_path(str(Path(__file__).with_name('core_without_insight_support.py')))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Question/material regressions must remain offline')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


def setup(tmp_path):
    app, transport = SUPPORT['make_app'](tmp_path)
    app.state.synthetic_transport = transport
    client = TestClient(app)
    SUPPORT['configure'](client)
    p = app.state.studio_pipeline
    # These controls isolate routing/ledger contracts; normal automatic advisor
    # dispatch is covered separately by test_studio_internal_question_trigger.
    p.question_advisor = None
    project = p.repository.create_project('合成光电')
    pid = project.id
    p.store.upsert_project(pid, {'legal_name': '合成光电有限公司', 'short_name': '合成光电'}, consent=True)
    transport.model_result = CORE['material_model'](transport.model_result)
    watches = [p.monitor.add_watchpoint(pid, '交付是否推进', '交付', ['合成光电 交付'], '交付情况', False, visibility='public'),
               p.monitor.add_watchpoint(pid, '临床是否推进', '临床', ['合成光电 临床'], '临床情况', False, visibility='public')]
    p.questions.record(pid, watches[0]['id'], 'human', Assessment(status='setback', note='人的原判断'))
    p.questions.record(pid, watches[0]['id'], 'agent', Assessment(status='insufficient', note='Agent 原判断'))
    return app, client, p, pid, watches


def intake(app, client, p, pid, text, *, run=True):
    route = '/api/studio/projects/' + pid
    reply = client.post(route + '/files', files={'file': ('Synthetic.txt', text.encode(), 'text/plain')})
    assert reply.status_code == 202, reply.text
    parsed = app.state.intake_worker.process_one()
    assert parsed.status == 'completed'
    p.snapshot_ready_documents()
    if run:
        p.run_internal(p.store.enqueue(pid, 'internal'))
    return parsed.source_id


def material(p, pid, watch_id):
    return next(w for w in p.questions.view(pid) if w['id'] == watch_id)['materials']


def test_incremental_material_routes_only_affected_question_and_preserves_history(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    original = p.questions.history(watches[0]['id'])
    intake(app, web, p, pid, '合成光电生产光学模组。')
    assert material(p, pid, watches[0]['id']) == []
    assert material(p, pid, watches[1]['id']) == []
    source = intake(app, web, p, pid, '合成光电交付10台光学模组。')
    [hit] = material(p, pid, watches[0]['id'])
    assert hit['source_id'] == source and hit['basis'] == 'literal_relevance'
    assert hit['status'] == 'pending' and hit['matched_terms'] == ['交付']
    assert '交付10台' in hit['ref']['quote']
    p.evidence.resolve(pid, LocatedRef(**hit['ref']))
    assert material(p, pid, watches[1]['id']) == []
    assert p.questions.history(watches[0]['id']) == original
    calls = list(app.state.synthetic_transport.calls)
    refs, _, ids = p._fragments(pid)
    assert p.questions.route_materials(pid, refs, set(ids), binding=p.model_binding()) == 0
    assert app.state.synthetic_transport.calls == calls
    assert material(p, pid, watches[0]['id'])[0]['id'] == hit['id']


def test_public_agent_cannot_see_or_cite_internal_material_even_for_public_question(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    [hit] = material(p, pid, watches[0]['id'])
    service = app.state.studio_application
    public = service.issue_grant(pid, 'write', 'synthetic-public')
    internal = service.issue_grant(pid, 'write', 'synthetic-internal', allow_internal=True)
    route = '/api/studio/agent/projects/' + pid
    public_view = web.get(route + '/questions', headers=headers(public))
    assert public_view.status_code == 200 and hit['id'] not in public_view.text and '交付10台' not in public_view.text
    own_view = web.get(route + '/questions', headers=headers(internal))
    assert own_view.status_code == 200 and hit['id'] in own_view.text
    body = {'status': 'progress', 'note': '材料有交付记录', 'material_ids': [hit['id']], 'idempotency_key': 'm1'}
    denied = web.post(route + '/questions/' + watches[0]['id'] + '/assessments', json=body, headers=headers(public))
    assert denied.status_code == 403, denied.text
    saved = web.post(route + '/questions/' + watches[0]['id'] + '/assessments', json=body, headers=headers(internal))
    assert saved.status_code == 201, saved.text
    assert saved.json()['visibility'] == 'internal' and saved.json()['material_ids'] == [hit['id']]
    assert web.post(route + '/questions/' + watches[0]['id'] + '/assessments', json=body, headers=headers(internal)).json() == saved.json()
    assert '材料有交付记录' not in web.get(route + '/questions', headers=headers(public)).text
    assert p.questions.view(pid)[0]['latest']['human']['note'] == '人的原判断'


@pytest.mark.parametrize('change', ['permission', 'binding', 'withdrawal', 'question', 'consent'])
def test_stale_or_unauthorized_material_is_hidden_and_cannot_write_judgement(tmp_path, change):
    app, web, p, pid, watches = setup(tmp_path)
    source = intake(app, web, p, pid, '合成光电交付10台光学模组。')
    [hit] = material(p, pid, watches[0]['id'])
    with closing(p.store._connect()) as db, db:
        if change == 'permission':
            db.execute('INSERT OR REPLACE INTO studio_source_permissions VALUES (?,?,?,?,?)', (source, pid, '', 'synthetic', '2026-10-12'))
        elif change == 'question':
            db.execute('UPDATE studio_watchpoints SET label=?,queries=? WHERE id=?', ('融资', '["合成光电 融资"]', watches[0]['id']))
        elif change == 'consent':
            db.execute('UPDATE studio_projects SET consent_at=NULL WHERE project_id=?', (pid,))
    if change == 'binding':
        p.settings_store.update({'model': {'model': 'synthetic-different-model'}})
    if change == 'withdrawal':
        p.evidence.dependencies.withdraw(pid, hit['ref']['evidence_version_id'], 'synthetic withdrawal', actor='human')
    assert material(p, pid, watches[0]['id']) == []
    with pytest.raises((ValueError, PermissionError)):
        p.questions.record(pid, watches[0]['id'], 'human', Assessment(status='progress', material_ids=(hit['id'],)))
    assert len(p.questions.history(watches[0]['id'])) == 2


def test_material_cannot_be_borrowed_from_another_question_or_project(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    [hit] = material(p, pid, watches[0]['id'])
    with pytest.raises(ValueError):
        p.questions.record(pid, watches[1]['id'], 'human', Assessment(status='progress', material_ids=(hit['id'],)))
    other = p.repository.create_project('另一个合成项目').id
    watch = p.monitor.add_watchpoint(other, '交付', '交付', ['交付'], '交付', False)
    with pytest.raises(ValueError):
        p.questions.record(other, watch['id'], 'human', Assessment(status='progress', material_ids=(hit['id'],)))
    refs, _, _ = p._fragments(pid)
    assert p.questions.route_materials(other, refs, {hit['source_id']}, binding=p.model_binding()) == 0


def test_company_mention_alone_and_ascii_prefix_do_not_route(tmp_path):
    app, web, p, pid, _ = setup(tmp_path)
    company_only = p.monitor.add_watchpoint(pid, '公司动态', '动态', ['合成光电'], '公司信息', False)
    code = p.monitor.add_watchpoint(pid, 'ALPHA 产品', 'ALPHA', ['合成光电 ALPHA'], '产品', False)
    intake(app, web, p, pid, '合成光电生产MiniALPHA模组。')
    assert material(p, pid, company_only['id']) == [] and material(p, pid, code['id']) == []


def test_optional_advisor_uses_only_affected_new_material_and_consumes_once(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    calls = []
    class Advisor:
        def assess(self, _llm, _project, questions):
            calls.append(questions)
            return [{'id': q['id'], 'status': 'progress', 'note': '资料记录交付10台',
                     'material_ids': [q['materials'][0]['id']]} for q in questions]
    class Model:
        ready = True
    assert p.questions.assess(Advisor(), Model(), pid, p.store.get_project(pid)) == 1
    assert [q['id'] for q in calls[0]] == [watches[0]['id']]
    assert p.questions.assess(Advisor(), Model(), pid, p.store.get_project(pid)) == 0 and len(calls) == 1
    assert p.questions.view(pid)[0]['latest']['system']['visibility'] == 'internal'
    assert p.questions.view(pid)[0]['latest']['human']['note'] == '人的原判断'
    assert material(p, pid, watches[0]['id'])[0]['status'] == 'assessed'
    assert p.questions.view(pid)[1]['latest'] == {}


def test_unknown_advisor_does_not_consume_material(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    class Advisor:
        def assess(self, *_args):
            raise ModelError('synthetic unknown', code='request_outcome_unknown')
    class Model:
        ready = True
    with pytest.raises(ModelError):
        p.questions.assess(Advisor(), Model(), pid, {})
    assert len(p.questions.new_materials(pid, watches[0]['id'])) == 1
    assert len(p.questions.history(watches[0]['id'])) == 2


def test_model_cannot_borrow_unreferenced_amount_or_material(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    class Advisor:
        def assess(self, _llm, _project, questions):
            q = questions[0]
            return [{'id': q['id'], 'status': 'progress', 'note': '交付100台', 'material_ids': [q['materials'][0]['id']]}]
    class Model:
        ready = True
    assert p.questions.assess(Advisor(), Model(), pid, {}) == 0
    assert len(p.questions.history(watches[0]['id'])) == 2


def test_comparison_failure_does_not_route_new_material(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    intake(app, web, p, pid, '合成光电生产光学模组。')
    source = intake(app, web, p, pid, '合成光电交付10台光学模组。', run=False)
    def fail(*_args, **_kwargs):
        raise ModelError('synthetic unknown', code='request_outcome_unknown')
    p._record_changes = fail
    with pytest.raises(ModelError):
        p.run_internal(p.store.enqueue(pid, 'internal'))
    assert material(p, pid, watches[0]['id']) == []
    assert source not in p.store.analyzed_source_ids(pid)


def test_new_ai_question_attaches_existing_bp_without_reparsing(tmp_path):
    app, web, p, pid, _ = setup(tmp_path)
    intake(app, web, p, pid, '合成光电生产光学模组。')
    before = list(app.state.synthetic_transport.calls)
    class Advisor:
        def suggest(self, *_args):
            return [{'text': '光学模组进展如何', 'label': '光学模组', 'queries': ['合成光电 光学模组'], 'criteria': '产品进展'}]
    class Model:
        ready = True
    assert p.questions.suggest(Advisor(), Model(), pid, {}, {}, {'summary': '合成 BP'}) == 1
    watch = next(w for w in p.monitor.watchpoints(pid) if w['origin'] == 'ai')
    assert len(material(p, pid, watch['id'])) == 1
    assert watch['visibility'] == 'internal'
    assert app.state.synthetic_transport.calls == before


def test_owner_material_assessment_cannot_be_public_and_returns_readable_error(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    [hit] = material(p, pid, watches[0]['id'])
    reply = web.post(f'/api/studio/projects/{pid}/questions/{watches[0]["id"]}/assessments',
                     json={'status': 'progress', 'material_ids': [hit['id']], 'visibility': 'public'})
    assert reply.status_code == 403, reply.text
    assert len(p.questions.history(watches[0]['id'])) == 2


@pytest.mark.parametrize('change', ['permission', 'question'])
def test_revocation_during_advisor_response_rolls_back_material_judgement(tmp_path, change):
    app, web, p, pid, watches = setup(tmp_path)
    source = intake(app, web, p, pid, '合成光电交付10台光学模组。')
    class Advisor:
        def assess(self, _llm, _project, questions):
            with closing(p.store._connect()) as db, db:
                if change == 'permission':
                    db.execute('INSERT OR REPLACE INTO studio_source_permissions VALUES (?,?,?,?,?)',
                               (source, pid, '', 'synthetic', '2026-10-12'))
                else:
                    db.execute('UPDATE studio_watchpoints SET label=? WHERE id=?', ('融资', watches[0]['id']))
            return [{'id': questions[0]['id'], 'status': 'progress', 'material_ids': [questions[0]['materials'][0]['id']]}]
    class Model:
        ready = True
    with pytest.raises(ValueError):
        p.questions.assess(Advisor(), Model(), pid, {})
    assert len(p.questions.history(watches[0]['id'])) == 2
    with closing(p.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_question_consumed_materials').fetchone()[0] == 0


def test_material_overflow_is_not_consumed_until_sent_and_no_insight_is_read_only(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    for i in range(7):
        intake(app, web, p, pid, f'合成光电交付{i}台光学模组。')
    view = p.questions.view(pid)[0]
    assert view['material_count'] == 7 and view['materials_truncated']
    assert len(view['materials']) == 6
    before = list(app.state.synthetic_transport.calls)
    class Model:
        ready = True
    assert p.questions.assess(None, Model(), pid, {}) == 0
    assert len(p.questions.new_materials(pid, watches[0]['id'])) == 6
    assert app.state.synthetic_transport.calls == before
    calls = []
    class Declined:
        def assess(self, _llm, _project, questions):
            calls.append(questions)
            return []
    assert p.questions.assess(Declined(), Model(), pid, {}) == 0
    assert p.questions.new_materials(pid, watches[0]['id']) == []
    assert [len(c[0]['materials']) for c in calls] == [6, 1]
    assert p.questions.assess(Declined(), Model(), pid, {}) == 0
    assert p.questions.new_materials(pid, watches[0]['id']) == []
    assert len(calls) == 2


def test_idempotent_material_receipt_cannot_bypass_revoked_source(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    source = intake(app, web, p, pid, '合成光电交付10台光学模组。')
    [hit] = material(p, pid, watches[0]['id'])
    judgement = Assessment(status='progress', material_ids=(hit['id'],), idempotency_key='material-receipt')
    p.questions.record(pid, watches[0]['id'], 'human', judgement)
    with closing(p.store._connect()) as db, db:
        db.execute('INSERT OR REPLACE INTO studio_source_permissions VALUES (?,?,?,?,?)', (source, pid, '', 'synthetic', '2026-10-12'))
    with pytest.raises(ValueError):
        p.questions.record(pid, watches[0]['id'], 'human', Assessment(status='unchanged', idempotency_key='material-receipt'))
    assert p.questions.view(pid)[0]['latest']['human']['note'] == '人的原判断'
    assert len(p.questions.history(watches[0]['id'])) == 3


def test_new_owner_question_routes_existing_bp_and_uses_only_existing_spec_call(tmp_path):
    app, web, p, pid, _ = setup(tmp_path)
    intake(app, web, p, pid, '合成光电生产光学模组。')
    transport = app.state.synthetic_transport
    normal = transport.model_result
    # A deterministic fake spec, still through the real existing HTTP call.
    from project_watch.studio.monitor import _WATCH_SYSTEM
    transport.model_result = lambda system, user: ({'label': '光学模组', 'queries': ['合成光电 光学模组'], 'criteria': '产品'}
                                                   if system.startswith(_WATCH_SYSTEM) else normal(system, user))
    before = len(transport.calls)
    reply = web.post(f'/api/studio/projects/{pid}/watchpoints', json={'text': '跟踪光学模组', 'alert': False})
    assert reply.status_code == 201, reply.text
    assert len(material(p, pid, reply.json()['id'])) == 1
    assert len(transport.calls) == before + 1


def test_lost_lease_rolls_back_assessment_and_material_consumption(tmp_path):
    app, web, p, pid, watches = setup(tmp_path)
    intake(app, web, p, pid, '合成光电交付10台光学模组。')
    [hit] = material(p, pid, watches[0]['id'])
    def guard(db):
        if db.execute('SELECT count(*) FROM studio_question_assessments WHERE watch_id=?', (watches[0]['id'],)).fetchone()[0] > 2:
            raise RuntimeError('synthetic lost lease after insert')
    with p.store.write_scope(guard), pytest.raises(RuntimeError, match='lost lease'):
        p.questions.record(pid, watches[0]['id'], 'system', Assessment(status='progress', material_ids=(hit['id'],)),
                           consumed_materials=(hit,))
    assert len(p.questions.history(watches[0]['id'])) == 2
    assert len(p.questions.new_materials(pid, watches[0]['id'])) == 1


@pytest.mark.parametrize('replacement', ['new_extractor', 'public', 'wrong_source'])
def test_non_intake_extractions_and_public_or_wrong_source_snapshots_cannot_route(tmp_path, replacement):
    app, web, p, pid, watches = setup(tmp_path)
    source = intake(app, web, p, pid, '合成光电交付10台光学模组。')
    [hit] = material(p, pid, watches[0]['id'])
    record = p.evidence.get(pid, hit['ref']['evidence_version_id'])
    incoming = EvidenceInput(project_id=pid, source_key=record['source_key'],
                             extraction_version='synthetic-unapproved-' + replacement,
                             source_type='public_page' if replacement == 'public' else 'document',
                             visibility='public' if replacement == 'public' else 'internal',
                             read_scope='partial', internal_source_id='not-the-intake-source' if replacement == 'wrong_source' else source,
                             raw_content_hash=record['raw_content_hash'],
                             parts=(EvidencePart(text='合成光电交付20台光学模组。', locator='stored text', locator_kind='stored_text'),))
    changed = p.evidence.save(incoming)
    refs = {'S1': {'source_id': source, 'evidence_version_id': changed['id'], 'part_index': 0}}
    assert p.questions.route_materials(pid, refs, {source}, binding=p.model_binding()) == 0
    assert material(p, pid, watches[0]['id']) == []
    assert p.questions.history(watches[0]['id'])[0]['note'] == 'Agent 原判断'
