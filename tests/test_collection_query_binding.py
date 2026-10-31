"""Missing identifiers may only resolve against unique already exposed evidence."""
import json
from types import SimpleNamespace

import pytest
from test_bootstrap_collection import install, run

from project_watch.studio import bootstrap_collection, collection_dsh


def test_ordinary_professional_queries_bind_exact_exposed_sources(tmp_path, monkeypatch):
    from project_watch.studio.research_sources import research_sources

    pipeline, execution, _seen = install(tmp_path, monkeypatch, mode='professional', omit_source_id=True)
    result = run(pipeline, execution)
    assert result['records'] == 2
    assert research_sources(pipeline.store, execution.job['project_id'], 'paper')['total'] == 1
    assert research_sources(pipeline.store, execution.job['project_id'], 'patent')['total'] == 1
    before = execution.usage()
    assert run(pipeline, execution) == result
    assert execution.usage() == before


@pytest.mark.parametrize('condition', ['ambiguous', 'unexposed', 'withdrawn', 'inexact'])
def test_unqualified_quote_never_creates_binding(condition):
    sources = {str(i): dict(id=str(i), evidence_version_id=str(i), excerpt='A Laboratory disclosed X.') for i in (1, 2)}
    writes = []
    role = SimpleNamespace(task=json.dumps({'sources': list(sources.values()) if condition == 'ambiguous' else
                                          [] if condition == 'unexposed' else [sources['1']]}),
        parent=SimpleNamespace(source=lambda sid: sources[sid]),
        execution=SimpleNamespace(read_checkpoint=lambda key: [], checkpoint_chunked=lambda *a: writes.append(a)),
        available=lambda source: condition != 'withdrawn')
    quote = 'A Laboratory disclosed X.' if condition != 'inexact' else 'A Laboratory has disclosed X.'
    with pytest.raises(ValueError):
        bootstrap_collection.Collection.locate_collect_quote(role, {'query': 'A Laboratory', 'quote': quote})
    assert not writes


def test_first_view_preserves_saved_sources_and_publisher_order(monkeypatch):
    entries = [dict(id=str(i), url='https://patents.example.invalid/' + str(i)) for i in range(9)]
    entries += [dict(id='paper', url='https://papers.example.invalid/one'), dict(id='company', url='https://company.example.invalid/')]
    monkeypatch.setattr(collection_dsh, 'catalog', lambda _, offset, limit: dict(sources=entries[offset:offset+limit],
        total=len(entries), next_offset=None))
    result = collection_dsh.discovery_catalog(None)
    assert result['sources'][:3] == [entries[0], entries[9], entries[10]]
    assert len(result['sources']) == 8 and result['next_offset'] == 0
    assert all(s in entries for s in result['sources'])
