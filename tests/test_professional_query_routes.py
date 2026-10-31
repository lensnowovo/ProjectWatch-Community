"""A public organization name must not silently skip available index routes."""
from types import SimpleNamespace

import pytest

from project_watch.studio import collection_dsh, collection_targets, retrieval_queries


def role(monkeypatch, *, source='patent_search', relation='subsidiary', disabled=(), history=()):
    names = ('甲方研究有限公司', 'Example Research (City) Ltd.')
    checkpoints = {
        'collection_scope_v1': {'capabilities': dict.fromkeys(
            ['patent_search', 'openalex_affiliation', 'europepmc_affiliation'], 'available'),
            'source_decisions': {a: {'selection': 'disabled'} for a in disabled}},
        'update_handoff_v1': {'professional_query_history': list(history)},
    }
    target = dict(adapter=source, capability='available', query=names[1],
        query_context={'relationship': relation, 'subject': names[1]},
        ref={'evidence_version_id': 'e', 'quote': 'Applicant: ' + names[1]})
    monkeypatch.setattr(collection_targets, 'targets', lambda _: [target])
    monkeypatch.setattr(collection_dsh, '_refs', lambda _: {'C1': [{'evidence_version_id': 'e'}]})
    monkeypatch.setattr(collection_dsh, 'rounds', lambda _: [dict(
        plan={'units': [dict(id='chinese', source='openalex_affiliation', query=names[0]),
                        dict(id='english', source=source, query=names[1])]},
        receipts=[dict(unit_id='chinese', outcome='success', result_count=0),
                  dict(unit_id='english', outcome='success', result_count=3)])])
    return SimpleNamespace(parent=object(), execution=SimpleNamespace(read_checkpoint=checkpoints.get),
                           data={'actions': {}})


def test_english_applicant_is_not_covered_by_chinese_affiliation_query(monkeypatch):
    current = role(monkeypatch)
    queries = retrieval_queries.remaining_name_queries(current)
    assert {q['adapter'] for q in queries} == {'openalex_affiliation', 'europepmc_affiliation'}
    assert all(q['query'] == 'Example Research (City) Ltd.' and q['source_id'] == 'C1'
               and q['query'] in q['quote'] for q in queries)


@pytest.mark.parametrize('outcome', ['success', 'unavailable', 'failed', 'unknown'])
def test_any_prior_attempt_is_not_recommended_for_replay(monkeypatch, outcome):
    current = role(monkeypatch, history=[dict(adapter='openalex_affiliation',
        query='Example Research (City) Ltd.', outcome=outcome)])
    assert [q['adapter'] for q in retrieval_queries.remaining_name_queries(current)] == ['europepmc_affiliation']


def test_disabled_index_and_non_organization_queries_do_not_expand(monkeypatch):
    current = role(monkeypatch, disabled=['openalex_affiliation', 'europepmc_affiliation'])
    assert retrieval_queries.remaining_name_queries(current) == []
    current = role(monkeypatch, relation='product')
    assert retrieval_queries.remaining_name_queries(current) == []
    current = role(monkeypatch, source='google_patent')
    assert retrieval_queries.remaining_name_queries(current) == []


def test_unknown_or_withdrawn_source_is_not_replaced_with_invented_id(monkeypatch):
    current = role(monkeypatch)
    monkeypatch.setattr(collection_dsh, '_refs', lambda _: {})
    assert retrieval_queries.remaining_name_queries(current) == []
    current = role(monkeypatch)
    monkeypatch.setattr(collection_targets, 'targets', lambda _: [])
    assert retrieval_queries.remaining_name_queries(current) == []


def test_patent_document_shortlist_is_bounded_and_never_retries(monkeypatch):
    state = {'collection_scope_v1': {'capabilities': {'google_patent': 'available'}}}
    numbers = ['US10000001B2', 'US10000002B2', 'US10000003B2', 'US10000004B2']
    refs = {str(i): [{'direction': 'patent'}] for i in range(4)}
    sources = {str(i): dict(url='https://patents.google.com/patent/' + n + '/en', excerpt=n)
               for i, n in enumerate(numbers)}
    current = SimpleNamespace(parent=object(), data={'actions': {}}, available=lambda s: True,
        execution=SimpleNamespace(read_checkpoint=state.get, checkpoint_chunked=state.__setitem__))
    monkeypatch.setattr(collection_dsh, '_refs', lambda _: refs)
    monkeypatch.setattr(collection_dsh, 'source', lambda _, sid: sources[sid])
    monkeypatch.setattr(collection_dsh, 'rounds', lambda _: [])
    candidates = retrieval_queries.remaining_patent_documents(current)
    assert [c['query'] for c in candidates] == numbers[:3]
    assert all(c['quote'] == c['query'] for c in candidates)
    current.data['actions'] = {str(i): {'operation': {'tool': 'pw_collect', 'args': q}, 'status': 'started'}
                               for i, q in enumerate(candidates)}
    assert retrieval_queries.remaining_patent_documents(current) == []
    assert len(state['collection_patent_shortlist_v1']) == 3  # No fourth query.


@pytest.mark.parametrize('url,excerpt', [
    ('https://example.invalid/patent/US10000001B2/en', 'US10000001B2'),
    ('https://patents.google.com/patent/US10000001B2/en', 'unrelated excerpt'),
    ('https://patents.google.com/patent/guessed/en', 'guessed'),
])
def test_patent_document_needs_exact_saved_public_number(monkeypatch, url, excerpt):
    state = {'collection_scope_v1': {'capabilities': {'google_patent': 'available'}}}
    current = SimpleNamespace(parent=object(), data={'actions': {}}, available=lambda s: True,
        execution=SimpleNamespace(read_checkpoint=state.get, checkpoint_chunked=state.__setitem__))
    monkeypatch.setattr(collection_dsh, '_refs', lambda _: {'C1': [{'direction': 'patent'}]})
    monkeypatch.setattr(collection_dsh, 'source', lambda *a: dict(url=url, excerpt=excerpt))
    monkeypatch.setattr(collection_dsh, 'rounds', lambda _: [])
    assert retrieval_queries.remaining_patent_documents(current) == []
