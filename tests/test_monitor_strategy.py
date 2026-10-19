import copy
import json

import pytest

from project_watch.monitor_strategy import strategy_prompt, validate_strategy


def packet():
    return {
        'project_id': 'synthetic-project', 'source_id': 'synthetic-bp',
        'source_sha256': 'a' * 64, 'reference': 'Synthetic BP.pptx',
        'excerpts': [
            {'id': 'p1', 'locator': 'slide 1', 'text': '合成视觉有限公司'},
            {'id': 'p2', 'locator': 'slide 2', 'text': '公司研发工业视觉检测设备，应用于零部件表面缺陷检测。'},
        ],
    }


def output():
    return {
        'understanding': {'text': '公司自述研发工业视觉检测设备。', 'evidence': ['p2']},
        'scopes': [
            {'category': 'company', 'anchor': '合成视觉有限公司',
             'question': '公司是否有新的经营动态？', 'rationale': '关注公司主体的公开变化。', 'evidence': ['p1']},
            {'category': 'sector', 'anchor': '工业视觉检测设备',
             'question': '产品技术及同类厂商有哪些变化？', 'rationale': 'BP列为公司产品。', 'evidence': ['p2']},
        ],
        'unknowns': ['具体同行名称需公开核对。'],
    }


def test_strategy_has_evidence_and_no_preconfigured_keywords():
    result = validate_strategy(packet(), output())
    assert result['scopes'][1]['source_refs'][0]['locator'] == 'slide 2'
    assert result['scopes'][1]['source_refs'][0]['version'] == 'a' * 64
    assert result['formal_facts'] is False
    assert result['public_terms_verified'] is False
    assert result['status'] == 'proposed'


def test_product_scope_is_not_company_but_organization_name_is_preserved():
    p, candidate = packet(), output()
    candidate['scopes'][1]['category'] = 'company'
    result = validate_strategy(p, candidate)
    assert result['scopes'][1]['category'] == 'sector'
    assert result['scopes'][0]['category'] == 'company'
    # Immutable legacy snapshots keep their old classification.
    old = validate_strategy(p, candidate, rule_version='bp-monitor-strategy-v1')
    assert old['scopes'][1]['category'] == 'company'
    assert old['rule_version'] == 'bp-monitor-strategy-v1'


@pytest.mark.parametrize('mutation', ['invented_anchor', 'unknown_ref', 'duplicate', 'empty', 'changed_text'])
def test_bad_strategy_fails_closed(mutation):
    p, candidate = packet(), output()
    if mutation == 'invented_anchor':
        candidate['scopes'][1]['anchor'] = '未出现的秘密客户'
    elif mutation == 'unknown_ref':
        candidate['scopes'][1]['evidence'] = ['foreign-document']
    elif mutation == 'duplicate':
        candidate['scopes'].append(copy.deepcopy(candidate['scopes'][0]))
    elif mutation == 'empty':
        candidate['scopes'] = []
    else:
        p['excerpts'][1]['text'] = '已变更的原文'
    with pytest.raises(ValueError):
        validate_strategy(p, candidate)


def test_strategy_prompt_separates_private_context_and_public_search():
    prompt = strategy_prompt(packet())
    assert 'untrusted data' in prompt
    assert 'customer' in prompt and 'financial' in prompt
    assert 'OUTPUT_SCHEMA' in prompt
    assert '合成视觉有限公司' in prompt
    assert 'selected subset, NOT the whole BP' in prompt
    assert 'extra named companies absent from its anchor' in prompt


def test_unknowns_are_bounded():
    candidate = output()
    candidate['unknowns'] = ['x' * 401]
    with pytest.raises(ValueError):
        validate_strategy(packet(), candidate)


@pytest.mark.parametrize('phrase', ['国内工业视觉检测设备市场', '国内中国工业视觉检测设备行业市场'])
def test_v3_searchable_category_phrase_and_legacy_preservation(phrase):
    from project_watch.monitor_activation import checked_strategy
    p, candidate=packet(),output()
    p['excerpts'][1]['text']=f'公司服务{phrase}。'
    candidate['scopes'][1]['anchor']=phrase
    current=validate_strategy(p,candidate)
    assert current['rule_version']=='bp-monitor-strategy-v3'
    assert current['scopes'][1]['anchor']=='工业视觉检测设备'
    assert checked_strategy(p,current)[1]==current
    old=validate_strategy(p,candidate,rule_version='bp-monitor-strategy-v2')
    assert old['scopes'][1]['anchor']==phrase
    assert checked_strategy(p,old)[1]==old


