"""M3B: a failed T1 comparison must not masquerade as a completed update."""

import runpy
from pathlib import Path

import pytest

from project_watch.studio.llm import ModelError

SUPPORT = runpy.run_path(str(Path(__file__).with_name('distribution_journey_support.py')))
CORE = runpy.run_path(str(Path(__file__).with_name('core_without_insight_support.py')))


@pytest.mark.parametrize('code', [
    'request_outcome_unknown', 'model_timeout', 'request_budget', 'provider_backoff',
    'model_content_rejected', 'model_access_denied', 'model_output_invalid_shape',
])
def test_comparison_failure_is_not_an_empty_success(tmp_path, code):
    app, _ = SUPPORT['make_app'](tmp_path)
    pipeline = app.state.studio_pipeline
    project = app.state.intake_worker.repository.create_project('合成变化项目')

    class FailedModel:
        def json(self, *_args, **_kwargs):
            raise ModelError('本轮变化对照未完成', code=code)

    # No fake DB repair or model retry: this is the real comparison method.
    with pytest.raises(ModelError) as caught:
        pipeline._record_changes(
            project.id, FailedModel(),
            {'F1': {'source_id': 'synthetic-new', 'source_name': 'synthetic-t1.txt', 'locator': 'line 1'}},
            {'synthetic-new'}, {'summary': '合成旧材料'},
            [{'text': '合成新材料出现变化', 'ref': 'F1'}],
        )
    assert caught.value.code == code

