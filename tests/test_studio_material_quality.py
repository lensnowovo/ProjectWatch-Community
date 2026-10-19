"""M3B content regressions use fictional materials and no provider network."""

import runpy
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_studio_agent_instance import make_service
from test_studio_application_parity import headers
from test_studio_evidence_versions import sample

from project_watch.studio.business_profile import BusinessProfiles
from project_watch.studio.monitoring_contracts import LocatedRef, ObjectInput

SUPPORT = runpy.run_path(str(Path(__file__).with_name('distribution_journey_support.py')))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Material quality regression cannot use real network')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


def compare(tmp_path, text, proposal, *, previous_at='2026-09-01T10:00:00Z'):
    app, _ = SUPPORT['make_app'](tmp_path)
    pipeline = app.state.studio_pipeline
    pid = pipeline.repository.create_project('合成精密科技').id
    evidence = pipeline.evidence.save(sample(pid, text))
    refs = {'F1': {'source_id': 'new-source', 'source_name': 'Supplement-2026-10-12.txt',
                   'locator': 'line 1', 'evidence_version_id': evidence['id'], 'part_index': 0}}
    if previous_at:
        # Explicit old material period, not an assertion based on processing time.
        old = pipeline.evidence.save(sample(pid, f'截至{previous_at[:10]}的合成报告。'))
        refs['F0'] = {'source_id': 'old-source', 'source_name': 'Synthetic-T0.txt',
                      'locator': 'line 1', 'evidence_version_id': old['id'], 'part_index': 0}

    class Model:
        def json(self, system, user, **_kwargs):
            assert '新一批内部资料' in system
            return {'changes': [dict(proposal, refs=['F1'])]}

    pipeline._record_changes(pid, Model(), refs, {'new-source'},
                            {'summary': '公司从事合成模组', 'updated_at': previous_at},
                            [{'text': text, 'ref': 'F1'}])
    return pipeline.store.doc_changes(pid)[0]['texts']


@pytest.mark.parametrize('text,extra', [
    ('合成精密科技产品包括 ALPHA、MiniALPHA 与 H-MTD。', {}),
    ('2025年6月2日，合成精密科技完成旧款产品测试。',
     {'event_date': '2025-06-02', 'event_quote': '2025年6月2日，合成精密科技完成旧款产品测试。'}),
    ('2026年9月2日，合成精密科技计划完成产品测试。',
     {'event_date': '2026-09-02', 'event_quote': '2026年9月2日，合成精密科技计划完成产品测试。'}),
    ('2026年9月2日，合成精密科技尚未完成产品测试。',
     {'event_date': '2026-09-02', 'event_quote': '2026年9月2日，合成精密科技尚未完成产品测试。'}),
    ('2026年9月2日，合成精密科技未交付产品。',
     {'event_date': '2026-09-02', 'event_quote': '2026年9月2日，合成精密科技未交付产品。'}),
    ('2026年9月2日，合成精密科技的演示不代表已完成验收。',
     {'event_date': '2026-09-02', 'event_quote': '2026年9月2日，合成精密科技的演示不代表已完成验收。'}),
    ('2030年9月2日，合成精密科技完成产品测试。',
     {'event_date': '2030-09-02', 'event_quote': '2030年9月2日，合成精密科技完成产品测试。'}),
    ('2026年9月2日，合成精密科技完成产品测试。',
     {'event_date': '2026-09-03', 'event_quote': '2026年9月2日，合成精密科技完成产品测试。'}),
    ('合成精密科技完成产品测试。',
     {'event_date': '2026-09-02', 'event_quote': '2026年9月2日，合成精密科技完成产品测试。'}),
    ('2026年9月2日，合成精密科技成立于2010年。',
     {'event_date': '2026-09-02', 'event_quote': '2026年9月2日，合成精密科技成立于2010年。'}),
])
def test_supplement_does_not_invent_a_new_business_event(tmp_path, text, extra):
    [change] = compare(tmp_path, text, {'kind': '新进展', 'text': '显示技术和客户应用的新进展',
                                      'event_ref': 'F1', **extra})
    assert change['kind'] == '资料补充'
    if extra.get('event_quote') == text:
        assert change['text'] == text
        assert change['refs'][0]['quote'] == text
    else:
        # A model proposal is not supported by an arbitrary same-page point.
        assert change['evidence_support'] == 'source_only'
        assert not change['refs'][0].get('quote')
        assert change['proposed_text'] == '显示技术和客户应用的新进展'
        assert change['text'] == '新增资料，未定位到对应的新进展原句。'