def test_organization_and_short_ambiguous_phrases_are_not_truncated():
    p,candidate=packet(),output()
    p['excerpts'][0]['text']='中国合成市场有限公司'
    candidate['scopes'][0]['anchor']=p['excerpts'][0]['text']
    assert validate_strategy(p,candidate)['scopes'][0]['anchor']=='中国合成市场有限公司'
    p['excerpts'][1]['text']='公司提供国内市场服务。'
    candidate['scopes'][1]['anchor']='国内市场'
    assert validate_strategy(p,candidate)['scopes'][1]['anchor']=='国内市场'


def test_product_reclassification_and_normalization_are_idempotent():
    from project_watch.monitor_activation import checked_strategy
    p,candidate=packet(),output()
    p['excerpts'][1]['text']='国内工业视觉检测设备'
    candidate['scopes'][1].update(category='company',anchor=p['excerpts'][1]['text'])
    result=validate_strategy(p,candidate)
    assert result['scopes'][1]['category']=='sector'
    assert result['scopes'][1]['anchor']=='工业视觉检测设备'
    assert checked_strategy(p,result)[1]==result


def test_v3_prompt_covers_generic_products_and_each_named_peer():
    prompt=strategy_prompt(packet())
    assert 'BROAD' in prompt and 'SPECIALIST' in prompt
    assert 'each explicitly named peer' in prompt


def test_adapter_preserves_gate_failure_and_releases_lock(monkeypatch):
    from project_watch.dsh_executor import DshProposalModel
    from project_watch.executors import ExecutorUnavailable
    from project_watch.model_provider import ModelProviderConfig
    model = DshProposalModel(ModelProviderConfig('https://example.test', 'anthropic-messages', 'test'), 'secret', max_runs=1)
    def denied(_):
        raise ValueError('private denial details')
    def run(evidence, **kwargs):
        kwargs['before_send']({})
        pytest.fail('must not continue after consent denial')
    monkeypatch.setattr(model, '_run', run)
    with pytest.raises(ExecutorUnavailable, match=r'^DSH monitor strategy failed$'):
        model.plan_monitoring(packet(), before_send=denied)
    assert model._lock.acquire(blocking=False)
    model._lock.release()
    with pytest.raises(ExecutorUnavailable, match='model process quota reached'):
        model.plan_monitoring(packet(), before_send=lambda _: None)


def test_adapter_requires_gate_and_freezes_source(monkeypatch):
    from project_watch.dsh_executor import DshProposalModel
    from project_watch.executors import ExecutorUnavailable
    from project_watch.model_provider import ModelProviderConfig
    model = DshProposalModel(ModelProviderConfig('https://example.test', 'anthropic-messages', 'test'), 'secret')
    p, calls = packet(), []
    def run(evidence, **kwargs):
        calls.append(kwargs)
        p['excerpts'][1]['text'] = 'caller-mutated'
        return output()
    monkeypatch.setattr(model, '_run', run)
    with pytest.raises(ExecutorUnavailable):
        model.plan_monitoring(p, before_send=None)
    assert calls == []
    result = model.plan_monitoring(p, before_send=lambda _: None)
    assert result['scopes'][1]['anchor'] == '工业视觉检测设备'
    assert calls[0]['max_output_tokens'] == 8192
    assert 'secret' not in json.dumps(result)


def test_operator_probe_defaults_to_no_network(monkeypatch, capsys):
    from scripts import monitor_strategy_probe as probe
    monkeypatch.setattr(probe, 'DshProposalModel', lambda *a, **kw: pytest.fail('unexpected model'))
    assert probe.main([]) == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'preview_no_network'


def test_operator_probe_refuses_missing_packet_approval(monkeypatch, capsys, tmp_path):
    import io

    from scripts import monitor_strategy_probe as probe
    body = {'provider': {'base_url': 'https://example.test', 'protocol': 'anthropic-messages', 'model': 'test'},
            'api_key': 'DO-NOT-ECHO', 'packet': packet(), 'approved_packet_digest': 'wrong', 'consent': True}
    monkeypatch.setattr(probe.sys, 'stdin', io.StringIO(json.dumps(body)))
    assert probe.main(['--run', '--receipt', str(tmp_path / 'private.json')]) == 2
    result = capsys.readouterr().out
    assert 'DO-NOT-ECHO' not in result
    assert json.loads(result)['code'] == 'explicit packet approval required'


def test_unknown_extra_data_and_duplicate_ids_rejected():
    p = packet()
    p['api_key'] = 'must-not-enter-model-packet'
    with pytest.raises(ValueError):
        strategy_prompt(p)
    p = packet()
    p['excerpts'].append(p['excerpts'][0])
    with pytest.raises(ValueError):
        strategy_prompt(p)
