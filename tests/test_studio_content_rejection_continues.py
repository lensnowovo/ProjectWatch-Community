"""A provider content refusal on one batch must not stop the whole round (R44). Synthetic.

The refused batch is never resent or split; it stays visible as unassessed.
"""

import pytest

from project_watch.studio.llm import ModelError
from tests.test_studio_data_path import setup
from tests.test_studio_triage_split import _setup


class Refuses:
    ready = True

    def __init__(self):
        self.calls = 0

    def json(self, *_args, **_kwargs):
        self.calls += 1
        raise ModelError('模型服务拒绝分析本批内容；已保留资料和响应，未自动重发', code='model_content_rejected', status=400)


def test_prefilter_refusal_fails_open_without_resend(tmp_path):
    pipeline, pid = setup(tmp_path)
    llm = Refuses()
    eligible = pipeline.prefilter_bodies(pid, llm, pipeline.store.get_project(pid), {}, lambda _: None)
    assert len(eligible) == 2 and llm.calls == 1


def test_triage_refusal_keeps_batch_visible_unassessed_and_continues(tmp_path):
    store, project, pipeline = _setup(tmp_path, 25)
    llm = Refuses()
    pipeline.triage(project.id, llm, store.get_project(project.id), {"competitors": []}, lambda _: None)
    items = store.list_items(project_id=project.id, include_dismissed=True)
    assert len(items) == 25 and store.untriaged(project.id) == []
    assert all(i['importance'] == 'routine' and '拒绝' in i['impact'] for i in items)
    # One call per batch, never a bisection or resend of refused content.
    assert llm.calls == 2


def test_other_model_errors_still_fail(tmp_path):
    pipeline, pid = setup(tmp_path)

    class Broken(Refuses):
        def json(self, *_a, **_k):
            raise ModelError('x', code='model_http_failure')

    with pytest.raises(ModelError):
        pipeline.prefilter_bodies(pid, Broken(), pipeline.store.get_project(pid), {}, lambda _: None)


@pytest.mark.parametrize('code', ['model_output_invalid_json', 'model_output_invalid_shape',
                                  'model_output_incomplete', 'model_output_truncated'])
def test_unusable_prefilter_reply_reads_the_batch_instead_of_failing(tmp_path, code):
    """A definite "no usable answer" only prioritises reading; it must not fail the round."""
    pipeline, pid = setup(tmp_path)

    class Unclear(Refuses):
        def json(self, *_a, **_k):
            self.calls += 1
            raise ModelError('x', code=code)
    llm = Unclear()
    eligible = pipeline.prefilter_bodies(pid, llm, pipeline.store.get_project(pid), {}, lambda _: None)
    assert len(eligible) == 2 and llm.calls == 1


@pytest.mark.parametrize('reply', [{'results': 'nope'}, {'results': [1]}, None])
def test_malformed_or_conflicting_prefilter_reply_reads_rather_than_fails(tmp_path, reply):
    pipeline, pid = setup(tmp_path)

    class Malformed(Refuses):
        def json(self, *_a, **_k):
            return reply
    assert len(pipeline.prefilter_bodies(pid, Malformed(), pipeline.store.get_project(pid), {},
                                         lambda _: None)) == 2


@pytest.mark.parametrize('code', ['provider_backoff', 'request_budget', 'project_paused', 'model_rate_limited',
                                  'request_outcome_unknown', 'model_timeout'])
def test_recoverable_and_uncertain_outcomes_still_stop_the_prefilter(tmp_path, code):
    """An uncertain outcome holds the round for the owner; it is not silently continued."""
    pipeline, pid = setup(tmp_path)

    class Backoff(Refuses):
        def json(self, *_a, **_k):
            raise ModelError('x', code=code)
    with pytest.raises(ModelError):
        pipeline.prefilter_bodies(pid, Backoff(), pipeline.store.get_project(pid), {}, lambda _: None)


def test_a_conflicting_decision_is_read_and_the_others_are_kept(tmp_path):
    import json
    pipeline, pid = setup(tmp_path)

    class Conflicting(Refuses):
        def json(self, system, user, **_k):
            a, b = [row['id'] for row in json.loads(user)['items']]
            return {'results': [{'id': a, 'read': True}, {'id': a, 'read': False}, {'id': b, 'read': False}]}
    eligible = pipeline.prefilter_bodies(pid, Conflicting(), pipeline.store.get_project(pid), {}, lambda _: None)
    assert len(eligible) == 1  # the contradicted item is read; the clear "skip" still stands


def test_a_failed_open_batch_is_marked_and_not_resent_on_resume(tmp_path):
    import json

    from project_watch.studio.execution import JobExecution
    from project_watch.studio.store import StudioStore
    pipeline, pid = setup(tmp_path)
    pipeline.store.enqueue(pid, 'collect')
    job = pipeline.store.claim()
    pipeline._execution_state.current = JobExecution(pipeline.store, job)

    class Unclear(Refuses):
        def json(self, *_a, **_k):
            self.calls += 1
            raise ModelError('x', code='model_output_invalid_json')
    llm = Unclear()
    args = (pid, llm, pipeline.store.get_project(pid), {}, lambda _: None)
    first = pipeline.prefilter_bodies(*args)
    assert len(first) == 2 and llm.calls == 1
    marks = {json.loads(i['extra'])['body_prefilter'].get('unassessed') for i in pipeline.store.untriaged(pid)}
    assert marks == {'model_output_invalid_json'}
    pipeline.store = StudioStore(pipeline.store.db_path)
    pipeline._execution_state.current = JobExecution(pipeline.store, job)
    assert pipeline.prefilter_bodies(*args) == first
    assert llm.calls == 1
