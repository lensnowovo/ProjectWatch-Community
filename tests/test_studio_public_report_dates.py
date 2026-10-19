"""Synthetic report/occurrence date contract; all provider access is denied."""
import socket
from types import SimpleNamespace

import pytest

from project_watch.studio.public_profile import build_public_profile


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError('public date regression must remain offline')

    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def compile_fact(text, *, body='合成研究公司专注光学模组。', published='2025-03-12',
                 date='', hits=None, refs=None, evidence=None, project_id=None):
    source = {'title': '合成研究公司', 'url': 'https://synthetic.example/first',
              'body': body, 'published_at': published}

    class Model:
        ready = True
        calls = 0

        def json(self, *_args, **_kwargs):
            self.calls += 1
            return {'facts': [{'kind': 'person', 'label': '研究人员', 'text': text, 'date': date,
                              'refs': refs or [{'ref': 'P1', 'quote': body}]}]}

    model = Model()
    project = {'name': '合成研究公司', 'legal_name': '合成研究公司'}
    if project_id:
        project['id'] = project_id
    result = build_public_profile(project, SimpleNamespace(has_search=False), model,
                                  hits or [source], evidence=evidence)
    assert model.calls == 1
    return result


@pytest.mark.parametrize('qualifier', [
    '据2025年3月报道', '据2025年3月12日报道', '根据2025年3月的报道',
    '截至2025年3月报道', '据2025-03-12报道', '据2025年报道',
])
def test_explicit_report_time_can_use_the_retained_reference_metadata(qualifier):
    text = f'合成研究公司专注光学模组（{qualifier}）'
    result = compile_fact(text)
    assert [fact['text'] for fact in result['facts']] == [text]
    assert result['facts'][0]['date'] == ''
    assert result['omitted_numeric_clauses'] == 0


@pytest.mark.parametrize('published,text', [
    ('2025-03-12', '据2025年4月报道，公司专注光学模组'),
    ('2025-03-12', '据2025年3月13日报道，公司专注光学模组'),
    ('2025-02-30', '据2025年2月报道，公司专注光学模组'),
    ('2100-03-12', '据2100年3月报道，公司专注光学模组'),
    ('not-a-date', '据2025年3月报道，公司专注光学模组'),
    (None, '据2025年3月报道，公司专注光学模组'),
])
def test_invalid_future_unknown_or_different_metadata_cannot_support_report_time(published, text):
    result = compile_fact(text, published=published)
    assert result['facts'] == []
    assert result['omitted_numeric_clauses'] == 1


@pytest.mark.parametrize('text', [
    '据2025年3月报道，公司在2025年3月完成任命',
    '据2025年3月报道，公司收入2025万元',
    '据2025年3月报道，公司在3月完成任命',
    '据2025年3月报道，公司聘任3人',
    '截至2025年3月公司专注光学模组',
    '公司在2025年3月获批产品',
])
def test_report_time_does_not_support_other_occurrences_of_the_same_figures(text):
    result = compile_fact(text)
    assert result['facts'] == [] and result['omitted_numeric_clauses'] == 1


def test_valid_body_event_time_and_metadata_report_time_can_coexist():
    text = '2024年10月任命甲为负责人（据2025年3月报道）'
    result = compile_fact(text, body='合成研究公司在2024年10月任命甲为负责人。', date='2024-10')
    assert result['facts'][0]['text'] == text
    assert result['facts'][0]['date'] == '2024-10'
    assert result['omitted_occurrence_dates'] == 0


@pytest.mark.parametrize('invalid_ref', [False, True])
def test_unretained_reference_metadata_cannot_supply_report_date(invalid_ref):
    body = '合成研究公司专注光学模组。'
    hits = [{'title': '合成研究公司', 'url': f'https://synthetic.example/{i}', 'body': body,
             'published_at': published} for i, published in enumerate(['2025-03-12', '2024-10-01'])]
    refs = [{'ref': 'P1', 'quote': body}]
    if invalid_ref:
        refs.append({'ref': 'P2', 'quote': '没有这段正文'})
    result = compile_fact('据2024年10月报道，公司专注光学模组', hits=hits, refs=refs)
    assert result['facts'] == []


