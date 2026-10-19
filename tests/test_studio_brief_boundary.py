"""Offline brief separation: useful Core summaries without private investment rules."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_studio_signals import _event, _setup

from project_watch.studio.app import create_studio_app
from project_watch.studio.pipeline import Pipeline
from project_watch.studio.settings import SettingsStore


def setup_brief(tmp_path, monkeypatch, result):
    repo, store, project, _monitor, _signals = _setup(tmp_path)
    pipe = Pipeline(repo, store, SettingsStore(tmp_path / 'settings.json'))
    event_id = _event(store, project.id, '合成公司公布产品更新')
    calls = []

    def generate(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return deepcopy(result)

    model = SimpleNamespace(ready=True, json=generate)
    monkeypatch.setattr(pipe, 'tools', lambda: (None, None, model, None))
    monkeypatch.setattr(pipe, 'model_view', lambda llm=None: store)
    return pipe, project.id, event_id, calls


def output():
    return {'headline': '产品更新', 'summary': '公司发布了新产品。', 'tone': 'positive',
            'changes': [{'text': '新产品发布', 'refs': ['N1']}],
            'watch': [{'text': '后续交付公告', 'refs': ['N1']}],
            'outlook': {'opportunities': [{'text': '合成投资推断', 'refs': ['N1']}],
                        'threats': [], 'verify': []}}


def test_core_brief_retains_summary_and_sources_not_investment_fields(tmp_path, monkeypatch):
    pipe, pid, eid, calls = setup_brief(tmp_path, monkeypatch, output())
    assert pipe.brief_analysis is None
    pipe.run_brief({'project_id': pid})
    brief = pipe.store.get_brief(pid)
    assert brief['headline'] == '产品更新'
    assert brief['changes'][0]['refs'][0]['item_id'] == eid
    assert brief['watch'][0]['text'] == '后续交付公告'
    assert brief['tone'] == 'neutral' and brief['outlook'] == {}
    assert len(calls) == 1
    assert '最新动态简报' in calls[0][0]
    assert '投后分析师' not in calls[0][0] and 'opportunities' not in calls[0][0]


def test_full_brief_uses_optional_prompt_and_cited_analysis_once(tmp_path, monkeypatch):
    from project_watch.studio import brief_analysis
    data = output()
    data['outlook']['opportunities'].append({'text': '无引用推断', 'refs': ['missing']})
    pipe, pid, _eid, calls = setup_brief(tmp_path, monkeypatch, data)
    pipe.brief_analysis = brief_analysis
    pipe.run_brief({'project_id': pid})
    brief = pipe.store.get_brief(pid)
    assert brief['tone'] == 'positive'
    assert [x['text'] for x in brief['outlook']['opportunities']] == ['合成投资推断']
    assert len(calls) == 1 and calls[0][0] == brief_analysis.SYSTEM


def test_full_app_injects_brief_analysis(tmp_path):
    from project_watch.studio import brief_analysis
    app = create_studio_app(tmp_path, start_worker=False)
    assert app.state.studio_pipeline.brief_analysis is brief_analysis


def test_core_reopen_keeps_preexisting_analysis_record(tmp_path, monkeypatch):
    pipe, pid, _eid, _calls = setup_brief(tmp_path, monkeypatch, output())
    pipe.store.put_brief(pid, output())
    from project_watch.studio.store import StudioStore
    reopened = StudioStore(pipe.store.db_path)
    assert reopened.get_brief(pid)['outlook'] == output()['outlook']


@pytest.mark.parametrize('failure', ['unknown', 'stale'])
def test_brief_failure_does_not_replace_last_good(tmp_path, monkeypatch, failure):
    pipe, pid, _eid, calls = setup_brief(tmp_path, monkeypatch, output())
    pipe.store.put_brief(pid, {'headline': '历史简报'})
    if failure == 'unknown':
        from project_watch.studio.llm import ModelError
        def fail(*args, **kwargs):
            raise ModelError('call_outcome_unknown')
        model = SimpleNamespace(ready=True, json=fail)
        monkeypatch.setattr(pipe, 'tools', lambda: (None, None, model, None))
        with pytest.raises(ModelError):
            pipe.run_brief({'project_id': pid})
    else:
        monkeypatch.setattr(pipe.store, 'put_compiled_brief', lambda *a, **k: {'status': 'stale'})
        pipe.run_brief({'project_id': pid})
        assert len(calls) == 1
    assert pipe.store.get_brief(pid)['headline'] == '历史简报'
