"""Synthetic durable consent / at-most-once egress contracts. Never call a provider."""

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from project_watch.reading_jobs import ReadingDenied, ReadingJobs, ReadingLimits
from project_watch.repository import ProjectRepository

RECIPIENT = {"base_url": "https://example.test", "protocol": "anthropic-messages", "model": "synthetic-model"}

def setup(tmp_path, **limits):
    repo = ProjectRepository(tmp_path / "reading.sqlite3")
    project = repo.create_project("Synthetic optics")
    source = repo.add_source(project.id, kind="document", reference="bp.txt", content_sha256="a" * 64)
    ledger = ReadingJobs(repo)
    grant = ledger.authorize(
        project.id, config_id="b" * 64, model="synthetic-model", rule_version="reading-v1",
        source_hashes=[source.content_sha256], include_future=False,
        limits=ReadingLimits(**limits), actor="local-user", recipient=RECIPIENT,
    )
    return repo, project, source, ledger, grant


def enqueue(ledger, grant, source, digest="c" * 64):
    return ledger.enqueue(grant["id"], sources={source.id: source.content_sha256}, input_digest=digest)


def dispatch(ledger, job, lease, config_id="b" * 64):
    body = json.dumps({"model": "synthetic-model", "stream": True,
                       "max_tokens": 128, "messages": [{"role": "user", "content": "synthetic"}]}).encode()
    return ledger.before_send(job["id"], lease, config_id=config_id)(body)


def test_duplicate_scope_and_restart_do_not_reset_budget(tmp_path):
    repo, project, source, ledger, grant = setup(tmp_path, max_requests=1)
    job = enqueue(ledger, grant, source)
    assert enqueue(ReadingJobs(repo), grant, source)["id"] == job["id"]
    lease = ledger.claim(job["id"])["lease_token"]
    dispatch(ledger, job, lease)
    ledger.finish(job["id"], lease, succeeded=True)
    reopened = ReadingJobs(ProjectRepository(repo.db_path))
    assert reopened.status(grant["id"])["requests_used"] == 1
    assert reopened.get(job["id"])["state"] == "succeeded"
    assert reopened.claim(job["id"]) is None
    other = enqueue(reopened, grant, source, "d" * 64)
    token = reopened.claim(other["id"])["lease_token"]
    with pytest.raises(ReadingDenied, match="budget"):
        dispatch(reopened, other, token)
    assert reopened.status(grant["id"])["requests_used"] == 1
    assert repo.list_observations(project.id) == []  # Ledger never writes business facts.


def test_pause_revoke_scope_and_model_checked_at_dispatch(tmp_path):
    repo, project, source, ledger, grant = setup(tmp_path)
    job = enqueue(ledger, grant, source)
    lease = ledger.claim(job["id"])["lease_token"]
    ledger.set_status(grant["id"], "paused", actor="local-user")
    with pytest.raises(ReadingDenied, match="active"):
        dispatch(ledger, job, lease)
    ledger.set_status(grant["id"], "active", actor="local-user")
    with pytest.raises(ReadingDenied, match="configuration"):
        dispatch(ledger, job, lease, "d" * 64)
    other = repo.add_source(project.id, kind="document", reference="new.txt", content_sha256="e" * 64)
    with pytest.raises(ReadingDenied, match="scope"):
        enqueue(ledger, grant, other)
    different = repo.create_project("Another company")
    foreign = repo.add_source(different.id, kind="document", reference="other.txt", content_sha256="a" * 64)
    with pytest.raises(ReadingDenied, match="scope"):
        enqueue(ledger, grant, foreign)
    ledger.set_status(grant["id"], "revoked", actor="local-user")
    with pytest.raises(ReadingDenied, match="revoked"):
        ledger.set_status(grant["id"], "active", actor="local-user")
    assert ledger.status(grant["id"])["requests_used"] == 0


def test_concurrent_claim_and_dispatch_only_one_winner(tmp_path):
    repo, _, source, ledger, grant = setup(tmp_path)
    job = enqueue(ledger, grant, source)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: ReadingJobs(repo).claim(job["id"]), range(4)))
    claimed = [r for r in results if r]
    assert len(claimed) == 1
    token = claimed[0]["lease_token"]

    def attempt(_):
        try:
            dispatch(ReadingJobs(repo), job, token)
            return True
        except ReadingDenied:
            return False

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(attempt, range(4))) == 1
    assert ledger.status(grant["id"])["requests_used"] == 1


