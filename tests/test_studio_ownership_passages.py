"""Synthetic continuous span selection; not live ownership accuracy acceptance."""
from copy import deepcopy

import pytest
from test_studio_business_identity import FULL, PRODUCT, SHORT, contextual_product, setup


def prepared(tmp_path, *, text=None, company=FULL):
    service, pid, subject, profiles = setup(tmp_path)
    quote = PRODUCT.replace(SHORT, '该公司')
    text = text or FULL + '研发软件。研发团队有独立的测试流程。' + quote
    inputs, data = contextual_product(service, pid, profiles, text, quote, '', company=company)
    part = next(p for p in inputs['parts'] if p['text'] == text)
    data['products'][0]['ownership_passage'] = part['marker'] + ':P1'
    return service, pid, subject, profiles, inputs, data, part


def test_selected_paragraph_preserves_intervening_sentence_and_refs(tmp_path):
    from project_watch.studio.business_identity import has_current_product_ownership

    service, pid, subject, profiles, inputs, data, part = prepared(tmp_path)
    before = deepcopy(inputs), deepcopy(data)
    result = profiles.apply(pid, inputs, data)
    assert result['accepted'] == 1
    product = service.objects.get(pid, result['product_ids'][0])
    assert product['refs'][0]['quote'] == data['products'][0]['quote']
    context = next(r for r in product['refs'] if r['quote'] == part['text'])
    assert context['start'] == 0 and context['end'] == len(part['text'])
    assert context['part_index'] == part['part_index']
    assert all(any(r['quote'] == part['text'] for r in f['refs']) for f in service.objects.facts(pid, product['id']))
    assert has_current_product_ownership(service.store, pid, subject, product, include_internal=False)
    assert (inputs, data) == before
    assert profiles.apply(pid, inputs, data) == result
    service.evidence.dependencies.withdraw(pid, context['evidence_version_id'], 'synthetic withdrawal', actor='user')
    assert all(f['dependency']['status'] == 'stale' for f in service.objects.facts(pid, product['id']))


def test_payload_exposes_stable_local_passages_without_offsets_or_private_paths(tmp_path):
    from project_watch.studio.business_profile import model_payload

    _, _, _, _, inputs, _, part = prepared(tmp_path)
    payload = model_payload(inputs)
    source = next(s for s in payload['sources'] if s['source'] == part['marker'])
    assert source['ownership_passages'] == [{'marker': part['marker'] + ':P1', 'text': part['text']}]
    assert payload == model_payload(inputs)
    assert 'evidence_version_id' not in source and 'source_id' not in source


def test_paragraph_coordinates_keep_unicode_and_crlf_exact():
    from project_watch.studio.business_profile import ownership_passages

    text = '  合成标题\r\n原始内容。\r\n\r\n第二段。\n\n第三段。'
    part = {'marker': 'B2', 'text': text}
    spans = ownership_passages(part)
    assert [s['text'] for s in spans] == ['合成标题\r\n原始内容。', '第二段。', '第三段。']
    assert [s['marker'] for s in spans] == ['B2:P1', 'B2:P2', 'B2:P3']
    assert all(text[s['start']:s['end']] == s['text'] for s in spans)


def test_passages_are_bounded_without_truncating_paragraphs():
    from project_watch.studio.business_profile import ownership_passages

    assert ownership_passages({'marker': 'B1', 'text': 'x' * 2001}) == []
    spans = ownership_passages({'marker': 'B1', 'text': '\n\n'.join(['x' * 2001, 'Y', *['Z'] * 30])})
    assert len(spans) == 19 and spans[0]['marker'] == 'B1:P2'
    assert all(len(s['text']) <= 2000 for s in spans)


@pytest.mark.parametrize('marker', ['B99:P1', 'B1:P99', 'bad', '', None, ['B1:P1']])
def test_unknown_or_malformed_passage_cannot_prove_pronoun_ownership(tmp_path, marker):
    _, pid, _, profiles, inputs, data, _ = prepared(tmp_path)
    data['products'][0]['ownership_passage'] = marker
    assert profiles.apply(pid, inputs, data)['accepted'] == 0


def test_locator_cannot_borrow_another_selected_source(tmp_path):
    _, pid, _, profiles, inputs, data, part = prepared(tmp_path)
    another = next(p for p in inputs['parts'] if p['marker'] != part['marker'])
    data['products'][0]['ownership_passage'] = another['marker'] + ':P1'
    assert profiles.apply(pid, inputs, data)['accepted'] == 0


@pytest.mark.parametrize('same', [False, True])
def test_supplied_free_context_must_equal_selected_passage(tmp_path, same):
    _, pid, _, profiles, inputs, data, part = prepared(tmp_path)
    data['products'][0]['ownership_quote'] = part['text'] if same else FULL + '研发软件。' + data['products'][0]['quote']
    assert profiles.apply(pid, inputs, data)['accepted'] == int(same)


