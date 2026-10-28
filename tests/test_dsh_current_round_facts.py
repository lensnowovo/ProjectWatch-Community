"""Same-round public facts must survive a stale profile projection, without bypassing evidence gates."""
from copy import copy, deepcopy

import pytest
import test_studio_dsh_monitor as fixtures

from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart


@pytest.fixture
def loop(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path)


def fact(loop, text, *, key='synthetic:new', visibility='public', project_id=None):
    version = loop.store.evidence.save(EvidenceInput(project_id=project_id or loop.project_id,
        source_key=key, source_type='public_page', visibility=visibility, read_scope='full',
        extraction_version='synthetic', parts=[EvidencePart(locator_kind='stored_text', locator=key, text=text)]))
    ref = dict(evidence_version_id=version['id'], part_index=0, start=0, end=len(text), quote=text,
               url='https://example.com/current', title='Synthetic', source='Synthetic', published_at='', read_scope='full')
    return dict(key=key, kind='business', label='合成经营资料', text=text, date='', refs=[ref])


def stale_profile(loop):
    fixtures.collect(loop)
    old = fact(loop, '合成公司原有产品资料。', key='synthetic:old')
    loop.store.put_compiled_profile(loop.project_id, {'facts':[old]}, [old['refs'][0]['evidence_version_id']],
        expected_generation=loop.store.evidence.dependencies.generation(loop.project_id), public=True)
    fact(loop, '合成公司原来源内容已经更新。', key='synthetic:old')
    return old


def test_new_facts_persist_while_old_versions_remain_historical(loop):
    old = stale_profile(loop)
    new = fact(loop, '合成公司本轮公开产品取得许可，量产仍在计划中。')
    loop.state['records'].append(new)
    ctx = loop.tool('pw_context', {})
    profile = loop.store.get_public_profile(loop.project_id)
    assert old in profile['historical_facts']
    assert all(f['text'] != old['text'] for f in profile['facts'])
    assert any(f['text'] == new['text'] for f in profile['facts'])
    assert profile['dependency']['status'] == 'current'
    entries = [(k,v) for k,v in ctx['numbered'].items() if v['text'] == new['text']]
    assert len(entries) == 1
    ref, entry = entries[0]
    assert ref.startswith('W') and entry['evidence_version_id'] == new['refs'][0]['evidence_version_id']
    assert all(v['text'] != old['text'] for v in ctx['numbered'].values())
    assert ctx['profile_projection']['status'] == 'current'
    assert ref not in ctx['watch_evidence'][loop.watches()[0]['id']]
    fixtures.judge(loop, ref)
    loop.tool('pw_revise', {'changes_json':'[]', 'reason':'保留已有监控范围'})
    finished = loop.tool('pw_finish', {'reason':'合成轮次结束'})
    assert finished['profile_projection']['status'] == 'current'
    assert not finished['new_evidence_after_judgment']


@pytest.mark.parametrize('invalid', ['withdrawn', 'superseded', 'private', 'other_project', 'offset'])
def test_current_round_context_does_not_bypass_source_scope_or_versions(loop, invalid):
    options = {}
    if invalid == 'private':
        options['visibility'] = 'internal'
    if invalid == 'other_project':
        options['project_id'] = loop.pipeline.repository.create_project('另一合成公司').id
    new = fact(loop, '合成公司本轮公开资料。', **options)
    if invalid == 'withdrawn':
        loop.store.evidence.dependencies.withdraw(loop.project_id, new['refs'][0]['evidence_version_id'],
                                                  'synthetic withdrawal', actor='user')
    if invalid == 'superseded':
        fact(loop, '合成来源已经更正。')
    if invalid == 'offset':
        new['refs'][0]['quote'] = '不存在的摘录'
    loop.state['records'].append(new)
    # Pure context construction does not project or mutate intentionally invalid fixture records.
    assert not any(e['text'] == new['text'] for e in loop.context()['numbered'].values())


def test_withdrawal_after_context_invalidates_frozen_judgment(loop):
    stale_profile(loop)
    new = fact(loop, '合成公司新资料。')
    loop.state['records'].append(new)
    ctx = loop.tool('pw_context', {})
    assert any(v['text'] == new['text'] for v in ctx['numbered'].values())
    loop.store.evidence.dependencies.withdraw(loop.project_id, new['refs'][0]['evidence_version_id'],
                                              'synthetic withdrawal', actor='user')
    with pytest.raises(ValueError, match='inputs changed'):
        loop.frozen()


