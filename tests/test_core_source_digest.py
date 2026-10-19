"""Core source extraction remains useful; uncertainty cannot become empty success."""

from types import SimpleNamespace

import pytest

from project_watch.studio import source_digest
from project_watch.studio.http import HttpError
from project_watch.studio.llm import ModelError


def empty_store():
    return SimpleNamespace(list_items=lambda **kwargs: [], list_events=lambda **kwargs: [])


@pytest.mark.parametrize('code', ['request_outcome_unknown', 'connection_failed', 'request_budget', 'provider_backoff'])
def test_gather_propagates_uncertainty_and_execution_holds(code):
    def search(*args, **kwargs):
        raise HttpError(code)
    with pytest.raises(HttpError, match=code):
        source_digest.gather(empty_store(), SimpleNamespace(has_search=True, web_search=search),
                             {'id': 'synthetic'}, {'industry_queries': ['合成光学']}, lambda name: None)


def test_all_digest_searches_fail_cannot_report_empty_success():
    def search(*args, **kwargs):
        raise HttpError('http_failure', status=401)
    with pytest.raises(HttpError):
        source_digest.gather(empty_store(), SimpleNamespace(has_search=True, web_search=search),
                             {'id': 'synthetic'}, {'industry_queries': ['合成光学']}, lambda name: None)


def test_cited_industry_facts_and_metrics_exist_without_investment_verdict():
    evidence = source_digest.Evidence()
    quote = '2026年合成光学行业营业收入1200万元'
    evidence.add(title='合成行业公告', url='https://synthetic.example/industry', source='合成发布方',
                 date='2026-01-05', text=quote, kind='industry')
    refs = [{'ref': 'E1', 'quote': quote}]
    llm = SimpleNamespace(json=lambda *args, **kwargs: {'overview': quote, 'overview_refs': refs,
        'facts': [{'kind': '披露', 'text': quote, 'refs': refs}], 'metrics': [
            {'name': '营业收入', 'period': '2026', 'value': 1200, 'unit': '万元', 'refs': refs}],
        'heat': '高', 'implications': ['不应成为 Core 判断']})
    result = source_digest.industry(llm, evidence, {'name': '合成公司'}, {})
    assert result['overview'] == quote and result['facts'] and result['metrics'][0]['value'] == 1200
    assert result['analysis_available'] is False and result['heat'] is None and not result['implications']


@pytest.mark.parametrize('code', ['request_outcome_unknown', 'model_timeout', 'request_budget', 'provider_backoff'])
def test_partial_digest_cannot_hide_unknown_model_outcome(tmp_path, code):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore
    repo = ProjectRepository(tmp_path / 'synthetic.sqlite3')
    pid = repo.create_project('合成项目').id
    store = StudioStore(repo.db_path)
    store.upsert_project(pid, {}, consent=True)
    def failed(*args, **kwargs):
        raise ModelError('held synthetic request', code=code)
    with pytest.raises(ModelError) as error:
        source_digest.build(store, None, SimpleNamespace(has_search=False), pid, lambda name: None,
            company_builder=lambda *args: {'companies': []}, industry_builder=failed)
    assert error.value.code == code
