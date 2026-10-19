"""A provider content refusal on one batch must not stop the whole round (R44). Synthetic.

The refused batch is never resent or split; it stays visible as unassessed.
"""

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
    import pytest

    pipeline, pid = setup(tmp_path)

    class Broken(Refuses):
        def json(self, *_a, **_k):
            raise ModelError('x', code='model_http_failure')

    with pytest.raises(ModelError):
        pipeline.prefilter_bodies(pid, Broken(), pipeline.store.get_project(pid), {}, lambda _: None)
