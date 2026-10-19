"""Full isolated intake -> consent -> bounded compiler -> persistent reading loop."""

import json
from dataclasses import asdict

import pytest

from project_watch.company_state import READING_OUTPUT_TOKENS, READING_RULE_VERSION, CompanyState
from project_watch.model_provider import ModelProviderConfig
from project_watch.model_settings import ModelSettings
from project_watch.reading_jobs import ReadingJobs, ReadingLimits
from project_watch.reading_worker import ReadingWorker
from project_watch.repository import ProjectRepository
from tests.test_company_state import material


class FakeModel:
    def __init__(self):
        self.config = ModelProviderConfig("https://example.test", "anthropic-messages", "fake")
        self.calls = 0
        self.before = None

    def authorization_id(self):
        return "b" * 64

    def disclosure(self):
        return {"remaining_runs": 100}

    def compile_reading(self, packet, *, before_send):
        if self.before:
            self.before()
        before_send(json.dumps({"model": "fake", "stream": True, "max_tokens": READING_OUTPUT_TOKENS, "messages": []}).encode())
        self.calls += 1
        fragment = packet["fragments"][0]
        return {"items": [{"topic": "business", "headline": "业务进展", "text": fragment["text"],
                           "evidence": [{"ref_id": fragment["ref_id"], "quote": fragment["text"]}], "as_of": None}]}


def sample(tmp_path):
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Synthetic integration")
    source = material(repo, project, "合成公司主营工业检测设备。")
    ledger = ReadingJobs(repo)
    model = FakeModel()
    settings = ModelSettings(model)
    worker = ReadingWorker(repo, ledger, settings)
    return repo, project, source, ledger, model, settings, worker


def authorize(project, source, ledger, model, *, future=False, calls=20):
    return ledger.authorize(project.id, config_id=model.authorization_id(), model="fake", rule_version=READING_RULE_VERSION,
                            source_hashes=[source.content_sha256], include_future=future,
                            limits=ReadingLimits(max_requests=calls, max_output_tokens=READING_OUTPUT_TOKENS), actor="synthetic",
                            recipient=asdict(model.config))


def test_consent_material_increment_and_restart(tmp_path):
    repo, project, source, ledger, model, settings, worker = sample(tmp_path)
    assert worker.process_one() is None and model.calls == 0
    authorize(project, source, ledger, model, future=True)
    assert worker.process_one()["state"] == "succeeded"
    assert worker.process_one() is None
    reboot = ReadingWorker(ProjectRepository(repo.db_path), ReadingJobs(repo), settings)
    assert reboot.process_one() is None and model.calls == 1
    material(repo, project, "合成公司新增客户试用，尚未形成销售收入。", "e" * 64)
    assert reboot.process_one()["state"] == "succeeded"
    assert model.calls == 2
    assert len(CompanyState(repo).read(project.id)["batches"]) == 2
    assert repo.list_observations(project.id) == []


def test_pause_configuration_change_and_no_model_no_call(tmp_path):
    _, project, source, ledger, model, settings, worker = sample(tmp_path)
    grant = authorize(project, source, ledger, model)
    ledger.set_status(grant["id"], "paused", actor="synthetic")
    assert worker.process_one() is None
    ledger.set_status(grant["id"], "active", actor="synthetic")
    settings.model = None
    assert worker.process_one() is None
    settings.model = model
    model.before = lambda: setattr(settings, "model", None)
    assert worker.process_one()["state"] == "queued"
    assert model.calls == 0
    assert ledger.status(grant["id"])["requests_used"] == 0


def test_budget_exhaustion_leaves_remaining_material_without_call(tmp_path):
    repo, project, source, ledger, model, _, worker = sample(tmp_path)
    grant = authorize(project, source, ledger, model, future=True, calls=1)
    assert worker.process_one()["state"] == "succeeded"
    material(repo, project, "新增设备正在研发。", "e" * 64)
    assert worker.process_one() is None
    assert model.calls == 1
    assert ledger.status(grant["id"])["requests_used"] == 1


def test_bad_output_and_provider_failure_never_repeat_or_publish(tmp_path):
    repo, project, source, ledger, model, _, worker = sample(tmp_path)
    authorize(project, source, ledger, model)
    original = model.compile_reading

    def invalid(packet, *, before_send):
        result = original(packet, before_send=before_send)
        result["items"][0]["evidence"][0]["quote"] = "伪造出处"
        return result

    model.compile_reading = invalid
    assert worker.process_one()["state"] == "uncertain"
    assert worker.process_one() is None and model.calls == 1
    assert CompanyState(repo).read(project.id)["batches"] == []


def test_status_is_read_only_distinguishes_pause_budget_missing_and_legacy_rule(tmp_path):
    _, project, source, ledger, model, settings, worker = sample(tmp_path)
    assert worker.status(project.id)["reason"] == "consent_required"
    grant = authorize(project, source, ledger, model, calls=1)
    assert worker.status(project.id)["poll"] is True
    assert model.calls == 0
    ledger.set_status(grant["id"], "paused", actor="synthetic")
    assert worker.status(project.id)["reason"] == "paused"
    ledger.set_status(grant["id"], "active", actor="synthetic")
    worker.process_one()
    assert worker.status(project.id)["reason"] == "budget_reached"
    assert worker.status(project.id)["counts"] == {"succeeded": 1}
    settings.model = None
    assert worker.status(project.id)["reason"] == "model_missing"


def test_pause_between_claim_and_send_is_enforced(tmp_path):
    _, project, source, ledger, model, _, worker = sample(tmp_path)
    grant = authorize(project, source, ledger, model)
    model.before = lambda: ledger.set_status(grant["id"], "paused", actor="synthetic")
    assert worker.process_one()["state"] == "queued"
    assert model.calls == 0
    assert ledger.status(grant["id"])["requests_used"] == 0


@pytest.mark.parametrize('previous_rule', ['company-reading-v2', 'company-reading-v4'])
def test_editorial_rule_upgrade_requires_new_consent_and_preserves_history(tmp_path, monkeypatch, previous_rule):
    import project_watch.company_state as state_module
    import project_watch.reading_worker as worker_module

    repo, project, source, ledger, model, settings, _ = sample(tmp_path)
    current_rule = READING_RULE_VERSION
    assert current_rule != previous_rule
    # Write an actual previous-rule result, then restart under the current rule.
    with monkeypatch.context() as old:
        old.setattr(state_module, "READING_RULE_VERSION", previous_rule)
        old.setattr(worker_module, "READING_RULE_VERSION", previous_rule)
        grant = ledger.authorize(
            project.id, config_id=model.authorization_id(), model="fake",
            rule_version=previous_rule, source_hashes=[source.content_sha256],
            include_future=True, limits=ReadingLimits(max_requests=2, max_output_tokens=READING_OUTPUT_TOKENS),
            actor="synthetic", recipient=asdict(model.config),
        )
        assert ReadingWorker(repo, ledger, settings).process_one()["state"] == "succeeded"
    history = CompanyState(repo).read(project.id)
    assert history["batches"][0]["rule_version"] == previous_rule
    material(repo, project, "合成公司新增试用项目。", "e" * 64)
    reboot = ReadingWorker(ProjectRepository(repo.db_path), ReadingJobs(repo), settings)
    assert reboot.status(project.id)["reason"] == "consent_required"
    assert reboot.process_one() is None and model.calls == 1
    assert ledger.status(grant["id"])["requests_used"] == 1
    assert CompanyState(repo).read(project.id) == history
