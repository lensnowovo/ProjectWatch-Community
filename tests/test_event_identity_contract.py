"""Frozen synthetic identity pairs; no live data or model calls."""
import json
from pathlib import Path

import pytest

from project_watch.studio import events as ev

CASES = json.loads((Path(__file__).parent / 'fixtures/event_identity/pairs.json').read_text(encoding='utf-8'))


def item(title, key):
    return dict(id=key, title=title, summary='', category='company', published_at='2026-07-06', collected_at='2026-11-05')


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['id'])
def test_fixed_identity_pairs(case):
    groups, _ = ev.plan_groups([item(case['a'], 'a'), item(case['b'], 'b')], [])
    assert (len(groups) == 1) == case['same'], case['reason']


def test_corpus_contract():
    assert len(CASES) == 80
    assert sum(c['same'] for c in CASES) == 30


def test_no_transitive_group_admission_from_one_supported_member():
    a = '合成甲公布ZX-101三期研究数据'
    b = '最新：合成甲公布ZX-101三期研究数据'
    reaction = a + '后股价暴跌'
    group = ev.Group(category='company', titles=[a, reaction], when='2026-07-06')
    assert ev.identity_supported(a, b)
    assert not group.admits(item(b, 'b'))
    proof = dict(actor='合成甲', action='公布ZX-101三期研究数据', quote_a=a, quote_b=a)
    assert not ev.identity_supported(a, reaction, proof)
