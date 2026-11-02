"""A leased collect job on an isolated store, and synthetic provider answers. No network."""
import json
from urllib.parse import urlsplit

import pytest
from test_collection_pages import work

from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Response
from project_watch.studio.settings import ModelConfig


def setup(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    pipeline._test_app = app
    settings = pipeline.settings_store.load()
    settings.model = ModelConfig(base_url='https://model.example.invalid/v1', model='synthetic', api_key='synthetic')
    pipeline.settings_store.save(settings)
    project = pipeline.repository.create_project('Synthetic Host')
    pipeline.store.upsert_project(project.id, {'legal_name': 'Synthetic Host'}, consent=True)
    pipeline.store.enqueue(project.id, 'collect', input_version='manual:fixture')
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline._execution_state.current = execution
    return pipeline, execution


def response(url):
    host = urlsplit(url).hostname
    if host == 'api.openalex.org':
        return Response(200, {}, json.dumps({'results': [work()], 'meta': {'count': 1}}).encode())
    if host == 'www.cninfo.com.cn':
        return Response(200, {}, b'{"announcements":[],"totalAnnouncement":0}')
    if host == 'www.ebi.ac.uk':
        return Response(200, {}, b'{"hitCount":0,"resultList":{"result":[]}}')
    pytest.fail('unexpected provider dispatch: ' + host)


# Older tests call the former pytest fixture through __wrapped__.
setup.__wrapped__ = setup


def host(tmp_path):
    """A collect job's execution, one clinical unit and its ingest (no DSH harness)."""
    from types import SimpleNamespace

    from test_collection_plan import candidate, plan

    from project_watch.studio.collection_ingest import CollectionIngest

    pipeline, execution = setup(tmp_path)
    research = SimpleNamespace(pipeline=pipeline, execution=execution, project_id=execution.job['project_id'],
                               store=pipeline.store, guard=lambda: None)
    p = plan([candidate() | {'source': 'clinical_term', 'category': 'trial'}],
             round_id=execution.job['id'], project_id=research.project_id)
    return research, p['units'][0], CollectionIngest(execution)


host.__wrapped__ = host