def test_noncontiguous_legacy_quote_is_not_automatically_repaired(tmp_path):
    _, pid, _, profiles, inputs, data, _ = prepared(tmp_path)
    data['products'][0].pop('ownership_passage')
    data['products'][0]['ownership_quote'] = FULL + '研发软件。' + data['products'][0]['quote']
    assert profiles.apply(pid, inputs, data)['accepted'] == 0


def test_quote_outside_selected_paragraph_cannot_borrow_owner(tmp_path):
    quote = PRODUCT.replace(SHORT, '该公司')
    _, pid, _, profiles, inputs, data, _ = prepared(tmp_path, text=FULL + '研发软件。\n\n' + quote)
    assert profiles.apply(pid, inputs, data)['accepted'] == 0


def test_overlong_source_paragraph_cannot_be_repaired_or_truncated(tmp_path):
    text = FULL + '研发软件。' + '合成内容' * 600 + PRODUCT.replace(SHORT, '该公司')
    _, pid, _, profiles, inputs, data, _ = prepared(tmp_path, text=text)
    assert profiles.apply(pid, inputs, data)['accepted'] == 0


@pytest.mark.parametrize('mode', ['unmapped_short', 'foreign_proposed', 'subsidiary'])
def test_locator_does_not_create_company_mapping(tmp_path, mode):
    text = SHORT + '研发软件。' + PRODUCT.replace(SHORT, '该公司')
    company = FULL
    if mode == 'foreign_proposed':
        text = FULL + '与对照光学有限公司合作。' + PRODUCT.replace(SHORT, '该公司')
        company = '对照光学有限公司'
    elif mode == 'subsidiary':
        text = FULL + '的子公司研发软件。' + PRODUCT.replace(SHORT, '该公司')
    _, pid, _, profiles, inputs, data, _ = prepared(tmp_path, text=text, company=company)
    assert profiles.apply(pid, inputs, data)['accepted'] == 0


def test_source_withdrawal_before_apply_does_not_publish_old_locator(tmp_path):
    service, pid, _, profiles, inputs, data, part = prepared(tmp_path)
    service.evidence.dependencies.withdraw(pid, part['evidence_version_id'], 'synthetic withdrawal', actor='user')
    assert profiles.apply(pid, inputs, data)['accepted'] == 0


def test_duplicate_paragraph_text_keeps_selected_second_occurrence(tmp_path):
    paragraph = FULL + '研发软件。' + PRODUCT.replace(SHORT, '该公司')
    service, pid, _, profiles, inputs, data, part = prepared(tmp_path, text=paragraph + '\n\n' + paragraph)
    data['products'][0]['ownership_passage'] = part['marker'] + ':P2'
    result = profiles.apply(pid, inputs, data)
    assert result['accepted'] == 1
    product = service.objects.get(pid, result['product_ids'][0])
    offset = len(paragraph) + 2
    assert product['refs'][0]['start'] == offset + paragraph.index(data['products'][0]['quote'])
    assert any(r['quote'] == paragraph and r['start'] == offset for r in product['refs'])


def test_source_edit_before_apply_invalidates_old_marker_input(tmp_path):
    from test_studio_evidence_versions import sample

    service, pid, _, profiles, inputs, data, _ = prepared(tmp_path)
    service.evidence.save(sample(pid, 'Changed source no longer contains the product').model_copy(update={
        'source_key': 'public-profile:context-product', 'visibility': 'public'}))
    assert profiles.apply(pid, inputs, data)['status'] == 'superseded'


@pytest.mark.parametrize('old_version', [None, 'business-v7-row-isolation', 'business-v8-located-passages',
                                       'business-v9-explicit-product-brands'])
def test_old_business_contract_in_competition_checkpoint_stops_before_calls(tmp_path, old_version):
    from test_studio_competition_research import research_pipeline

    from project_watch.studio.business_profile import PROMPT_VERSION as BUSINESS_VERSION
    from project_watch.studio.competition_research import research_inputs
    from project_watch.studio.execution import JobExecution

    pipeline, pid, calls, reads = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job)
    inputs = research_inputs(pipeline, pid)
    assert inputs['business_version'] == BUSINESS_VERSION
    if old_version is None:
        inputs.pop('business_version')
    else:
        inputs['business_version'] = old_version
    execution.checkpoint('competition_inputs', inputs)
    execution.checkpoint('competition_target_business', {'accepted': 1, 'product_ids': ['old-product']})
    with pytest.raises(ValueError, match='old matching contract'):
        pipeline.run(job)
    assert calls == [] and reads == []


def test_business_contract_changes_competition_input_fingerprint(tmp_path, monkeypatch):
    from test_studio_competition_research import research_pipeline

    from project_watch.studio import competition_research

    pipeline, pid, _, _ = research_pipeline(tmp_path)
    current = competition_research.research_inputs(pipeline, pid)
    monkeypatch.setattr(competition_research, 'BUSINESS_VERSION', 'synthetic-older-version')
    old = competition_research.research_inputs(pipeline, pid)
    assert current['input_version'] != old['input_version']
    assert current['queries'] == old['queries']
