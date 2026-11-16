"""Opening the store gives every saved source a head at its latest version; synthetic data, no network."""
from contextlib import closing

import pytest
import test_collection_decisions as fixtures

from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart
from project_watch.studio.store import StudioStore


@pytest.fixture
def owner(tmp_path):
    return fixtures.owner.__wrapped__(tmp_path)


def version(pipe, pid, key, text):
    return pipe.evidence.save(EvidenceInput(project_id=pid, source_key=key, extraction_version='test-v1',
        source_type='document', visibility='public', read_scope='full',
        parts=[EvidencePart(locator_kind='stored_text', locator='page 1', text=text)]))


def heads(store, pid):
    with closing(store._connect()) as db:
        return dict(db.execute('SELECT source_key,evidence_version_id FROM studio_evidence_heads WHERE project_id=?',
                               (pid,)).fetchall())


def test_a_missing_head_points_at_the_latest_version_and_existing_heads_stay(owner):
    pipe, _, pid, _ = owner
    version(pipe, pid, 'synthetic-a', 'first reading')
    latest = version(pipe, pid, 'synthetic-a', 'second reading')
    kept = version(pipe, pid, 'synthetic-b', 'older reading')
    version(pipe, pid, 'synthetic-b', 'newer reading')
    with closing(pipe.store._connect()) as db, db:
        db.execute("DELETE FROM studio_evidence_heads WHERE project_id=? AND source_key='synthetic-a'", (pid,))
        db.execute("UPDATE studio_evidence_heads SET evidence_version_id=? WHERE project_id=? AND source_key='synthetic-b'",
                   (kept['id'], pid))
    reopened = StudioStore(pipe.store.db_path)
    found = heads(reopened, pid)
    assert found['synthetic-a'] == latest['id']
    assert found['synthetic-b'] == kept['id']  # an existing head is never moved