def test_current_fact_deduplicates_profile_and_round_copy(loop):
    new = fact(loop, '合成公司公开资料。')
    loop.state['records'].append(deepcopy(new))
    loop.store.put_compiled_profile(loop.project_id, {'facts':[new]}, [new['refs'][0]['evidence_version_id']],
        expected_generation=loop.store.evidence.dependencies.generation(loop.project_id), public=True)
    assert sum(v['text'] == new['text'] for v in loop.context()['numbered'].values()) == 1


def test_current_fact_survives_the_next_round_without_recollection(loop):
    old = stale_profile(loop)
    new = fact(loop, '合成公司本轮新增资料应供下一轮复用。')
    loop.state['records'].append(new)
    loop.tool('pw_context', {})
    next_round = copy(loop)
    next_round.state = {'records': []}
    assert any(e['text'] == new['text'] for e in next_round.public_fact_entries())
    profile = loop.store.get_public_profile(loop.project_id)
    assert old in profile['historical_facts']
    assert all(f['text'] != old['text'] for f in profile['facts'])
    assert profile['dependency']['status'] == 'current'


def test_refreshed_quote_with_same_fact_text_replaces_stale_reference(loop):
    old = stale_profile(loop)
    new = fact(loop, old['text'], key='synthetic:refreshed')
    loop.state['records'].append(new)
    loop.tool('pw_context', {})
    profile = loop.store.get_public_profile(loop.project_id)
    saved = [f for f in profile['facts'] if f['text'] == old['text']]
    assert len(saved) == 1 and saved[0]['refs'] == new['refs']
    assert old in profile['historical_facts']


def test_source_change_during_projection_keeps_atomic_fence(loop, monkeypatch):
    old = stale_profile(loop)
    new = fact(loop, '合成公司有效资料。')
    loop.state['records'].append(new)
    original = loop.store.put_compiled_profile

    def race(*args, **kwargs):
        fact(loop, '合成公司来源在写入前更正。')
        return original(*args, **kwargs)

    monkeypatch.setattr(loop.store, 'put_compiled_profile', race)
    ctx = loop.tool('pw_context', {})
    assert loop.store.get_public_profile(loop.project_id)['facts'] == [old]
    assert ctx['profile_projection']['status'] == 'stale'
    assert all(e['text'] != new['text'] for e in ctx['numbered'].values())
    ref = next(k for k in ctx['numbered'] if not k.startswith('W'))
    fixtures.judge(loop, ref)
    loop.tool('pw_revise', {'changes_json': '[]', 'reason': '保留范围'})
    result = loop.tool('pw_finish', {'reason': '合成结束'})
    assert result['status'] == 'partial'
    assert result['profile_projection']['status'] == 'stale'


def test_all_references_must_be_current_without_copying_private_facts_to_history(loop):
    stale_profile(loop)
    new = fact(loop, '合成公司公开资料。')
    private = fact(loop, '私有合成资料不得投影到公开档案。', key='synthetic:private', visibility='internal')
    new['refs'].extend(private['refs'])
    loop.state['records'].append(new)
    ctx = loop.tool('pw_context', {})
    profile = loop.store.get_public_profile(loop.project_id)
    assert all(e['text'] != new['text'] for e in ctx['numbered'].values())
    assert private['refs'][0]['evidence_version_id'] not in str(profile)
    assert ctx['profile_projection']['status'] == 'partial'
    assert ctx['profile_projection']['rejected_count'] == 1


def test_new_reference_does_not_erase_previous_valid_fact_history(loop):
    fixtures.collect(loop)
    old = fact(loop, '合成公司的同一条公开资料。', key='synthetic:previous')
    loop.store.put_compiled_profile(loop.project_id, {'facts': [old]}, [old['refs'][0]['evidence_version_id']],
        expected_generation=loop.store.evidence.dependencies.generation(loop.project_id), public=True)
    new = fact(loop, old['text'])
    loop.state['records'].append(new)
    loop.tool('pw_context', {})
    profile = loop.store.get_public_profile(loop.project_id)
    assert old in profile['historical_facts']
    assert len(profile['facts']) == 1 and profile['facts'][0]['refs'] == new['refs']
