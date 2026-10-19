"""A smaller model wire shape must preserve all publication evidence guards."""
from copy import deepcopy

import pytest
from test_external_analysis import inputs, output

from project_watch.external_analysis import AnalysisEvidenceError, validate_external_analysis
from project_watch.external_analysis_compact import compact_prompt, resolve_compact_analysis
from project_watch.external_analysis_selection import evidence_catalog


def compact_output(packet):
    raw = output(packet)
    catalog = evidence_catalog(packet)
    for item in [*raw['source_statements'], raw['relationship'], *raw['implications']]:
        item['evidence'] = [next(c['id'] for c in catalog if c['ref_id'] == cite['ref_id'])
                            for cite in item['evidence']]
    return raw


def test_compact_preserves_saved_contract_and_does_not_mutate_inputs(tmp_path):
    _, _, _, packet = inputs(tmp_path)
    raw = compact_output(packet)
    before = deepcopy((packet, raw))
    assert resolve_compact_analysis(packet, raw) == validate_external_analysis(packet, output(packet))
    assert (packet, raw) == before


@pytest.mark.parametrize('mutation,code', [
    ('number', 'unsupported_number'), ('unselected_number', 'unsupported_number'),
    ('private_fact', 'evidence_scope'), ('no_internal', 'evidence_scope'),
    ('unknown_id', 'citation_mismatch'), ('unknown_number', 'unsupported_number'),
    ('impact_not_relevant', 'unsupported_impact'),
])
def test_guards_still_fail_closed(tmp_path, mutation, code):
    _, _, _, packet = inputs(tmp_path)
    packet['references'][-1]['text'] += '合成设备24台。'
    raw = compact_output(packet)
    if mutation == 'number':
        raw['source_statements'][0]['text'] = '收入增长999%。'
    elif mutation == 'unselected_number':
        raw['source_statements'][0]['text'] = '设备24台。'
    elif mutation == 'private_fact':
        raw['source_statements'][0]['evidence'] = raw['relationship']['evidence'][-1:]
    elif mutation == 'no_internal':
        raw['relationship']['evidence'] = raw['source_statements'][0]['evidence']
    elif mutation == 'unknown_id':
        raw['relationship']['evidence'] = ['missing']
    elif mutation == 'unknown_number':
        raw['unknowns'] = ['待核实2099年的订单。']
    else:
        raw['relationship']['assessment'] = 'not_relevant'
    with pytest.raises(AnalysisEvidenceError) as caught:
        resolve_compact_analysis(packet, raw)
    assert caught.value.code == code


@pytest.mark.parametrize('mutation', ['extra_text', 'metadata', 'quotes', 'too_many', 'blank'])
def test_malformed_wire_is_not_repaired(tmp_path, mutation):
    _, _, _, packet = inputs(tmp_path)
    raw = compact_output(packet)
    if mutation == 'extra_text':
        raw['implications'][0]['text'] = '不能静默删除这个字段'
    elif mutation == 'metadata':
        raw['verified'] = True
    elif mutation == 'quotes':
        raw['source_statements'][0]['evidence'] = output(packet)['source_statements'][0]['evidence']
    elif mutation == 'too_many':
        raw['implications'] *= 2
    else:
        raw['implications'][0]['condition'] = ' '
    with pytest.raises(ValueError):
        resolve_compact_analysis(packet, raw)


def test_no_internal_context_and_inline_bounded_schema(tmp_path):
    _, _, _, packet = inputs(tmp_path, context=False)
    raw = compact_output(packet)
    raw['relationship']['assessment'] = 'unclear'
    raw['implications'] = []
    assert resolve_compact_analysis(packet, raw)['relationship_verified'] is False
    prompt = compact_prompt(packet)
    assert '$ref' not in prompt and '$defs' not in prompt
    assert 'external_evidence' not in prompt and 'internal_evidence' not in prompt
    assert len(prompt) < 6000
    assert 'untrusted data' in prompt


def test_prompt_contains_only_authorized_catalog_text(tmp_path):
    _, _, _, packet = inputs(tmp_path)
    prompt = compact_prompt(packet)
    for c in evidence_catalog(packet):
        assert c['text'] in prompt
    assert packet['project_id'] not in prompt
    assert packet['source_url'] not in prompt


def test_article_evidence_resolves_without_expanding_private_scope(tmp_path):
    from test_public_article_store import Reader

    from project_watch.external_analysis import build_external_packet
    from project_watch.public_article_store import ArticleStore
    repo, project, _, legacy = inputs(tmp_path)
    ArticleStore(repo).read_once(project.id, legacy['signal_id'], Reader())
    spans = [dict(source_id=r['source_id'], ordinal=r['ordinal'], offset=r['offset'], length=len(r['text']))
             for r in legacy['references'] if r['kind'] == 'internal']
    packet = build_external_packet(repo, project.id, legacy['signal_id'], selections=spans, read_articles=True)
    before = deepcopy(packet)
    catalog = evidence_catalog(packet)
    body = next(c for c in catalog if c['ref_id'].startswith('article:'))
    raw = compact_output(packet)
    raw['source_statements'][0] = dict(text='公开正文提到产品情况，仍需核实。', evidence=[body['id']])
    result = resolve_compact_analysis(packet, raw)
    assert result['source_statements'][0]['evidence'] == [dict(ref_id=body['ref_id'], quote=body['text'])]
    assert result['rule_version'] == 'external-analysis-v2'
    assert result['content_scope'] == 'article_excerpts'
    assert 'Saved public article excerpts' in compact_prompt(packet)
    assert packet == before
    assert [r for r in packet['references'] if r['kind'] == 'internal'] == [
        r for r in legacy['references'] if r['kind'] == 'internal']
