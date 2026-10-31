"""Generation is optional; stored/Agent judgments are not. Offline synthetic data."""


from project_watch.studio import insight
from project_watch.studio.app import create_studio_app


def test_core_result_module_contains_no_generator():
    for name in ('SYSTEM', 'gather', 'normalize', 'run', 'due'):
        assert not hasattr(insight, name), name




def test_full_app_injects_optional_generator(tmp_path):
    from project_watch.studio import insight_generation

    app = create_studio_app(tmp_path, start_worker=False)
    assert app.state.studio_pipeline.insight_generator is insight_generation


