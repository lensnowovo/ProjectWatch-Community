"""Offline regressions for context capacity, fair evidence selection and summaries."""
import json
from contextlib import closing
from unittest.mock import patch

import pytest
import test_studio_dsh_monitor as fixtures


@pytest.fixture
def loop(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path)


def seed(loop, count, *, related=True, body_size=700, date='2026-10-21'):
    word = '激光模组取得许可' if related else '农业产业政策'
    items = [{'key': f'scale-{related}-{i}', 'title': f'{word} {i}',
              'url': f'https://example.com/{related}/{i}', 'source': 'synthetic',
              'snippet': word, 'published_at': date} for i in range(count)]
    loop.store.add_items(loop.project_id, 'company', items)
    with closing(loop.store._connect()) as db, db:
        rows = db.execute('SELECT id,title FROM studio_items WHERE project_id=? AND event_id IS NULL',
                          (loop.project_id,)).fetchall()
        for row in rows:
            loop.store._insert_event(db, loop.project_id,
                {'title': row['title'], 'summary': word, 'category': 'company',
                 'importance': 'routine', 'event_at': date}, [row['id']])
        db.execute('UPDATE studio_items SET body=?,body_status=\'article_body\',relevant=1,triaged=1 WHERE project_id=?',
                   (word + '合成公开材料。' * (body_size // 7), loop.project_id))


def test_old_pending_evidence_survives_unrelated_recent_news(loop):
    watch, ctx, ref = fixtures.collect(loop)
    target = ctx['numbered'][ref]['id']
    seed(loop, 220, related=False)
    ctx = loop.tool('pw_context', {})
    refs = ctx['watch_evidence'][watch['id']]
    assert target in [ctx['numbered'][r]['id'] for r in refs]
    assert not ctx['judge_ready']
    assert len(ctx['event_ids']) == 200


def test_pending_pages_keep_numbers_until_explicit_refresh(loop):
    seed(loop, 205)
    ctx = loop.tool('pw_context', {})
    watch = loop.watches()[0]
    assert ctx['pending_evidence_omitted'] == {watch['id']: 5}
    assert not ctx['batch_complete']
    assert len(ctx['event_ids']) == 200
    # Consume exactly the displayed batch through the ledger, then confirm
    # frozen references don't shift mid-round as pending membership changes.
    snapshot = loop.execution.read_checkpoint('dsh_monitor_pending')[watch['id']]
    from project_watch.studio.questions import Assessment
    for offset in range(0, 200, 10):
        ids = ctx['event_ids'][offset:offset + 10]
        loop.pipeline.questions.record(loop.project_id, watch['id'], 'system',
            Assessment(status='unchanged', note='本批补充公开依据', event_ids=tuple(ids), visibility='public'),
            include_internal=False, consumed_events=tuple(e for e in snapshot if e['id'] in ids))
    assert loop.frozen()['numbered'] == ctx['numbered']
    following = loop.tool('pw_context', {})
    assert following['pending_evidence_omitted'] == {}
    pending_ids = {e['id'] for e in loop.pending_events(watch)}
    assert len(pending_ids) == 5 and pending_ids <= set(following['event_ids'])


def test_large_chinese_context_roundtrips_and_batches_source_reads(loop):
    seed(loop, 200, body_size=1100)
    ledger = loop.pipeline.questions
    with patch.object(ledger, 'public_event_rows', wraps=ledger.public_event_rows) as reads:
        ctx = loop.tool('pw_context', {})
    assert len(ctx['event_ids']) == 200
    assert reads.call_count <= 3
    saved = loop.execution.read_checkpoint('dsh_monitor_context')
    assert saved['numbered'] == ctx['numbered']
    with closing(loop.store._connect()) as db:
        sizes = [len(r[0]) for r in db.execute('SELECT data FROM studio_job_checkpoints')]
    assert max(sizes) <= 1_000_000


def test_chunked_checkpoint_preserves_lease_and_detects_corruption(loop):
    ex = loop.execution
    value = {'text': '中文\\\"\n' * 200000}
    ex.checkpoint_chunked('large-test', value)
    assert ex.read_checkpoint('large-test') == value
    ex.checkpoint_chunked('large-test', {'small': True})
    assert ex.read_checkpoint('large-test') == {'small': True}
    with closing(loop.store._connect()) as db, db:
        row = db.execute('SELECT data FROM studio_job_checkpoints WHERE stage=?', ('large-test',)).fetchone()
        manifest = json.loads(row[0])
        assert db.execute('SELECT count(*) FROM studio_job_checkpoints WHERE stage LIKE ?',
                          (manifest['prefix'] + '%',)).fetchone()[0] == 1
        db.execute('UPDATE studio_job_checkpoints SET data=? WHERE stage=?',
                   (json.dumps('broken'), manifest['prefix'] + '0'))
    with pytest.raises(ValueError, match='checksum'):
        ex.read_checkpoint('large-test')
    loop.store.finish(ex.job['id'], lease_token=ex.job['lease_token'])
    with pytest.raises(RuntimeError, match='lease'):
        ex.checkpoint_chunked('large-test', {'not': 'allowed'})


def test_match_index_is_not_reused_across_tools(loop):
    _, ctx, _ = fixtures.collect(loop)
    relation = loop.plan.add(loop.project_id, {'text': '关注激光模组', 'axis': 'science',
        'scope_kind': 'relation', 'relation_type': 'technology', 'relation_target': '激光模组',
        'queries': ['激光模组'], 'reason': '产品采用激光模组', 'sources': 'https://example.com/route',
        'tier': 'background'}, actor='system', origin='system', visibility='public')
    seed(loop, 1, date='2026-10-22')
    refreshed = loop.tool('pw_context', {})
    assert len(refreshed['event_ids']) > len(ctx['event_ids'])
    assert refreshed['watch_evidence'][relation['id']]
    with closing(loop.store._connect()) as db, db:
        db.execute('UPDATE studio_items SET relevant=0 WHERE project_id=?', (loop.project_id,))
    refreshed = loop.tool('pw_context', {})
    assert not refreshed['watch_evidence'][relation['id']]
    assert loop._source_rows is None


@pytest.mark.parametrize(('claim', 'source', 'supported'), [
    ('2025年9月实施', '2025年8月30日与2026年9月30日实施', False),
    ('2025年9月实施', '2025年9月30日实施', True),
    ('2025-09实施', '2025年9月实施', True),
    ('2025年9月30日实施', '2025年9月实施', False),
    ('2025年9月30日实施', '2025-09-30T00:00:00Z实施', True),
    ('2025年13月实施', '2025年13月实施', False),
])
def test_periods_are_bound(loop, claim, source, supported):
    assert (not loop.pipeline.questions._unsupported_numbers(loop.project_id, claim, [], [source])) is supported


def test_reassessment_uses_latest_effective_classification(loop):
    watch, _, ref = fixtures.collect(loop)
    for status, basis in [('progress', 'object_change'), ('unchanged', 'evidence_only')]:
        loop.tool('pw_assess', {'watch_id': watch['id'], 'refs': [ref], 'status': status,
            'change_basis': basis, 'note': '激光模组取得许可'})
    fixtures.judge(loop, ref)
    loop.tool('pw_revise', {'changes_json': '[]', 'reason': '继续观察'})
    summary = loop.tool('pw_finish', {'reason': '本轮结束'})['round_summary']
    assert summary['object_changes'] == 0 and summary['evidence_only'] == 1
    assert len(loop.state['assessments']) == 2
