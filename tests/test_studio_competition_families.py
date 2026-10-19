"""Offline duplicate-release regressions; no claims about authentic independence."""

import hashlib
from types import SimpleNamespace

import pytest
from test_studio_agent_instance import make_service
from test_studio_bilateral_competition import reviewer

from project_watch.studio.competition import assess_relation
from project_watch.studio.competition_research import CompetitionResearch
from project_watch.studio.material_package import compile_package
from project_watch.studio.monitoring_contracts import (
    EvidenceInput,
    EvidencePart,
    FactInput,
    LocatedRef,
    ObjectInput,
)
from project_watch.studio.provenance import provenance
from project_watch.studio.relationship_contracts import RelationshipCorrection, RelationshipDecision

BODY = ('Alpha builds Alpha organ for drug screening; buyers are pharma; delivery is software.\n'
        'Beta builds Beta organ for drug screening; buyers are pharma; delivery is software.\n'
        + 'Synthetic original product release with located statements. ' * 25)


def snapshot(service, pid, index, *, legacy=False, body=BODY, title=None):
    url = f'https://publisher-{index}.example/release'
    hit = {'url': url, 'title': title or f'Different headline {index}'}
    if not legacy:
        return CompetitionResearch(SimpleNamespace(store=service.store, evidence=service.evidence))._snapshot(
            pid, hit, body, 'partial')
    stored = (hit['title'] + '\n' + body)[:20000]
    return service.evidence.save(EvidenceInput(project_id=pid,
        source_key='competition:partial:' + hashlib.sha256(url.encode()).hexdigest(),
        source_type='public_page', read_scope='partial', visibility='public',
        extraction_version='competition-source-v1', **provenance(url, stored),
        parts=(EvidencePart(locator_kind='stored_text', locator=url, text=stored),)))


def pair(tmp_path, legacy):
    service, pid = make_service(tmp_path / 'families.sqlite3')
    service.relationships.assessment_validator = assess_relation
    companies, products, sources = [], [], []
    for index, name in enumerate(('Alpha', 'Beta')):
        evidence = snapshot(service, pid, index, legacy=legacy[index])
        text = evidence['parts'][0]['text']
        quote = next(line for line in BODY.splitlines() if line.startswith(name))
        start = text.index(quote)
        ref = LocatedRef(evidence_version_id=evidence['id'], part_index=0,
                         start=start, end=start + len(quote), quote=quote)
        company = service.objects.save(pid, ObjectInput(kind='company', name=name,
            visibility='public', refs=(ref,)), actor='user')
        product = service.objects.save(pid, ObjectInput(kind='product', name=name + ' organ',
            parent_id=company['id'], visibility='public', refs=(ref,)), actor='user')
        for predicate, value in (('use_case', 'drug screening'), ('buyer_type', 'pharma'), ('delivery', 'software')):
            service.objects.save_fact(pid, FactInput(subject_id=product['id'], predicate=predicate,
                value=value, text=value, visibility='public', basis='actual', subject_attribution='explicit', refs=(ref,)))
        companies.append(company)
        products.append(product)
        sources.append(evidence)
    scope = dict(subject_id=companies[0]['id'], peer_id=companies[1]['id'],
                 subject_product_id=products[0]['id'], peer_product_id=products[1]['id'])
    return service, pid, scope, sources


@pytest.mark.parametrize('legacy', [(False, False), (True, True), (True, False)])
def test_copied_body_is_not_bilateral_support(tmp_path, legacy):
    service, pid, scope, _ = pair(tmp_path, legacy)
    result = assess_relation(service, pid, scope, 'direct', review=reviewer)
    assert result['support'] == 'pending'
    assert 'distinct_bilateral_sources' in result['missing']
    assert result['review'] is None


