"""Original-page quantities, not generated summaries or inferred company sales."""

from copy import deepcopy

import pytest
from test_studio_peer_comparison import numbers, quantity


def original(name='合成视界', value='10万', **kwargs):
    event = quantity(name, value, **kwargs)
    text = '公告正文\n' + event['title'] + '。\n公司介绍。'
    event['title'] = event['sources'][0]['title'] = name + '发布经营公告'
    event['sources'][0]['body_status'] = 'article_body'
    event['sources'][0]['original_parts'] = [
        {'evidence_version_id': 'synthetic-version', 'part_index': 0, 'text': text}]
    return event


def test_original_sentence_numbers_pair_and_preserve_exact_coordinates():
    own, peer = original(), original('合成竞品甲', '90000')
    result = numbers([own], [peer])
    assert len(result['groups']) == 1
    rows = result['groups'][0]['records']
    assert [r['value'] for r in rows] == ['100000', '90000']
    for row, event in zip(rows, [own, peer], strict=True):
        ref = row['ref']
        text = event['sources'][0]['original_parts'][0]['text']
        assert text[ref['start']:ref['end']] == ref['quote'] == row['quote']
        assert row['basis'] == 'original_text'
        assert row['title'] == event['sources'][0]['title']


@pytest.mark.parametrize('status', ['search_summary', 'unavailable', 'twin_body'])
def test_unread_or_unresolved_twin_text_is_not_original_evidence(status):
    event = original()
    event['sources'][0]['body_status'] = status
    assert numbers([event], [])['records'] == []


@pytest.mark.parametrize('rewrite', [
    lambda t: t.replace('出货量', '预计出货量'),
    lambda t: t.replace('出货量', '累计出货量'),
    lambda t: t.replace('10万', '约10万'),
    lambda t: t.replace('10万', '10万至12万'),
    lambda t: t.replace('10万台', '10万台左右'),
    lambda t: t.replace('合成视界2026', '合成视界的客户2026'),
    lambda t: t.replace('合成视界2026', '合成陌生公司2026'),
    lambda t: t.replace('合成视界2026', '2026'),
])
def test_body_sentence_cannot_borrow_actor_period_or_exact_number(rewrite):
    event = original()
    part = event['sources'][0]['original_parts'][0]
    part['text'] = rewrite(part['text'])
    # The actor from the source title cannot be lent to another sentence.
    assert numbers([event], [])['records'] == []


def test_original_conflicts_do_not_let_headline_value_win():
    event = original()
    event['sources'][0]['title'] = quantity('合成视界', '12万')['title']
    result = numbers([event], [original('合成竞品甲')])
    assert len(result['records']) == 3 and result['groups'] == []


def test_original_enrichment_is_local_public_current_and_never_mutates_event_listing(tmp_path):
    from test_studio_agent_instance import make_service

    from project_watch.studio.comparisons import with_originals

    service, pid = make_service(tmp_path / 'quantities.sqlite3')
    service.store.add_items(pid, 'company', [{'key': 'public', 'title': 'Synthetic Optics quarterly update',
        'url': 'https://news.example/public', 'source': 'Synthetic', 'snippet': '', 'published_at': '2026-09-01'}])
    item = service.store.untriaged(pid)[0]
    body = 'Synthetic Optics2026年Q1 中国ADAS“激光雷达”产品出货量10万台。'
    service.store.save_body(item['id'], 'article_body', body)
    events = [{'sources': [{'id': item['id'], 'body_status': 'article_body'}]}]
    before = deepcopy(events)
    enriched = with_originals(service.store, pid, events)
    part = enriched[0]['sources'][0]['original_parts'][0]
    assert part['text'] == body and part['evidence_version_id']
    assert events == before
    assert not with_originals(service.store, 'other-project', events)[0]['sources'][0].get('original_parts')
    ref = service.store.item_evidence(pid, item['id'])
    service.evidence.dependencies.withdraw(pid, ref['evidence_version_id'], reason='withdrawn', actor='owner')
    assert not with_originals(service.store, pid, events)[0]['sources'][0].get('original_parts')


def test_race_api_extracts_body_but_neither_returns_article_nor_changes_event_api(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline, store = app.state.studio_pipeline, app.state.studio_store
    project = pipeline.repository.create_project('合成视界')
    store.upsert_project(project.id, {'legal_name': '合成视界'}, consent=True)
    store.add_items(project.id, 'company', [{'key': 'report', 'title': '合成视界经营公告',
        'url': 'https://news.example/report', 'source': 'Synthetic', 'snippet': '', 'published_at': '2026-09-01'}])
    item = store.untriaged(project.id)[0]
    sentence = '合成视界2026年Q1 中国ADAS“激光雷达”产品出货量10万台。'
    body = '不应整段导出的公告上下文\n' + sentence + '\n不应整段导出的尾注'
    store.save_body(item['id'], 'article_body', body)
    store.create_event(project.id, {'category': 'company', 'title': '合成视界经营公告',
        'importance': 'notable', 'event_at': '2026-09-01'}, [item['id']])
    with TestClient(app) as client:
        response = client.get(f'/api/studio/projects/{project.id}/race')
        assert response.status_code == 200
        rows = response.json()['quantities']['records']
        assert len(rows) == 1 and rows[0]['quote'] == sentence
        from project_watch.studio.monitoring_contracts import LocatedRef
        pipeline.evidence.resolve(project.id, LocatedRef.model_validate(rows[0]['ref']), include_internal=False)
        assert '不应整段导出' not in response.text
        assert 'original_parts' not in str(store.list_events(project_id=project.id))
    store.save_body(item['id'], 'search_summary', sentence)
    with TestClient(app) as client:
        assert client.get(f'/api/studio/projects/{project.id}/race').json()['quantities']['records'] == []
