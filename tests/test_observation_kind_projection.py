"""Synthetic regression: select required observations before projection and pagination."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from project_watch.studio import observations


@pytest.fixture
def service(tmp_path, monkeypatch):
    path = tmp_path / 'observations.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE studio_fact_records (project_id TEXT, id TEXT, visibility TEXT, record TEXT)')
        for identity, kind, visibility, project in [
            ('a1', 'policy', 'public', 'a'), ('a2', 'policy', 'public', 'a'),
            ('b1', 'business_relation', 'public', 'a'), ('b2', 'business_relation', 'public', 'a'),
            ('b3', 'business_relation', 'internal', 'a'), ('b4', 'business_relation', 'public', 'other'),
        ]:
            db.execute('INSERT INTO studio_fact_records VALUES (?,?,?,?)',
                       (project, identity, visibility, json.dumps({'id': identity, 'attributes': {'kind': kind}})))
    def forbidden(*args, **kwargs):
        pytest.fail('unrelated policy projector must not run')
    monkeypatch.setattr(observations, 'project_policy', forbidden)
    monkeypatch.setattr(observations, 'project_relation', lambda s, p, fact, **kw: {'id': fact['id']})
    return SimpleNamespace(store=SimpleNamespace(_connect=lambda: sqlite3.connect(path), get_project=lambda p: {}))


def test_kind_before_projection_limit_and_visibility(service):
    result = observations.read(service, 'a', kind='business_relation', limit=1)
    assert [r['id'] for r in result['items']] == ['b1']
    assert result['next_cursor'] == 'b1'
    second = observations.read(service, 'a', kind='business_relation', limit=1, after=result['next_cursor'])
    assert [r['id'] for r in second['items']] == ['b2']
    assert not second['truncated']
    internal = observations.read(service, 'a', kind='business_relation', include_internal=True)
    assert [r['id'] for r in internal['items']] == ['b1', 'b2', 'b3']


def test_invalid_kind_rejected(service):
    with pytest.raises(ValueError, match='kind'):
        observations.read(service, 'a', kind="policy' OR 1=1")