def test_new_snapshots_keep_title_locators_but_share_body_family(tmp_path):
    service, pid = make_service(tmp_path / 'snapshot.sqlite3')
    first = snapshot(service, pid, 0, body=BODY * 20, title='A')
    second = snapshot(service, pid, 1, body=BODY * 20, title='B' * 300)
    assert first['propagation_family'] == second['propagation_family']
    assert first['parts'][0]['locator'] != second['parts'][0]['locator']
    assert first['parts'][0]['text'].startswith('A\n')
    assert second['parts'][0]['text'].startswith('B' * 300 + '\n')


def test_existing_support_is_reprojected_without_history_write(tmp_path, monkeypatch):
    import project_watch.studio.competition as competition

    service, pid, scope, sources = pair(tmp_path, (True, True))
    # Reproduce the old contract through normal immutable writes, not SQL edits.
    with monkeypatch.context() as patch:
        patch.setattr(competition, 'effective_family', lambda e: {
            'propagation_family': e['propagation_family'], 'family_basis': e['family_basis']})
        old = assess_relation(service, pid, scope, 'direct', review=reviewer)
        assert old['support'] == 'supported'
        service.relationships.save_assessment(pid, old)
    history = service.relationships.history(pid)
    current = service.relationships.page(pid, include_internal=False)[0]
    assert current['support'] == 'pending' and current['previous_support'] == 'supported'
    assert current['validity_reason'] == 'shared_propagation_family'
    package = compile_package(service, pid)
    assert package['relationships'][0]['support'] == 'pending'
    families = {e['propagation_family'] for e in package['evidence']}
    assert len(families) == 1
    assert BODY not in str(package)
    assert service.relationships.history(pid) == history
    assert service.evidence.get(pid, sources[0]['id']) == sources[0]
    service.relationships.correct(service, pid, RelationshipCorrection(expected_version=1,
        idempotency_key='human-family-choice', changes=(
            RelationshipDecision(**scope, decision='include', reason='Human choice'),)))
    assert service.relationships.page(pid)[0]['origin'] == 'user_override'


def test_withdrawn_copy_remains_missing_and_history_is_retained(tmp_path):
    service, pid, scope, sources = pair(tmp_path, (True, False))
    service.evidence.dependencies.withdraw(pid, sources[1]['id'], 'Synthetic correction', actor='test')
    result = assess_relation(service, pid, scope, 'direct', review=reviewer)
    assert result['support'] == 'pending' and 'current_bilateral_facts' in result['missing']
    assert service.evidence.get(pid, sources[1]['id']) == sources[1]


def test_different_original_body_remains_distinct(tmp_path):
    service, pid = make_service(tmp_path / 'different.sqlite3')
    first = snapshot(service, pid, 0)
    second = snapshot(service, pid, 1, body=BODY.replace('Synthetic original', 'Independent interview'))
    assert first['propagation_family'] != second['propagation_family']


def test_whitespace_copy_and_legacy_short_body_use_body_not_headline(tmp_path):
    from project_watch.studio.provenance import effective_family

    service, pid = make_service(tmp_path / 'whitespace.sqlite3')
    first = snapshot(service, pid, 0, legacy=True)
    copy = snapshot(service, pid, 1, body=BODY.replace(' ', '  ').replace('\n', '\n\n'))
    assert effective_family(first) == effective_family(copy)
    short = snapshot(service, pid, 2, legacy=True, title='A' * 600, body='short')
    assert effective_family(short)['family_basis'] == 'source_only'


@pytest.mark.parametrize('change', [
    {'extraction_version': 'manual-v1'}, {'read_scope': 'summary'},
    {'source_key': 'manual'}, {'parts': []},
])
def test_unknown_formats_keep_recorded_provenance(tmp_path, change):
    from project_watch.studio.provenance import effective_family

    service, pid = make_service(tmp_path / 'formats.sqlite3')
    evidence = snapshot(service, pid, 0, legacy=True)
    assert effective_family({**evidence, **change}) == {
        k: evidence[k] for k in ('propagation_family', 'family_basis')}