def test_expired_dispatched_call_never_retries_or_refunds(tmp_path):
    _, _, source, ledger, grant = setup(tmp_path)
    job = enqueue(ledger, grant, source)
    now = time.time()
    claim = ledger.claim(job["id"], now=now)
    dispatch(ledger, job, claim["lease_token"])
    ledger.recover(now=now + 1000)
    assert ledger.get(job["id"])["state"] == "uncertain"
    assert ledger.claim(job["id"]) is None
    assert ledger.status(grant["id"])["requests_used"] == 1
    with pytest.raises(ReadingDenied):
        ledger.finish(job["id"], claim["lease_token"], succeeded=True)


def test_pre_dispatch_recovery_bounded_and_stale_worker_denied(tmp_path):
    _, _, source, ledger, grant = setup(tmp_path)
    job = enqueue(ledger, grant, source)
    first = ledger.claim(job["id"], now=100)
    ledger.recover(now=1000)
    second = ledger.claim(job["id"], now=1001)
    with pytest.raises(ReadingDenied):
        dispatch(ledger, job, first["lease_token"])
    ledger.recover(now=2000)
    assert ledger.claim(job["id"], now=2001)
    ledger.recover(now=3000)
    assert ledger.get(job["id"])["state"] == "failed"
    assert ledger.claim(job["id"]) is None
    assert first["lease_token"] != second["lease_token"]
    assert ledger.status(grant["id"])["requests_used"] == 0


def test_future_files_need_explicit_separate_authorization(tmp_path):
    repo, project, source, ledger, first_grant = setup(tmp_path)
    original = enqueue(ledger, first_grant, source)
    token = ledger.claim(original["id"])["lease_token"]
    dispatch(ledger, original, token)
    ledger.finish(original["id"], token, succeeded=True)
    grant = ledger.authorize(
        project.id, config_id="b" * 64, model="synthetic-model", rule_version="reading-v1",
        source_hashes=[], include_future=True, limits=ReadingLimits(), actor="local-user", recipient=RECIPIENT,
    )
    other = repo.add_source(project.id, kind="document", reference="later.txt", content_sha256="e" * 64)
    job = enqueue(ledger, grant, other)
    assert job["state"] == "queued"
    assert ledger.status(grant["id"])["include_future"] is True
    # A new grant does not rebill the same project/input/model/rule.
    assert enqueue(ledger, grant, source)["id"] == original["id"]
    assert ledger.status(grant["id"])["requests_used"] == 0


def test_cost_reservation_and_invalid_body_do_not_egress(tmp_path):
    _, _, source, ledger, grant = setup(
        tmp_path, max_cost_cny="0.0001", input_cny_per_million="1", output_cny_per_million="1"
    )
    job = enqueue(ledger, grant, source)
    token = ledger.claim(job["id"])["lease_token"]
    with pytest.raises(ReadingDenied, match="budget"):
        dispatch(ledger, job, token)
    callback = ledger.before_send(job["id"], token, config_id="b" * 64)
    with pytest.raises(ReadingDenied):
        callback(b"invalid json")
    assert ledger.status(grant["id"])["requests_used"] == 0


def test_revision_identity_and_no_implicit_grant(tmp_path):
    _, _, source, ledger, grant = setup(tmp_path)
    with pytest.raises(ReadingDenied):
        ledger.enqueue("missing", sources={source.id: source.content_sha256}, input_digest="c" * 64)
    original = enqueue(ledger, grant, source)
    with pytest.raises(ReadingDenied):
        ledger.enqueue(grant["id"], sources={source.id: "d" * 64}, input_digest="c" * 64)
    assert ledger.get(original["id"])["state"] == "queued"


def test_authorization_audit_is_append_only(tmp_path):
    repo, _, _, ledger, grant = setup(tmp_path)
    ledger.set_status(grant["id"], "paused", actor="local-user")
    import sqlite3
    with sqlite3.connect(repo.db_path) as db:
        assert db.execute("SELECT count(*) FROM reading_authorization_events").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM reading_authorization_events")


