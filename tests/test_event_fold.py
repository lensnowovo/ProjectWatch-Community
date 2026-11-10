from contextlib import closing
from types import SimpleNamespace

import pytest
import test_collection_view as fixtures
from test_studio_event_context import _event

from project_watch.studio import event_fold
from project_watch.studio import events as ev


def _item(i, title, summary='', published='2026-07-06T09:00:00+00:00', category='company'):
    return {'id': i, 'title': title, 'summary': summary, 'category': category, 'importance': 'major',
            'published_at': published, 'collected_at': '2026-11-02T00:00:00+00:00'}


def test_plan_wording_in_prose_is_not_a_forecast_conflict():
    reported = '华源智因完成数千万种子轮融资 水木创投领投，用于数据、模型和临床建设。'
    planned = '华源智因完成数千万元种子轮融资 资金计划用于数据、模型和临床三大壁垒建设。'
    assert not ev.conflicting(reported, planned)
    # Figures still keep forecast and reported apart.
    assert ev.conflicting('甲公司2026年Q3销量1万台', '甲公司预计2026年Q3销量1万台')


def test_actor_identity_does_not_guess_from_suffixes():
    assert not ev.same_actor('虚拟细胞企业华源智因', '华源智因')
    assert not ev.same_actor('新星海', '星海')
    assert ev.same_actor('星海', '星海')
    assert not ev.same_actor('华源智因', '百曜科技')
    assert ev.conflicting('新星海完成种子轮融资', '星海完成种子轮融资')


def test_one_named_round_merges_across_differently_worded_headlines():
    items = [_item('a', '华源智因完成数千万种子轮融资，水木创投投资'),
             _item('b', '华源智因宣布获种子轮投资 打造生命反应OS', published='2026-07-07T03:00:00+00:00')]
    assert ev.similarity(items[0]['title'], items[1]['title']) < ev.MERGE_SIMILARITY
    groups, _ = ev.plan_groups(items, [])
    assert [len(g.items) for g in groups] == [2]


@pytest.mark.parametrize('other', [
    '华源智因完成天使轮融资',                  # another round
    '百曜科技完成种子轮融资',                  # another company
    '华源智因完成种子轮融资5000万元',          # a different amount
])
def test_financing_key_needs_the_same_company_and_round(other):
    a = '华源智因完成种子轮融资3000万元'
    merged = len(ev.plan_groups([_item('a', a), _item('b', other)], [])[0]) == 1
    assert merged is False


def test_ambiguous_pairs_also_come_from_alike_summaries():
    lead = _item('a', '华源智因完成数千万种子轮融资', '华源智因完成数千万元种子轮融资，水木创投领投，用于数据和模型。')
    odd = _item('b', '用AI复刻人类细胞，这家公司拿到首笔钱', '华源智因完成数千万元种子轮融资，水木创投领投，用于数据和临床。')
    groups, ambiguous = ev.plan_groups([lead, odd], [])
    assert len(groups) == 2 and [i['id'] for i, _ in ambiguous] == ['b']


def test_plan_moves_a_whole_confirmed_group():
    rows = [{'id': 'a', 'title': '华源智因完成数千万种子轮融资', 'summary': '华源智因完成数千万元种子轮融资，水木创投领投。',
             'category': 'company', 'importance': 'major', 'event_at': '2026-07-06', 'created_at': '2026-10-28'},
            {'id': 'b', 'title': '「华源智因」获千万级人民币种子轮融资｜36氪首发', 'summary': '华源智因完成数千万元种子轮融资，水木创投领投。',
             'category': 'company', 'importance': 'routine', 'event_at': '2026-07-06', 'created_at': '2026-10-29'},
            {'id': 'c', 'title': '「华源智因」获千万级人民币种子轮融资｜36氪首发-36氪', 'summary': '同上',
             'category': 'company', 'importance': 'major', 'event_at': '2026-07-06', 'created_at': '2026-11-02'}]
    assert event_fold.plan(rows) == [['b', 'c']] or event_fold.plan(rows) == [['a', 'b', 'c']]
    assert event_fold.plan(rows, confirm=lambda pairs: set(range(len(pairs)))) == [['a', 'b', 'c']]


@pytest.fixture
def owner(tmp_path):
    return fixtures.owner.__wrapped__(tmp_path)


