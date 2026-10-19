"""Synthetic-only evidence selection: exact server-owned excerpts, not rewritten quotes."""
from copy import deepcopy

import pytest
from test_external_analysis import inputs, output

from project_watch.external_analysis import AnalysisEvidenceError
from project_watch.external_analysis_selection import (
    evidence_catalog,
    resolve_analysis_selection,
)
from project_watch.external_analysis_selection import (
    selection_prompt as external_analysis_prompt,
)


def selection_output(packet):
    raw = output(packet)
    catalog = evidence_catalog(packet)
    for item in [*raw['source_statements'], raw['relationship'], *raw['implications']]:
        selected = [next(c for c in catalog if c['ref_id'] == cite['ref_id']) for cite in item.pop('evidence')]
        item['external_evidence'] = [c['id'] for c in selected if c['kind'] == 'external']
        if item is raw['relationship'] or item in raw['implications']:
            item['internal_evidence'] = [c['id'] for c in selected if c['kind'] == 'internal']
    return raw


def test_year_and_growth_in_adjacent_sentences_stay_with_their_source(tmp_path):
    _, _, _, packet = inputs(tmp_path)
    packet['references'][0]['text'] = '示例公司在2025年披露季度业绩。收入增长12%，仍待核实。'
    raw = selection_output(packet)
    raw['source_statements'][0]['text'] = '来源称2025年该季度收入增长12%，仍待核实。'
    result = resolve_analysis_selection(packet, raw)
    assert result['source_statements'][0]['evidence'][0]['quote'] == packet['references'][0]['text']
    assert result['review_status'] == 'unreviewed'
    assert result['relationship_verified'] is False
    assert 'OUTPUT_SCHEMA' in external_analysis_prompt(packet)


@pytest.mark.parametrize('length', [4, 599, 600, 601, 602, 603, 1199, 3000])
def test_catalog_covers_exact_authorized_text_and_is_bounded(tmp_path, length):
    _, _, _, packet = inputs(tmp_path)
    ref = packet['references'][-1]
    ref['text'] = ('合成材料。' * 600)[:length]
    catalog = [c for c in evidence_catalog(packet) if c['ref_id'] == ref['ref_id']]
    covered = set()
    for c in catalog:
        assert 4 <= len(c['text']) <= 600
        assert c['text'] == ref['text'][c['start']:c['end']]
        covered.update(range(c['start'], c['end']))
    assert covered == set(range(length))
    assert evidence_catalog(packet) == evidence_catalog(deepcopy(packet))


@pytest.mark.parametrize('mutation,code', [('invented_number', 'unsupported_number'),
    ('other_quote_number', 'unsupported_number'), ('missing_internal', 'evidence_scope'),
    ('private_public_statement', 'evidence_scope'), ('unknown_id', 'citation_mismatch')])
def test_selection_never_weakens_evidence_guards(tmp_path, mutation, code):
    _, _, _, packet = inputs(tmp_path)
    packet['references'][-1]['text'] += '合成设备24台。'
    raw = selection_output(packet)
    if mutation == 'invented_number':
        raw['source_statements'][0]['text'] = '收入增长999%。'
    elif mutation == 'other_quote_number':
        raw['source_statements'][0]['text'] = '设备24台。'
    elif mutation == 'missing_internal':
        raw['relationship']['internal_evidence'] = []
    elif mutation == 'private_public_statement':
        raw['source_statements'][0]['external_evidence'] = raw['relationship']['internal_evidence']
    elif mutation == 'unknown_id':
        raw['relationship']['external_evidence'] = ['not-in-this-packet']
    with pytest.raises(AnalysisEvidenceError) as caught:
        resolve_analysis_selection(packet, raw)
    assert caught.value.code == code


def test_duplicate_or_mislabelled_id_uses_server_kind_without_adding_evidence(tmp_path):
    _, _, _, packet = inputs(tmp_path)
    raw = selection_output(packet)
    expected = resolve_analysis_selection(packet, raw)
    raw['relationship']['external_evidence'] += raw['relationship']['internal_evidence']
    assert resolve_analysis_selection(packet, raw) == expected
    raw['source_statements'][0]['external_evidence'] = raw['relationship']['external_evidence']
    with pytest.raises(AnalysisEvidenceError) as caught:
        resolve_analysis_selection(packet, raw)
    assert caught.value.code == 'evidence_scope'


def test_selection_rejects_generated_quotes_and_metadata(tmp_path):
    _, _, _, packet = inputs(tmp_path)
    with pytest.raises(ValueError):
        resolve_analysis_selection(packet, output(packet))
    raw = selection_output(packet)
    raw['verified'] = True
    with pytest.raises(ValueError):
        resolve_analysis_selection(packet, raw)


def test_no_private_context_still_allows_unclear_public_analysis(tmp_path):
    _, _, _, packet = inputs(tmp_path, context=False)
    raw = selection_output(packet)
    raw['relationship'].update(assessment='unclear', internal_evidence=[])
    raw['implications'] = []
    result = resolve_analysis_selection(packet, raw)
    assert result['relationship']['assessment'] == 'unclear'
    assert all(c['ref_id'].startswith('news:') for c in result['relationship']['evidence'])


def test_number_in_unselected_chunk_does_not_support_a_claim(tmp_path):
    _, _, _, packet = inputs(tmp_path)
    packet['references'][0]['text'] = '合成业务介绍。' * 90 + '合成规模729台。'
    raw = selection_output(packet)
    raw['source_statements'][0]['text'] = '规模729台。'
    with pytest.raises(AnalysisEvidenceError) as caught:
        resolve_analysis_selection(packet, raw)
    assert caught.value.code == 'unsupported_number'


def test_model_wire_selection_is_published_and_revalidated_without_a_second_call(tmp_path, monkeypatch):
    from test_external_analysis_compact import compact_output
    from test_external_analysis_jobs import allow, sample

    from project_watch.dsh_executor import DshProposalModel
    s = sample(tmp_path)
    allow(s)
    calls = []
    model = DshProposalModel(s[4].config, 'synthetic-key')
    # Test fixture consent already uses this receiver identity.
    monkeypatch.setattr(model, 'authorization_id', s[4].authorization_id)
    s[5].get = lambda: model

    def run(evidence, **kw):
        import json
        calls.append(kw)
        kw['before_send'](json.dumps({'model':model.config.model, 'max_tokens':3072, 'stream':True,
                                     'messages':[{'role':'user','content':'synthetic'}]}).encode())
        return compact_output(s[3])

    monkeypatch.setattr(model, '_run', run)
    assert s[7].process_one()['state'] == 'succeeded'
    assert s[7].process_one() is None
    assert len(calls) == 1
    assert len(s[6].read(s[1].id)['items']) == 1


def test_unknown_numbers_keep_evidence_guard_and_have_safe_diagnostics(tmp_path):
    _, _, _, packet = inputs(tmp_path)
    raw = selection_output(packet)
    raw['unknowns'] = ['尚需核对2099年的订单。']
    with pytest.raises(AnalysisEvidenceError) as caught:
        resolve_analysis_selection(packet, raw)
    assert caught.value.code == 'unsupported_number'
    raw['unknowns'] = ['缺口' * 130]
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        resolve_analysis_selection(packet, raw)
    prompt = external_analysis_prompt(packet)
    assert 'NOT the monitored project' in prompt
    assert 'specific transmission mechanism' in prompt
    assert 'numeric rule applies to unknowns' in prompt
