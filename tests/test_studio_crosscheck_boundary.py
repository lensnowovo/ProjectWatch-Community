"""Synthetic comparisons: factual Core versus optional investment policy; no live calls."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_studio_signals import CrossModel, _event, _internal, _setup

from project_watch.studio import signals as sig
from project_watch.studio.attention import project_attention


def candidate():
    return {'kind': 'mismatch', 'severity': 'high', 'topic': 'B轮金额',
            'question': '差额是否为后续交割？', 'public': ['N1'], 'internal': ['M1'],
            'public_quotes': {'N1': '光启视界完成3亿元B轮融资'},
            'internal_quotes': {'M1': 'B轮融资额 2026 20000万元（实际）'}}


def setup_comparison(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo, store, project, monitor, signals = _setup(tmp_path)
    _internal(store, project.id)
    _event(store, project.id, '光启视界完成3亿元B轮融资')
    return repo, store, project, monitor, signals


class RecordingModel(CrossModel):
    def __init__(self, checks):
        super().__init__(checks)
        self.calls = []

    def json(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return super().json(system, user, **kwargs)


def test_core_crosscheck_drops_investment_fields_and_single_sided_candidates(tmp_path):
    _, store, project, monitor, signals = setup_comparison(tmp_path)
    one_side = dict(candidate(), kind='undisclosed', internal=[], internal_quotes={})
    model = RecordingModel([candidate(), one_side])
    assert sig.crosscheck(signals, store, model, project.id) == 1
    check, = signals.crosschecks(project.id)
    assert check['severity'] == 'unrated' and check['question'] == ''
    assert check['public_refs'] and check['internal_refs']
    assert check['internal_refs'][0]['refs'][0]['locator'] == '第3页'
    assert len(model.calls) == 1
    assert not any(word in model.calls[0][0] for word in ('投后', '投资经理', 'severity', 'question', 'undisclosed'))
    entry, = project_attention(store, monitor, signals, project.id, [])
    assert entry['level'] == 'info' and entry['question'] == ''


@pytest.mark.parametrize('status', ['resolved', 'dismissed'])
def test_core_crosscheck_keeps_closed_fingerprints_and_old_history(tmp_path, status):
    _, store, project, _, signals = setup_comparison(tmp_path)
    sig.crosscheck(signals, store, CrossModel([candidate()]), project.id)
    check, = signals.crosschecks(project.id)
    signals.set_status(check['id'], status)
    with store._connect() as db:
        db.execute('UPDATE studio_crosschecks SET severity=?, question=? WHERE id=?',
                   ('high', '历史投资追问', check['id']))
    before = deepcopy(signals.crosschecks(project.id, include_closed=True))
    reopened = sig.SignalStore(store)
    assert reopened.crosschecks(project.id, include_closed=True) == before
    assert sig.crosscheck(reopened, store, CrossModel([candidate()]), project.id) == 0
    assert reopened.crosschecks(project.id, include_closed=True) == before


@pytest.mark.parametrize('code', ['call_outcome_unknown', 'project_paused'])
def test_failed_comparison_keeps_last_good_without_retry(tmp_path, code):
    from project_watch.studio.llm import ModelError
    _, store, project, _, signals = setup_comparison(tmp_path)
    sig.crosscheck(signals, store, CrossModel([candidate()]), project.id)
    before = deepcopy(signals.crosschecks(project.id))
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise ModelError(code)

    with pytest.raises(ModelError):
        sig.crosscheck(signals, store, SimpleNamespace(json=fail), project.id)
    assert len(calls) == 1 and signals.crosschecks(project.id) == before


def test_full_crosscheck_injects_original_policy_once(tmp_path):
    from project_watch.studio import crosscheck_analysis
    from project_watch.studio.app import create_studio_app
    _, store, project, _, signals = setup_comparison(tmp_path / 'comparison')
    model = RecordingModel([candidate()])
    assert sig.crosscheck(signals, store, model, project.id, policy=crosscheck_analysis) == 1
    check, = signals.crosschecks(project.id)
    assert check['severity'] == 'high' and check['question'] == candidate()['question']
    assert len(model.calls) == 1
    assert model.calls[0][0] == crosscheck_analysis.SYSTEM
    import hashlib
    assert hashlib.sha256(crosscheck_analysis.SYSTEM.encode()).hexdigest() == 'f52081999fd1d036431fb21a2c328753f767db1475ff2ba451018b30c4950a25'
    assert model.calls[0][2] == {'max_tokens': 1800}
    app = create_studio_app(tmp_path / 'app', start_worker=False)
    assert app.state.studio_pipeline.crosscheck_analysis is crosscheck_analysis


@pytest.mark.parametrize('full', [False, True], ids=['core', 'full'])
def test_pipeline_passes_optional_policy_and_keeps_two_existing_calls(tmp_path, monkeypatch, full):
    from test_studio_brief_boundary import output, setup_brief
    pipe, pid, _, calls = setup_brief(tmp_path, monkeypatch, output())
    _internal(pipe.store, pid)
    # Use exactly the published synthetic item as N1.
    finding = candidate()
    finding['public_quotes'] = {'N1': '合成公司公布产品更新'}
    if full:
        from project_watch.studio import crosscheck_analysis
        pipe.crosscheck_analysis = crosscheck_analysis
    else:
        assert pipe.crosscheck_analysis is None

    def generate(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return {'checks': [finding]} if '"checks":' in system else output()

    model = SimpleNamespace(ready=True, json=generate)
    monkeypatch.setattr(pipe, 'tools', lambda: (None, None, model, None))
    pipe.run_brief({'project_id': pid})
    check, = pipe.signals.crosschecks(pid)
    assert len(calls) == 2  # One existing brief + one existing comparison, not an extra review.
    assert check['severity'] == ('high' if full else 'unrated')
    assert check['question'] == (finding['question'] if full else '')


@pytest.mark.parametrize('full', [False, True], ids=['core', 'full'])
def test_policy_cannot_bypass_internal_source_authorization(tmp_path, monkeypatch, full):
    from project_watch.studio.materials import ModelView
    _, store, project, _, signals = setup_comparison(tmp_path)
    policy = None
    if full:
        from project_watch.studio import crosscheck_analysis
        policy = crosscheck_analysis
    monkeypatch.setattr(store, 'source_analysis_allowed', lambda *args: False)
    model = RecordingModel([candidate()])
    view = ModelView(store, 'unapproved-provider-binding')
    assert sig.crosscheck(signals, view, model, project.id, policy=policy) == 0
    assert model.calls == []
    assert store.list_metrics(project.id) and store.get_profile(project.id)
