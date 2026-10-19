"""Unsupported schemes must not consume original-reading slots; no real network."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_studio_competition_research import research_pipeline

from project_watch.studio.execution import JobExecution


def prepare(tmp_path):
    pipeline, pid, _, reads = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    execution = JobExecution(pipeline.store, pipeline.store.claim(), budgets={'page': 3})
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'article_body', 'text': 'Synthetic Optics publishes product specifications.'})
    return pipeline, pid, reads, execution


def hit(url):
    return {'url': url, 'title': 'Public product', 'snippet': 'Synthetic Optics product specifications'}


@pytest.mark.parametrize('unsupported', [
    ['http://alpha.test/0'],
    ['http://alpha.test/0', 'ftp://alpha.test/1', 'http://beta.test/2'],
])
def test_ineligible_leads_do_not_displace_three_readable_results(tmp_path, unsupported):
    pipeline, pid, reads, execution = prepare(tmp_path)
    urls = [f'https://alpha.test/{i}' for i in range(4)]
    hits = [hit(url) for url in [*unsupported, *urls]]
    before = deepcopy(hits)
    result = pipeline.competition_research.originals(pid, hits, execution=execution)
    assert reads == urls[:3]
    assert len(result) == 3
    assert execution.usage()['page']['requests'] == 3
    assert hits == before
    assert pipeline.competition_research.originals(pid, hits, execution=execution) == result
    assert reads == urls[:3]


def test_all_unsupported_leads_produce_no_read_or_page_receipt(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    hits = [hit(url) for url in ['http://alpha.test/0', 'ftp://alpha.test/1',
        'https://user:password@alpha.test/2', 'javascript:alert(1)']]
    assert pipeline.competition_research.originals(pid, hits, execution=execution) == []
    assert reads == []
    assert execution.usage() == {}


def test_rejection_does_not_invent_https_equivalent(tmp_path):
    pipeline, pid, reads, execution = prepare(tmp_path)
    result = pipeline.competition_research.originals(pid, [hit('http://alpha.test/report'),
        hit('https://beta.test/discovered')], execution=execution)
    assert reads == ['https://beta.test/discovered']
    assert len(result) == 1
