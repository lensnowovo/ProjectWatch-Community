"""Freezing analysis material reads without holding the write lock."""
import sqlite3

import test_collection_ingest as fixtures
from test_collection_ingest import trial

from project_watch.studio import material_snapshot


def test_other_writers_are_not_held_while_materials_are_checked(tmp_path, monkeypatch):
    research, unit, ingest = fixtures.host.__wrapped__(tmp_path)
    ingest(unit, [trial()])
    checked = []
    original = material_snapshot._permitted

    def permitted(db, pid, eid):
        other = sqlite3.connect(research.store.db_path, timeout=0)  # no busy wait: fails if the lock is held
        other.execute('BEGIN IMMEDIATE')
        other.rollback()
        other.close()
        checked.append(eid)
        return original(db, pid, eid)
    monkeypatch.setattr(material_snapshot, '_permitted', permitted)
    first = material_snapshot.capture(research.execution)
    assert checked and first['materials']
    again = material_snapshot.capture(research.execution)
    assert again == first  # replay returns the recorded manifest


def test_a_manifest_recorded_meanwhile_is_kept(tmp_path, monkeypatch):
    research, unit, ingest = fixtures.host.__wrapped__(tmp_path)
    ingest(unit, [trial()])
    recorded = material_snapshot.capture(research.execution)
    original, calls = material_snapshot._existing, []

    def existing(db, pid, key):
        calls.append(key)
        # the read phase sees nothing yet: another attempt records its manifest meanwhile
        return None if len(calls) == 1 else original(db, pid, key)
    monkeypatch.setattr(material_snapshot, '_existing', existing)
    assert material_snapshot.capture(research.execution) == recorded and len(calls) == 2
