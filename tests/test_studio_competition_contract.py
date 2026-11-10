"""Competition signals: what changes for us, with a source. Synthetic data only."""
from project_watch.studio import insight_generation as gen
from project_watch.studio import judgment_review as review
from project_watch.studio import section_analysis


def source(body, **extra):
    return {'kind': 'public', 'text': body, 'context': body, 'context_kind': '原文摘录',
            'url': 'https://example.invalid/report', **extra}


SOURCES = {
    'C1': source('2026年9月，山海医疗获Ⅲ类医疗器械注册证。'),
    'C2': source('2026年8月，阶梯医疗完成B轮融资5亿元。'),
    'N1': source('2026年3月，本公司产品获批上市。'),
}
SIGNALS = [
    {'peer': '山海医疗', 'event': '2026年9月获Ⅲ类证', 'impact': '国内首家获批被追平', 'refs': ['C1']},
    {'peer': '阶梯医疗', 'event': '2026年8月完成B轮5亿', 'impact': '融资节奏快于本公司', 'refs': ['C2']},
]


def normalize(signals=None, window=None, peers=('山海医疗', '阶梯医疗')):
    part = {'verdict': 'ahead', 'confidence': 'medium', 'summary': '本公司先获批，窗口期正在收窄。',
            'peers': list(peers), 'for': [{'text': '2026年3月，本公司产品获批上市', 'refs': ['N1']}],
            'against': [], 'change_if': []}
    if signals is not None:
        part['competition_signals'] = signals
    if window is not None:
        part['competition_window'] = window
    return gen.normalize({'headline': '先获批，窗口期收窄', 'position': part}, SOURCES)['position']


def test_signals_and_window_survive_normalization():
    p = normalize(SIGNALS, {'months': 12, 'basis': '3 家同类进入注册临床'})
    assert p['competition_status'] == 'produced'
    assert [(s['peer'], s['event'], s['impact'], [r['code'] for r in s['refs']]) for s in p['competition_signals']] == [
        ('山海医疗', '2026年9月获Ⅲ类证', '国内首家获批被追平', ['C1']),
        ('阶梯医疗', '2026年8月完成B轮5亿', '融资节奏快于本公司', ['C2'])]
    assert p['competition_window'] == {'months': 12, 'basis': '3 家同类进入注册临床'}
    assert p['verdict'] == 'ahead'


def test_signal_without_impact_is_dropped_not_shown():
    p = normalize([{**SIGNALS[0], 'impact': ''}])
    assert 'competition_signals' not in p
    assert p['competition_status'] == 'empty'
    assert p['summary'] and p['verdict'] == 'ahead'


def test_signal_without_source_is_dropped():
    p = normalize([{**SIGNALS[0], 'refs': []}])
    assert 'competition_signals' not in p


def test_unknown_peer_is_dropped_unless_the_source_names_it():
    p = normalize([{**SIGNALS[0], 'peer': '未登记公司'}])
    assert 'competition_signals' not in p
    # An unlisted peer is still admissible when a cited source names it: the quote is the bar.
    p = normalize([{**SIGNALS[0], 'peer': '山海医疗'}], peers=())
    assert p['competition_signals'][0]['peer'] == '山海医疗'


def test_window_without_basis_is_removed_entirely():
    """The model returned the key, so the reason is 'nothing usable', not 'not attempted'."""
    p = normalize(None, {'months': 12, 'basis': ''})
    assert 'competition_window' not in p
    assert p['competition_status'] == 'empty'


def test_omitted_when_the_model_skips_the_fields():
    p = normalize(None)
    assert p['competition_status'] == 'omitted'


def test_month_count_out_of_range_keeps_the_qualitative_basis():
    p = normalize(None, {'months': 999, 'basis': '3 家同类进入注册临床'})
    assert p['competition_window'] == {'basis': '3 家同类进入注册临床'}


def test_review_budget_stays_within_two_batches():
    p = normalize(SIGNALS, {'months': 12, 'basis': '3 家同类进入注册临床'})
    payload = review.review_input({'position': p}, {'numbered': SOURCES})
    payload['fields'] = {k: v for k, v in payload['fields'].items() if section_analysis._reviewed(k)}
    assert not any(path.split('.')[-1] == 'peer' for path in payload['fields'])
    assert len(payload['fields']) <= 19
    assert len(review.review_batches(payload)) <= 2


def test_one_bad_signal_does_not_take_the_others_down():
    p = normalize(SIGNALS + [{**SIGNALS[0], 'impact': ''}])
    assert len(p['competition_signals']) == 2
    trimmed, dropped, blocking = section_analysis.trim(
        p, [{'field': 'position.competition_signals.0.impact', 'reason': 'unsupported'}])
    assert trimmed is not None and not blocking
    assert [s['peer'] for s in trimmed['competition_signals']] == ['阶梯医疗']
    assert trimmed['summary'] == p['summary']
