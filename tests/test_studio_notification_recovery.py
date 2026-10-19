import json

import pytest
from test_studio_digest import WECOM, Bot, _seed

from project_watch.studio.http import Http, HttpError


@pytest.fixture
def app(tmp_path):
    from project_watch.studio.app import create_studio_app
    application = create_studio_app(tmp_path, start_worker=False)
    bot = Bot()
    application.state.studio_pipeline.http_factory = lambda _: Http(bot)
    application.state.bot = bot
    return application


def configure(app):
    pipeline = app.state.studio_pipeline
    pipeline.settings_store.update({'notify': {'kind': 'wecom', 'webhook_url': WECOM, 'instant_major': True, 'weekly': False}})
    return pipeline


def test_ack_survives_crash_before_local_notified_flag(app):
    _seed(app)
    pipeline = configure(app)
    original = pipeline.store.mark_notified
    pipeline.store.mark_notified = lambda _: (_ for _ in ()).throw(KeyboardInterrupt('synthetic after vendor ack'))
    with pytest.raises(KeyboardInterrupt):
        pipeline.notify()
    assert len(app.state.bot.posts) == 1
    pipeline.store.mark_notified = original
    assert pipeline.notify() == []
    assert len(app.state.bot.posts) == 1 and not pipeline.store.pending_major_events()


def test_unknown_delivery_is_held_even_after_rebatch_and_restart(app):
    _seed(app)
    pipeline = configure(app)
    calls = []
    def ambiguous(*args):
        calls.append(1)
        raise HttpError('connection_failed', detail=WECOM)
    pipeline.http_factory = lambda _: Http(ambiguous)
    assert pipeline.notify() == []
    pipeline.store.set_state('notify_retry_after', '2000-01-01')
    assert pipeline.notify() == [] and len(calls) == 1
    _seed(app, 'New Synthetic', title='New Synthetic new event')
    pipeline.store.set_state('notify_retry_after', '2000-01-01')
    pipeline.http_factory = lambda _: Http(app.state.bot)
    assert pipeline.notify() == ['major:1']
    assert len(app.state.bot.posts) == 1
    assert '光启视界' not in app.state.bot.posts[0][1]['markdown']['content']
    assert len(pipeline.store.pending_major_events()) == 1
    with pipeline.store._connect() as db:
        journal = str([tuple(r) for r in db.execute('SELECT * FROM studio_notification_deliveries')])
    assert WECOM not in journal and 'synthetic-key' not in journal and '光启视界' not in journal
    assert 'notify_outcome_unknown' in json.loads(pipeline.store.get_state('notify_last'))['error']


def test_known_rejections_are_bounded(app):
    _seed(app)
    pipeline = configure(app)
    app.state.bot.reply = {'errcode': 93000}
    for _ in range(4):
        pipeline.store.set_state('notify_retry_after', '2000-01-01')
        assert pipeline.notify() == []
    assert len(app.state.bot.posts) == 3


def test_nested_dispatch_cannot_resend_inflight_notification(app):
    _seed(app)
    pipeline = configure(app)
    nested = []
    def transport(*args):
        nested.append(pipeline.notify())
        return app.state.bot(*args)
    pipeline.http_factory = lambda _: Http(transport)
    assert pipeline.notify() == ['major:1']
    assert nested == [[]] and len(app.state.bot.posts) == 1


def test_major_batch_only_marks_members_actually_submitted(app):
    for i in range(11):
        _seed(app, f'Synthetic {i}', title=f'Synthetic {i} funding')
    pipeline = configure(app)
    assert pipeline.notify() == ['major:10']
    assert len(pipeline.store.pending_major_events()) == 1
    assert pipeline.notify() == ['major:1']
    assert not pipeline.store.pending_major_events()
