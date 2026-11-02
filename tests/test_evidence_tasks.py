"""Synthetic durable material-stage invariants; no external services."""
from contextlib import closing

import pytest
import test_collection_view as fixtures
from test_collection_ingest import trial

from project_watch.studio import evidence_tasks


@pytest.fixture
def owner(tmp_path):
    return fixtures.owner.__wrapped__(tmp_path)


def rows(owner):
    with closing(owner[0].store._connect()) as db:
        return [dict(row) for row in db.execute('SELECT * FROM studio_evidence_tasks ORDER BY rowid')]


def proposal(source, _execution):
    return {'observations': [{'text': 'Source assertion, not an approved fact', 'refs': [next(iter(source['refs']))]}]}


def two(owner):
    execution, unit, ingest, ref = fixtures.save(owner)
    second = trial(url='https://example.org/second')
    second['extra']['source_identity'] = 'clinical:NCT00000002'
    ingest(unit, [second])
    return execution, unit, ingest, ref














def test_ingest_failure_rolls_back_source_and_stage_together(owner, monkeypatch):
    _, unit, ingest, _ = fixtures.save(owner)
    before = rows(owner)
    monkeypatch.setattr(evidence_tasks, 'admit', lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic insert error')))
    second = trial(url='https://example.org/uncommitted')
    second['extra']['source_identity'] = 'clinical:NCT00000002'
    with pytest.raises(RuntimeError, match='synthetic insert error'):
        ingest(unit, [second])
    assert rows(owner) == before
    with closing(owner[0].store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_collection_sources').fetchone()[0] == 1