def test_fold_merges_once_with_a_receipt(owner):
    pipe, _, pid, _ = owner
    execution, _, _, _ = fixtures.save(owner)
    _, a = _event(pipe.store, pid, '合成甲完成种子轮融资', 'x')
    _, b = _event(pipe.store, pid, '合成甲完成种子轮融资 - 来源二', 'y')
    _, c = _event(pipe.store, pid, '合成乙发布新品', 'z')
    llm = SimpleNamespace(ready=False)
    assert event_fold.fold(pipe, execution, pid, llm) == 1
    visible = {e['id'] for e in pipe.store.list_events(project_id=pid)}
    assert visible == {a, c} or visible == {b, c}
    assert event_fold.fold(pipe, execution, pid, llm) == 0  # once per project
    with closing(pipe.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_event_merge_receipts WHERE project_id=?', (pid,)).fetchone()[0] == 1


def test_old_news_reported_again_is_compared_with_its_event(owner):
    pipe, _, pid, _ = owner
    _, event = _event(pipe.store, pid, '合成甲完成种子轮融资', 'x')
    with closing(pipe.store._connect()) as db, db:
        db.execute("UPDATE studio_events SET event_at='2026-07-06T00:00:00+00:00' WHERE id=?", (event,))
    assert pipe.store.recent_events(pid) == []
    since = ev.earliest_window([{'published_at': '2026-07-06T09:00:00+00:00'}, {'published_at': None}])
    assert since.startswith('2026-07-03')
    assert [e['id'] for e in pipe.store.recent_events(pid, since=since)] == [event]


def test_suffix_company_names_do_not_merge():
    groups, _ = ev.plan_groups([_item('a', '新星海完成A轮融资'), _item('b', '星海完成A轮融资')], [])
    assert len(groups) == 2


def test_model_boolean_alone_cannot_merge_related_events():
    from project_watch.studio.pipeline import Pipeline

    a = _item('a', '合成甲公布产品研究数据')
    group = ev.Group(category='company', titles=['合成甲股价下跌'], when=None)
    llm = SimpleNamespace(json=lambda *a, **kw: {'results': [{'pair': 1, 'same': True}]})
    assert not Pipeline.same_event_pairs(llm, [(a, group)])
    result = {'actor': '合成甲', 'action': '公布研究数据',
              'quote_a': '合成甲公布研究数据', 'quote_b': '合成甲公布研究数据'}
    assert not ev.confirmation_supported(result, '合成甲公布研究数据', '合成甲股价下跌，研究数据引发关注')
    assert ev.confirmation_supported(result, '合成甲公布研究数据', '最新：合成甲公布研究数据')


def test_model_error_does_not_start_later_batches():
    from project_watch.studio.llm import ModelError
    from project_watch.studio.pipeline import Pipeline

    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise ModelError('synthetic unknown', code='request_unknown')
    group = ev.Group(category='company', titles=['合成甲发布研究报告'], when=None)
    with pytest.raises(ModelError):
        Pipeline.same_event_pairs(SimpleNamespace(json=fail), [(_item('a', '合成甲研究报告'), group)] * 21)
    assert len(calls) == 1


def test_incomplete_model_response_is_not_complete():
    from project_watch.studio.pipeline import Pipeline

    group = ev.Group(category='company', titles=['合成甲发布研究报告'], when=None)
    pairs = [(_item('a', '合成甲研究报告'), group)]
    llm = SimpleNamespace(json=lambda *a, **kw: {'results': []})
    assert not Pipeline.same_event_pairs(llm, pairs).complete
    assert not Pipeline.same_event_pairs(llm, pairs, max_batches=0).complete


def test_fold_processes_more_than_twenty_members(owner, monkeypatch):
    pipe, _, pid, _ = owner
    execution, _, _, _ = fixtures.save(owner)
    ids = [_event(pipe.store, pid, f'合成事件来源{i}', str(i))[1] for i in range(24)]
    monkeypatch.setattr(event_fold, 'plan', lambda *a, **kw: [ids])
    assert event_fold.fold(pipe, execution, pid, None) == 23
    assert len(pipe.store.list_events(project_id=pid)) == 1
    with closing(pipe.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_event_merge_receipts WHERE project_id=?', (pid,)).fetchone()[0] == 2
        receipts = [r[0] for r in db.execute('SELECT id FROM studio_event_merge_receipts WHERE project_id=? ORDER BY rowid DESC', (pid,))]
    for receipt in receipts:
        pipe.store.event_revisions.undo_merge(execution, receipt)
    assert len(pipe.store.list_events(project_id=pid)) == 24


def test_failed_merge_does_not_mark_fold_complete(owner, monkeypatch):
    pipe, _, pid, _ = owner
    execution, _, _, _ = fixtures.save(owner)
    ids = [_event(pipe.store, pid, f'合成事件来源{i}', str(i))[1] for i in range(2)]
    monkeypatch.setattr(event_fold, 'plan', lambda *a, **kw: [ids])
    original = pipe.store.event_revisions.merge
    def fail(*args):
        raise ValueError('synthetic stale revision')
    monkeypatch.setattr(pipe.store.event_revisions, 'merge', fail)
    assert event_fold.fold(pipe, execution, pid, None) == 0
    assert pipe.store.get_state(event_fold.KEY.format(pid)) is None
    monkeypatch.setattr(pipe.store.event_revisions, 'merge', original)
    assert event_fold.fold(pipe, execution, pid, None) == 1
    assert pipe.store.get_state(event_fold.KEY.format(pid)) == '1'