@pytest.mark.parametrize('day', ['2026年9月2日', '2026-09-02'])
def test_later_dated_completed_action_retains_exact_event_reference(tmp_path, day):
    quote = f'{day}，合成精密科技完成产品验收。'
    [change] = compare(tmp_path, quote, {'kind': '新进展', 'text': '完成产品验收',
                                       'event_date': '2026-09-02', 'event_ref': 'F1', 'event_quote': quote})
    assert change['kind'] == '新进展'
    assert change['event_date'] == '2026-09-02'
    assert change['text'] == quote  # No uncited order/revenue claim can be appended.
    assert change['refs'][0]['quote'] == quote


def test_missing_baseline_cannot_prove_recent_progress(tmp_path):
    quote = '2026年9月2日，合成精密科技完成产品验收。'
    [change] = compare(tmp_path, quote, {'kind': '新进展', 'text': '完成产品验收',
        'event_date': '2026-09-02', 'event_ref': 'F1', 'event_quote': quote}, previous_at='')
    assert change['kind'] == '资料补充'


@pytest.mark.parametrize('same_part', [False, True])
def test_profile_product_codes_receive_all_permitted_source_quotes(tmp_path, same_part):
    app, transport = SUPPORT['make_app'](tmp_path)
    original = transport.model_result

    def reply(system, user):
        if '一页纸档案' in system:
            return {'summary': '合成产品目录', 'sections': [{'title': '业务与产品', 'points': [
                {'text': '产品包括 ALPHA、MiniALPHA 与 H-MTD。', 'refs': ['F1']},
                {'text': 'ALPHA用于合成车载连接。', 'refs': ['F1']},
                {'text': 'SECRET-CODE用于保密业务。', 'refs': ['F1']},
            ]}]}
        return original(system, user)
    transport.model_result = reply
    with TestClient(app) as client:
        SUPPORT['configure'](client)
        created = client.post('/api/studio/projects', json={
            'entity': {'legal_name': '合成精密科技'}, 'consent': True})
        pid = created.json()['id']
        route = '/api/studio/projects/' + pid
        store, service = app.state.studio_store, app.state.studio_application
        grant = service.issue_grant(pid, 'write', 'synthetic-material-test', allow_internal=True)
        with store._connect() as db, db:
            db.execute('DELETE FROM studio_jobs')
        # Even a foreign public version containing SECRET-CODE isn't a donor.
        other = app.state.intake_worker.repository.create_project('另一个合成项目')
        app.state.studio_pipeline.evidence.save(sample(other.id, 'SECRET-CODE用于保密业务。').model_copy(update={
            'source_key': 'public-profile:foreign', 'visibility': 'public'}))
        authorized = []
        product_texts = ['ALPHA用于合成车载连接。', 'MiniALPHA用于缩小接口。', 'H-MTD用于合成高速数据传输。']
        files = [''.join(product_texts)] if same_part else product_texts
        for i, text in enumerate([*files, 'SECRET-CODE用于保密业务。']):
            assert client.post('/api/studio/agent/projects/' + pid + '/sources', headers=headers(grant),
                data={'expected_version': str(service.project(pid)['config_version']), 'idempotency_key': f'upload-{i}'},
                files={'file': (
                f'Synthetic-{i}.txt', text.encode(), 'text/plain')}).status_code == 202
            parsed = app.state.intake_worker.process_one()
            assert parsed.status == 'completed'
            if i < len(files):
                result = client.post(route + '/sources/' + parsed.source_id + '/authorize-analysis', json={
                    'expected_version': service.project(pid)['config_version'], 'idempotency_key': f'consent-{i}'})
                assert result.status_code == 200, result.text
                authorized.append(parsed.source_id)
            else:
                assert not store.source_analysis_allowed(pid, parsed.source_id, app.state.studio_pipeline.model_binding())
        with store._connect() as db, db:
            db.execute('DELETE FROM studio_jobs')
        job = store.enqueue(pid, 'internal')
        app.state.studio_pipeline.run_internal(job)
        points = client.get(route).json()['profile']['sections'][0]['points']
        assert {r['source_id'] for r in points[0]['refs']} == set(authorized)
        assert [r['quote'] for r in points[0]['refs']] == [
            'ALPHA用于合成车载连接。', 'MiniALPHA用于缩小接口。', 'H-MTD用于合成高速数据传输。']
        # ALPHA is not a substring match to MiniALPHA; denied input isn't a donor.
        assert [r['source_id'] for r in points[1]['refs']] == authorized[:1]
        assert len(points) == 2  # SECRET-CODE has no permitted source; the claim is omitted.
        for point in points:
            for ref in point['refs']:
                app.state.studio_pipeline.evidence.resolve(pid, LocatedRef(**{
                    k: ref[k] for k in LocatedRef.model_fields}))


