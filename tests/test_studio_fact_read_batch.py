"""Synthetic facts: batching must preserve disclosure and live dependency updates."""
from contextlib import closing

import pytest
from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample

from project_watch.studio.monitoring_contracts import FactInput, LocatedRef, ObjectInput


def test_fact_dependencies_are_batched_and_not_cached(tmp_path, monkeypatch):
    service, pid = make_service(tmp_path/'batch.sqlite3')
    text = 'Synthetic Co has a product'
    evidence = service.evidence.save(sample(pid, text).model_copy(update={'visibility':'public'}))
    ref = LocatedRef(evidence_version_id=evidence['id'], part_index=0, start=0, end=len(text), quote=text)
    subject = service.objects.save(pid, ObjectInput(kind='company', name='Synthetic Co', visibility='public'), actor='user')
    for i in range(20):
        service.objects.save_fact(pid, FactInput(subject_id=subject['id'], predicate=f'feature_{i}', text=text,
                                               visibility='public', refs=(ref,)))
    private = service.objects.save_fact(pid, FactInput(subject_id=subject['id'], predicate='private_feature', text=text,
                                                      visibility='internal', refs=(ref,)))
    expected = service.objects.facts(pid, subject['id'])
    def no_individual_get(*args, **kwargs):
        pytest.fail('facts() must not perform a dependency query per fact')
    monkeypatch.setattr(service.evidence.dependencies, 'get', no_individual_get)
    assert service.objects.facts(pid, subject['id']) == expected
    assert len(service.objects.facts(pid, subject['id'], include_internal=False)) == 20
    # Missing dependency must stay missing, not disappear through an inner join.
    with closing(service.store._connect()) as db, db:
        for table in ('studio_dependency_links', 'studio_object_dependency_links'):
            db.execute(f'DELETE FROM {table} WHERE project_id=? AND artifact_key=?', (pid,'fact:'+private['id']))
        db.execute('DELETE FROM studio_analysis_dependencies WHERE project_id=? AND artifact_key=?', (pid,'fact:'+private['id']))
    assert next(f for f in service.objects.facts(pid, subject['id']) if f['id']==private['id'])['dependency'] is None
    service.evidence.dependencies.withdraw(pid,evidence['id'],'synthetic withdrawal',actor='user')
    assert all(f['dependency']['status'] != 'current' for f in service.objects.facts(pid,subject['id'],include_internal=False))
    other_pid = service.store.list_projects()[0]['id'] + '-not-this-project'
    with pytest.raises(KeyError):
        service.objects.facts(other_pid,subject['id'])
