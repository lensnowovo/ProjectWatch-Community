"""Owner-confirmed company names reach institution/applicant indexes on the ordinary entry.

Synthetic companies and transports only; the network is denied.
"""
import json
import socket
from contextlib import closing
from datetime import date
from urllib.parse import unquote_plus, urlsplit

import pytest

from project_watch.studio import (
    bootstrap_collection,
    collection_host,
    dsh_runtime,
    event_insight_work,
    progressive_bootstrap,
)
from project_watch.studio.app import create_studio_app
from project_watch.studio.collection_ledger import STAGE
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.monitoring_contracts import Alias, ObjectInput
from project_watch.studio.settings import ModelConfig

LEGAL = '合成宿主生物制药（苏州）有限公司'


def confirm(pipeline, project_id, names):
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        return pipeline.objects.confirm_subject_identity(project_id, LEGAL, names, db=db)


def add_aliases(pipeline, project_id, *aliases):
    with closing(pipeline.store._connect()) as db:
        object_id = db.execute('SELECT object_id FROM studio_primary_objects WHERE project_id=?', (project_id,)).fetchone()[0]
    old = pipeline.objects.get(project_id, object_id)
    item = ObjectInput.model_validate({k: old[k] for k in ObjectInput.model_fields})
    updated = ObjectInput.model_validate({**item.model_dump(), 'aliases': [*item.aliases, *aliases]})
    pipeline.objects.save(project_id, updated, actor='user', object_id=object_id, expected_version=old['version'])


@pytest.fixture
def entry(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network prohibited'))
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    settings = pipeline.settings_store.load()
    settings.model = ModelConfig(base_url='https://model.example.invalid/v1', model='synthetic', api_key='synthetic')
    pipeline.settings_store.save(settings)
    project = pipeline.repository.create_project('合成宿主')
    pipeline.store.upsert_project(project.id, {'legal_name': LEGAL}, consent=True)
    # A second subject's confirmed name must never enter this project's queries.
    other = pipeline.repository.create_project('Other Subject')
    pipeline.store.upsert_project(other.id, {'legal_name': 'Other Subject Labs'}, consent=True)
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        pipeline.objects.confirm_subject_identity(other.id, 'Other Subject Labs', ['Wrong Subject Biologics'], db=db)
    calls, held = [], {}

    def send(_method, url, *_args):
        calls.append(url)
        host = urlsplit(url).hostname
        if host == 'api.openalex.org':
            return Response(200, {}, json.dumps({'results': [], 'meta': {'count': 0}}).encode())
        if host == 'www.cninfo.com.cn':
            return Response(200, {}, b'{"announcements":[],"totalAnnouncement":0}')
        if host == 'www.ebi.ac.uk':
            return Response(200, {}, b'{"hitCount":0,"resultList":{"result":[]}}')
        pytest.fail('unexpected provider dispatch: ' + host)

    pipeline.http_factory = lambda _: Http(send)
    # Stop after the host's base collection; model roles are outside this check.
    monkeypatch.setattr(bootstrap_collection, 'run', lambda research, *_: held.setdefault('research', research))
    monkeypatch.setattr(progressive_bootstrap, 'run', lambda *_: {'status': 'stopped_for_test'})
    monkeypatch.setattr(event_insight_work, 'settle', lambda *_: {})

    def start():
        pipeline.store.enqueue(project.id, 'external')
        execution = JobExecution(pipeline.store, pipeline.store.claim())
        pipeline._execution_state.current = execution
        dsh_runtime.run_bootstrap(pipeline, execution, None)
        return execution

    return pipeline, project.id, calls, held, start


def sent_queries(calls):
    return [unquote_plus(url) for url in calls if urlsplit(url).hostname in ('www.ebi.ac.uk', 'api.openalex.org')]


def test_confirmed_english_name_reaches_affiliation_and_applicant_indexes(entry):
    pipeline, pid, calls, _held, start = entry
    confirm(pipeline, pid, ['Synthetic Host Biologics', 'SynHost'])
    execution = start()
    assert progressive_bootstrap.freeze(execution, 'bootstrap')['owner'] == 'materials'  # ordinary new-project path
    plan = execution.read_checkpoint(STAGE)['plan']
    by_source = {}
    for unit in plan['units']:
        by_source.setdefault(unit['source'], []).append(unit['query'])
    for source in ('openalex_affiliation', 'europepmc_affiliation', 'europepmc_patent', 'patent_search'):
        assert sorted(by_source[source]) == sorted([LEGAL, 'Synthetic Host Biologics', 'SynHost']), source
    sent = ' '.join(sent_queries(calls))
    assert 'Synthetic Host Biologics' in sent and 'SynHost' in sent
    assert 'Wrong Subject Biologics' not in sent and 'Other Subject Labs' not in sent
    # Company news searches are unchanged by this fix.
    assert 'Synthetic Host Biologics' not in by_source['search']


def test_internal_alias_and_not_yet_valid_name_are_never_sent_but_former_name_is(entry):
    pipeline, pid, calls, _held, start = entry
    confirm(pipeline, pid, ['Synthetic Host Biologics'])
    future = date.today().replace(year=date.today().year + 1)
    add_aliases(pipeline, pid,
                Alias(name='Secret Codename', visibility='internal'),
                Alias(name='Future Synthetic Brand', visibility='public', valid_from=future),
                Alias(name='Old Synthetic Name', visibility='public', valid_from=date(2015, 1, 1), valid_to=date(2020, 1, 1)))
    execution = start()
    queries = [u['query'] for u in execution.read_checkpoint(STAGE)['plan']['units'] if u['source'] == 'europepmc_affiliation']
    assert sorted(queries) == sorted([LEGAL, 'Synthetic Host Biologics', 'Old Synthetic Name'])
    sent = ' '.join(sent_queries(calls))
    assert 'Secret Codename' not in sent and 'Future Synthetic Brand' not in sent


def test_no_confirmed_aliases_keeps_the_legal_name_baseline(entry):
    *_, start = entry
    execution = start()  # legacy project: no subject object was ever confirmed
    queries = [u['query'] for u in execution.read_checkpoint(STAGE)['plan']['units'] if u['source'] == 'openalex_affiliation']
    assert queries == [LEGAL]
    assert 'subject_names' not in execution.read_checkpoint(collection_host.SCOPE)


def test_withdrawn_confirmed_name_stops_the_round_before_further_dispatch(entry):
    pipeline, pid, calls, held, start = entry
    confirm(pipeline, pid, ['Synthetic Host Biologics', 'SynHost'])
    execution = start()
    research = held['research']
    collection_host.guard_scope(research)  # unchanged names: still admitted
    confirm(pipeline, pid, ['Synthetic Host Biologics'])  # owner removes one name mid-round
    before = len(calls)
    with pytest.raises(RuntimeError, match='scope changed'):
        collection_host.guard_scope(research)
    assert len(calls) == before
    assert execution.read_checkpoint(collection_host.SCOPE)['subject_names'] == [LEGAL, 'Synthetic Host Biologics', 'SynHost']