def test_fourth_valid_reference_metadata_is_not_available_after_reference_bound():
    body = '合成研究公司专注光学模组。'
    hits = [{'title': '合成研究公司', 'url': f'https://synthetic.example/{i}', 'body': body,
             'published_at': '2025-09-12' if i < 3 else '2025-08-12'} for i in range(4)]
    refs = [{'ref': f'P{i}', 'quote': body} for i in range(1, 5)]
    result = compile_fact('据2025年8月报道，公司专注光学模组', hits=hits, refs=refs)
    assert result['facts'] == []


@pytest.mark.parametrize('requested,body', [
    ('2025-03-12', '合成研究公司专注光学模组。'),
    ('2025-03-12', '合成研究公司收入2025万元。'),
    ('2025-03-12', '合成研究公司于2025年成立。'),
    ('2025-03-12', '合成研究公司于2025年3月任命甲。'),
    ('2025-03-13', '合成研究公司于2025年3月12日任命甲。'),
    ('2025-02-30', '合成研究公司于2025年2月30日任命甲。'),
])
def test_occurrence_date_requires_body_date_not_metadata_or_numeric_coincidence(requested, body):
    result = compile_fact('公司专注光学模组', body=body, date=requested)
    assert result['facts'][0]['date'] == ''
    assert result['omitted_occurrence_dates'] == 1


@pytest.mark.parametrize('requested,source_date', [
    ('2024-10-12', '2024年10月12日'), ('2024-10', '2024年10月12日'),
    ('2024', '2024年10月12日'), ('2024年10月底', '2024年10月底'),
    ('2024-10', '2024年10月底'), ('2024-10-12', '2024-10-12'),
])
def test_explicit_body_dates_may_keep_or_reduce_but_not_increase_precision(requested, source_date):
    result = compile_fact('甲出任负责人', body=f'合成研究公司在{source_date}任命甲为负责人。', date=requested)
    assert result['facts'][0]['date'] == requested
    assert result['omitted_occurrence_dates'] == 0


def test_month_end_cannot_be_invented_from_a_month_only_source():
    result = compile_fact('甲出任负责人', body='合成研究公司在2024年10月任命甲为负责人。', date='2024年10月底')
    assert result['facts'][0]['date'] == '' and result['omitted_occurrence_dates'] == 1


@pytest.mark.parametrize('requested,source_date', [
    ('2025-00', '2025年0月'), ('2025-03-00', '2025年3月0日'),
    ('2025年0月0日', '2025年0月0日'),
])
def test_zero_month_or_day_is_not_an_explicit_valid_date(requested, source_date):
    result = compile_fact('甲出任负责人', body=f'合成研究公司在{source_date}任命甲。', date=requested)
    assert result['facts'][0]['date'] == '' and result['omitted_occurrence_dates'] == 1


@pytest.mark.parametrize('reporting', ['据2025年3月12日报道', '发布日期：2025-03-12'])
def test_explicit_reporting_date_in_body_does_not_become_an_occurrence_date(reporting):
    result = compile_fact('甲出任负责人', body=f'{reporting}，合成研究公司任命甲为负责人。', date='2025-03-12')
    assert result['facts'][0]['date'] == '' and result['omitted_occurrence_dates'] == 1


def test_fourth_reference_cannot_supply_occurrence_date_either():
    body = '合成研究公司专注光学模组。'
    hits = [{'title': '合成研究公司', 'url': f'https://synthetic.example/{i}',
             'body': body + ('2024年10月12日任命甲。' if i == 3 else '')} for i in range(4)]
    result = compile_fact('甲出任负责人', hits=hits, date='2024-10-12',
                          refs=[{'ref': f'P{i}', 'quote': body} for i in range(1, 5)])
    assert result['facts'][0]['date'] == '' and result['omitted_occurrence_dates'] == 1


def test_empty_source_result_has_empty_date_omission_receipt():
    class NotReady:
        ready = False

    result = build_public_profile({'name': '合成研究公司'}, SimpleNamespace(has_search=False), NotReady(), [])
    assert result['omitted_occurrence_dates'] == 0