def test_long_same_part_citation_still_contains_requested_code(tmp_path):
    app, _ = SUPPORT['make_app'](tmp_path)
    pipeline = app.state.studio_pipeline
    pid = pipeline.repository.create_project('合成精密科技').id
    text = '合成背景说明' * 200 + 'H-MTD用于合成传输。'
    evidence = pipeline.evidence.save(sample(pid, text))
    ref = {'source_id': 'synthetic', 'source_name': 'synthetic.txt', 'locator': 'line 1',
           'evidence_version_id': evidence['id'], 'part_index': 0}
    cite = pipeline._internal_cite('F1', ref, text, ['H-MTD'])
    assert 'H-MTD' in cite['quote'] and len(cite['quote']) <= 300
    pipeline.evidence.resolve(pid, LocatedRef(**{k: cite[k] for k in LocatedRef.model_fields}))


def business(tmp_path, text, names):
    service, pid = make_service(tmp_path / 'business.sqlite3')
    service.objects.ensure_subject(pid)
    service.evidence.save(sample(pid, text).model_copy(update={
        'source_key': 'public-profile:company', 'visibility': 'public'}))
    profiles = BusinessProfiles(service.store, service.objects)
    inputs = profiles.inputs(pid, binding='synthetic')
    rows = [{'name': name, 'company': 'Synthetic Optics', 'source': inputs['parts'][0]['marker'],
             'quote': text, 'basis': 'actual', 'polarity': 'asserted'} for name in names]
    result = profiles.apply(pid, inputs, {'products': rows})
    return service, pid, result


@pytest.mark.parametrize('names', [
    ['合成A注射液（曦达®）', '曦达'], ['曦达', '合成A注射液（曦达®）'],
    ['合成A注射液（商品名：曦达）', '曦达'],
])
def test_explicit_product_brand_does_not_create_two_products(tmp_path, names):
    service, pid, result = business(tmp_path, f'Synthetic Optics的{next(n for n in names if "注射液" in n)}用于合成治疗。', names)
    assert result['accepted'] == 2 and len(result['product_ids']) == 1
    product = service.objects.get(pid, result['product_ids'][0])
    assert product['name'] == next(n for n in names if '注射液' in n)
    assert {a['name'] for a in product['aliases']} == {'曦达'}


def test_similar_names_without_explicit_brand_pair_stay_separate(tmp_path):
    service, pid, result = business(tmp_path,
        'Synthetic Optics的曦达和曦达Plus是两种不同产品。', ['曦达', '曦达Plus'])
    assert len(result['product_ids']) == 2
    assert {o['name'] for o in service.objects.page(pid) if o['kind'] == 'product'} == {'曦达', '曦达Plus'}


def test_acquired_process_license_is_not_a_sold_product(tmp_path):
    service, pid, result = business(tmp_path,
        'Synthetic Optics获得LVV生产工艺许可，用于改进自己的制造流程。', ['LVV生产工艺许可'])
    assert result['accepted'] == 0 and result['rejected'] == 1
    assert not [o for o in service.objects.page(pid) if o['kind'] == 'product']


def test_outbound_licensing_service_is_not_blocked(tmp_path):
    _, _, result = business(tmp_path,
        'Synthetic Optics对外提供LVV生产工艺许可服务，交付给制药企业。', ['LVV生产工艺许可服务'])
    assert result['accepted'] == 1


def test_human_product_name_and_history_are_not_replaced_by_brand_pair(tmp_path):
    text = 'Synthetic Optics的合成A注射液（曦达®）用于合成治疗。'
    service, pid, result = business(tmp_path, text, ['曦达'])
    original = service.objects.get(pid, result['product_ids'][0])
    human = service.objects.save(pid, ObjectInput.model_validate({k: original[k] for k in ObjectInput.model_fields}).model_copy(update={'name': '人的产品名'}),
        actor='user', object_id=original['id'], expected_version=original['version'])
    # A new actual input version, not an attempt to replay a completed proposal.
    service.evidence.save(sample(pid, text + ' 新版说明。').model_copy(update={
        'source_key': 'public-profile:company', 'visibility': 'public'}))
    profiles = BusinessProfiles(service.store, service.objects)
    inputs = profiles.inputs(pid, binding='synthetic')
    rows = [{'name': name, 'company': 'Synthetic Optics', 'source': inputs['parts'][0]['marker'],
             'quote': text, 'basis': 'actual', 'polarity': 'asserted'} for name in ['曦达', '合成A注射液（曦达®）']]
    updated = profiles.apply(pid, inputs, {'products': rows})
    assert updated['accepted'] == 0 and updated['rejected'] == 2
    assert service.objects.get(pid, human['id']) == human
