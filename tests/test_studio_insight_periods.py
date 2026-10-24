"""Synthetic temporal regressions. No providers or live data used."""
from contextlib import closing

from test_studio_insight import _setup, _triage

from project_watch.studio import insight_generation as generation


def test_public_report_context_survives_summary_and_keeps_dates_distinct(tmp_path):
    project, store, pipeline = _setup(tmp_path)
    store.add_items(project.id, 'company', [{
        'key': 'report', 'title': '合成公司十月更新招股书', 'url': 'https://example.invalid/report',
        'published_at': '2026-10-13', 'snippet': '2026年上半年产品收入为零。', 'source': '合成公告',
    }])
    _triage(store)
    body = '合成公司公告。' * 300 + '招股书显示，2026年上半年公司营收4000万元。产品收入为零。'
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_items SET summary=?,body=?,body_status='read' WHERE key='report'",
                   ('产品收入仍为零', body))
        item_id = db.execute("SELECT id FROM studio_items WHERE key='report'").fetchone()[0]
    store.create_event(project.id, {'title': '合成公司十月更新招股书', 'summary': '产品收入仍为零',
                                   'category': 'company', 'importance': 'normal'}, [item_id])
    gathered = generation.gather(pipeline, project.id)
    entry = next(e for e in gathered['numbered'].values() if e.get('url') == 'https://example.invalid/report')
    assert entry['published_at'] == '2026-10-13'
    assert entry['report_period'] == ''  # Do not guess which date belongs to which fact.
    assert '2026年上半年' in entry['context']
    assert '原文摘录' in gathered['text'] and '发布日期：2026-10-13' in gathered['text']
    assert '合成公司十月更新招股书' in gathered['text']
    assert generation.gather(pipeline, project.id)['fingerprint'] == gathered['fingerprint']
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_items SET body=replace(body,'2026年上半年','2025年度') WHERE key='report'")
    assert generation.gather(pipeline, project.id)['fingerprint'] != gathered['fingerprint']


def test_collection_time_is_never_the_report_or_event_date():
    entry = generation._public_entry({'title': '合成历史资料', 'collected_at': '2026-10-20'}, '公司新闻')
    assert entry['date'] == entry['published_at'] == entry['event_at'] == entry['report_period'] == ''
    assert entry['collected_at'] == '2026-10-20'
    assert '未注明' in generation._source_line('N1', entry)


def test_unknown_period_and_forecast_are_not_inferred_from_publication():
    entry = generation._public_entry({'title': '合成公告', 'published_at': '2026-10-20',
                                     'body': '预计明年收入增加。报告期内产品收入为零。'}, '公司新闻')
    assert entry['report_period'] == ''
    assert '预计明年' in entry['context'] and '报告期内' in entry['context']
    assert '预测' in generation.SYSTEM and '销售' in generation.SYSTEM and '报告期' in generation.SYSTEM


def test_undated_zero_income_is_not_republished_as_current_judgment():
    numbered = {'N1': generation._public_entry({'title': '合成报告', 'published_at': '2026-10-13',
                    'body': '2026年上半年公司营收4000万元。植入式产品收入为零。'}, '公司新闻'),
                'N2': generation._public_entry({'title': '合成新进展', 'published_at': '2026-10-15',
                    'summary': '2026年9月已销售13套产品，未披露收入确认。'}, '公司新闻')}
    out = generation.normalize({'headline': '植入式收入仍为零', 'operations': {
        'verdict': 'pressured', 'summary': '已售13套，但收入仍为零', 'confidence': 'high',
        'for': [{'text': '2026年9月已销售13套产品', 'refs': ['N2']}],
        'against': [{'text': '植入式产品收入仍为零', 'refs': ['N1']}] }}, numbered)
    assert not out['operations']['against']
    assert out['operations']['verdict'] == 'unclear'
    assert out['operations']['summary'] == '' and out['headline'] == ''
    assert out['operations']['for'][0]['text'] == '2026年9月已销售13套产品'
    assert any('报告期' in g for g in out['gaps'])
    qualified = generation.normalize({'operations': {'verdict': 'pressured', 'against': [
        {'text': '2026年上半年植入式产品收入为零，后续收入未披露', 'refs': ['N1']}
    ]}}, numbered)
    assert len(qualified['operations']['against']) == 1
    assert qualified['operations']['against'][0]['refs'][0]['published_at'] == '2026-10-13'


def test_period_qualification_must_be_in_the_same_clause():
    assert generation._undated_zero_income('2026年9月售出13套，但产品收入仍为零')
    assert generation._undated_zero_income('2026年9月售出13套，产品收入仍为零')
    assert not generation._undated_zero_income('2026年上半年产品收入为零；9月销售13套')
    assert not generation._undated_zero_income('2026 年上半年，产品收入为零')


def test_body_lookup_is_project_scoped(tmp_path):
    project, store, _pipeline = _setup(tmp_path)
    # A stale/corrupt event source must not expose another project's body.
    with closing(store._connect()) as db:
        item_id = db.execute("SELECT id FROM studio_items WHERE key='c1'").fetchone()[0]
    assert generation._source_bodies(store, project.id, [{'id': item_id}])
    assert generation._source_bodies(store, project.id + '-other', [{'id': item_id}]) == {}
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_items SET status='dismissed' WHERE id=?", (item_id,))
    assert generation._source_bodies(store, project.id, [{'id': item_id}]) == {}