def test_restored_report_time_preserves_exact_evidence_offsets(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.monitoring_contracts import LocatedRef
    from project_watch.studio.store import StudioStore

    db = tmp_path / 'synthetic.sqlite3'
    project = ProjectRepository(db).create_project('合成研究公司')
    store = StudioStore(db)
    result = compile_fact('公司专注光学模组（据2025年3月报道）', evidence=store.evidence, project_id=project.id)
    ref = result['facts'][0]['refs'][0]
    located = LocatedRef(**{k: ref[k] for k in ('evidence_version_id', 'part_index', 'start', 'end', 'quote')})
    assert store.evidence.resolve(project.id, located, include_internal=False)['quote'] == ref['quote']
    assert ref['published_at'] == '2025-03-12'


def test_publication_metadata_is_versioned_separately_without_rewriting_the_excerpt(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.monitoring_contracts import LocatedRef
    from project_watch.studio.store import StudioStore

    db = tmp_path / 'synthetic.sqlite3'
    project = ProjectRepository(db).create_project('合成研究公司')
    store = StudioStore(db)
    first = compile_fact('据2025年3月报道，公司专注光学模组', evidence=store.evidence, project_id=project.id)
    ref = first['facts'][0]['refs'][0]
    record = store.evidence.get(project.id, ref['evidence_version_id'])
    assert record['parts'][0]['text'] == '合成研究公司专注光学模组。'
    publication = LocatedRef(**ref['publication_ref'])
    assert publication.evidence_version_id == ref['evidence_version_id']
    assert publication.part_index == 1 and publication.quote == '2025-03-12'
    assert store.evidence.resolve(project.id, publication)['quote'] == '2025-03-12'
    second = compile_fact('据2025年4月报道，公司专注光学模组', published='2025-04-12',
                          evidence=store.evidence, project_id=project.id)
    second_ref = second['facts'][0]['refs'][0]
    assert second_ref['evidence_version_id'] != ref['evidence_version_id']
    reopened = StudioStore(db)
    assert reopened.evidence.get(project.id, ref['evidence_version_id']) == record
    assert reopened.evidence.resolve(project.id, publication)['quote'] == '2025-03-12'


def test_invalid_metadata_adds_no_valid_publication_ref(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore

    db = tmp_path / 'synthetic.sqlite3'
    project = ProjectRepository(db).create_project('合成研究公司')
    store = StudioStore(db)
    result = compile_fact('公司专注光学模组', published='2025-02-30', evidence=store.evidence, project_id=project.id)
    ref = result['facts'][0]['refs'][0]
    assert 'publication_ref' not in ref
    assert '日期未知' in store.evidence.get(project.id, ref['evidence_version_id'])['parts'][1]['text']


def test_pipeline_fact_includes_exact_publication_citation_not_only_body(tmp_path, monkeypatch):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    path = tmp_path / 'synthetic.sqlite3'
    repo = ProjectRepository(path)
    project = repo.create_project('合成研究公司')
    store = StudioStore(path)
    store.upsert_project(project.id, {'legal_name': '合成研究公司'}, consent=False)
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / 'unused-synthetic-settings.json'))
    body = '合成研究公司专注光学模组。'
    hit = {'title': '合成研究公司', 'body': body, 'url': 'https://synthetic.example/a', 'published_at': '2025-03-12'}

    class Model:
        ready = True

        def json(self, *_args, **_kwargs):
            return {'facts': [{'kind': 'business', 'text': '据2025年3月报道，公司专注光学模组',
                              'refs': [{'ref': 'P1', 'quote': body}]}]}

    monkeypatch.setattr(pipeline, 'tools', lambda: (None, None, Model(), SimpleNamespace(has_search=False)))
    monkeypatch.setattr(store, 'list_items', lambda **kwargs: [hit])
    job = store.enqueue(project.id, 'public_profile')
    pipeline.run_public_profile(job)
    fact = pipeline.objects.facts(project.id, pipeline.objects.ensure_subject(project.id)['id'])[0]
    assert [r['part_index'] for r in fact['refs']] == [0, 1]
    assert fact['refs'][1]['quote'] == '2025-03-12'
    assert fact['visibility'] == 'public'