def test_budget_proxy_invokes_durable_gate_before_network(tmp_path):
    from io import BytesIO

    from project_watch.model_gateway import BudgetGate, BudgetProxy

    _, _, source, ledger, grant = setup(tmp_path)
    job = enqueue(ledger, grant, source)
    token = ledger.claim(job["id"])["lease_token"]
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            # Reservation must be durable BEFORE any network activity.
            assert ledger.status(grant["id"])["requests_used"] == 1
            assert ledger.get(job["id"])["state"] == "dispatched"
            return BytesIO(b"synthetic response")

    proxy = BudgetProxy(
        BudgetGate(ReadingLimits().policy("synthetic-model")),
        upstream_url="https://example.test/v1/messages", provider_key="test-key", local_token="local-only",
        opener=Opener(), before_send=ledger.before_send(job["id"], token, config_id="b" * 64),
    )
    body = json.dumps({"model": "synthetic-model", "stream": True, "max_tokens": 128, "messages": []}).encode()
    ledger.set_status(grant["id"], "paused", actor="local-user")
    with pytest.raises(ReadingDenied):
        proxy.forward(body)
    assert calls == []
    ledger.set_status(grant["id"], "active", actor="local-user")
    assert proxy.forward(body) == b"synthetic response"
    with pytest.raises(ReadingDenied):
        proxy.forward(body)
    assert calls == ["https://example.test/v1/messages"]
    ledger.finish(job["id"], token, succeeded=False)
    assert ledger.get(job["id"])["state"] == "uncertain"


def test_aggregate_estimated_cost_is_not_reset_between_jobs(tmp_path):
    _, _, source, ledger, grant = setup(tmp_path, max_cost_cny="0.0007",
                                       input_cny_per_million="1", output_cny_per_million="1")
    first = enqueue(ledger, grant, source)
    dispatch(ledger, first, ledger.claim(first["id"])["lease_token"])
    other = enqueue(ledger, grant, source, "d" * 64)
    with pytest.raises(ReadingDenied, match="budget"):
        dispatch(ledger, other, ledger.claim(other["id"])["lease_token"])
    assert ledger.status(grant["id"])["requests_used"] == 1


def test_backup_restores_authorization_reservations_and_retains_existing_facts(tmp_path):
    from project_watch.backup import create_backup, restore_backup, verify_backup

    live = tmp_path / "live"
    live.mkdir()
    repo, project, source, ledger, grant = setup(live)
    job = enqueue(ledger, grant, source)
    dispatch(ledger, job, ledger.claim(job["id"])["lease_token"])
    backup = tmp_path / "backup"
    create_backup(repo.db_path, backup)
    verify_backup(backup)
    restored_db = restore_backup(backup, tmp_path / "restored")
    restored_repo = ProjectRepository(restored_db)
    restored = ReadingJobs(restored_repo)
    assert restored.status(grant["id"])["requests_used"] == 1
    assert restored.get(job["id"])["state"] == "dispatched"
    assert restored_repo.list_sources(project.id) == repo.list_sources(project.id)
    restored.recover(now=time.time() + 1000)
    assert restored.get(job["id"])["state"] == "uncertain"
    assert restored.claim(job["id"]) is None


def test_lease_expiry_and_source_mutation_rechecked_before_send(tmp_path):
    from contextlib import closing

    repo, _, source, ledger, grant = setup(tmp_path)
    job = enqueue(ledger, grant, source)
    old = ledger.claim(job["id"], now=time.time() - 1000)
    with pytest.raises(ReadingDenied, match="lease"):
        dispatch(ledger, job, old["lease_token"])
    ledger.recover()
    current = ledger.claim(job["id"])
    # Adversarial mutation; ordinary application sources are immutable versions.
    with closing(repo._connect()) as db, db:
        db.execute("UPDATE sources SET content_sha256=? WHERE id=?", ("d" * 64, source.id))
    with pytest.raises(ReadingDenied, match="scope"):
        dispatch(ledger, job, current["lease_token"])
    assert ledger.status(grant["id"])["requests_used"] == 0


@pytest.mark.parametrize("kwargs", [{"max_requests": 0}, {"max_requests": True},
                                   {"max_cost_cny": "NaN"}, {"max_request_bytes": -1}])
def test_invalid_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        ReadingLimits(**kwargs)
