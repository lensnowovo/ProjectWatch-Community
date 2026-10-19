from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_company_state import material

from project_watch.external_context import attach_internal_context
from project_watch.external_focus import ExternalFocusStore, external_reading
from project_watch.monitoring import FeedItem, ManualSnapshotAdapter, MonitoringRunner
from project_watch.repository import ProjectRepository


def setup(tmp_path, term='Synthetic Peer', category='competitors'):
    repo = ProjectRepository(tmp_path / 'watch.sqlite3')
    project = repo.create_project('Synthetic Optics')
    plan = ExternalFocusStore(repo).create(project.id, category=category, term=term, provider='gdelt')
    repo.set_monitoring_plan_enabled(plan.id, True)
    feed = FeedItem('one', f'{term} announces product trial', 'https://example.org/trial', '', datetime.now(UTC))
    MonitoringRunner(repo, {'rss': ManualSnapshotAdapter({plan.target: (feed,)})}).run(plan.id, idempotency_key='one')
    return repo, project


def story(repo, project):
    return next(g for g in external_reading(repo, project.id)['categories'] if g['items'])['items'][0]


def test_local_original_evidence_not_causal_inference_and_restart(tmp_path):
    repo, project = setup(tmp_path)
    source = material(repo, project, '合成材料。2025 年关注 Synthetic Peer 的产品试验；尚未确认双方存在竞争关系。')
    item = story(repo, project)
    context = item['internal_context']
    assert context['status'] == 'shared_term_only'
    assert context['relationship_verified'] is False
    assert context['impact'] is None
    assert context['external_published_at'] == item['published_at'].isoformat()
    ref = context['materials'][0]
    assert ref['source_id'] == source.id
    assert ref['quote'] in repo.list_fragments(source.id)[0].text
    assert '尚未确认' in ref['quote']
    assert ref['received_at'] == source.created_at.isoformat()
    assert ref['material_period'] is None  # Never turn receipt/publication into an event period.
    assert ref['version'] == source.content_sha256
    assert '同一主体' in context['boundary']
    assert story(ProjectRepository(repo.db_path), project)['internal_context'] == context
    assert repo.list_observations(project.id) == []


@pytest.mark.parametrize('term,text', [
    ('Synthetic Peer', 'Synthetic Peerless 正在发布产品。'),
    ('AI', '公司研究 AI 系统。'),
    ('Synthetic Peer', '文件名称提到竞品，但正文没有对应名称。'),
])
def test_short_terms_and_substrings_do_not_create_material_links(tmp_path, term, text):
    repo, project = setup(tmp_path, term)
    material(repo, project, text)
    assert story(repo, project)['internal_context']['materials'] == []


def test_cross_project_sources_and_fragments_are_excluded(tmp_path):
    repo, project = setup(tmp_path)
    other = repo.create_project('Other Synthetic')
    foreign = material(repo, other, 'Synthetic Peer 私有资料。')
    assert story(repo, project)['internal_context']['materials'] == []
    local = material(repo, project, '本项目材料。', 'b'*64)
    groups = [{'items': [{'title': 'Synthetic Peer news', 'summary': '', 'match_term': 'Synthetic Peer',
                          'published_at': datetime.now(UTC), 'category': 'competitors'}]}]
    # Defense against an accidental cross-project repository join.
    repo.list_fragments = lambda source_id: [replace(repo_fragment, source_id=local.id)]
    repo_fragment = ProjectRepository(repo.db_path).list_fragments(foreign.id)[0]
    attach_internal_context(repo, project.id, groups)
    assert groups[0]['items'][0]['internal_context']['materials'] == []


def test_bounded_distinct_materials_and_live_recalculation(tmp_path):
    repo, project = setup(tmp_path)
    sources = [material(repo, project, f'第 {i} 期：Synthetic Peer 尚待核实。', str(i)*64) for i in range(5)]
    result = story(repo, project)['internal_context']
    assert len(result['materials']) == 3
    assert result['total_materials'] == 5
    assert result['materials'][0]['source_id'] == sources[-1].id
    repo.list_sources = lambda project_id: sources[:1]
    updated = story(repo, project)['internal_context']
    assert updated['total_materials'] == 1
    assert updated['materials'][0]['source_id'] == sources[0].id


def test_changed_fragment_and_policy_applicability_remain_unverified(tmp_path):
    repo, project = setup(tmp_path, 'Device rules', 'policy')
    source = material(repo, project, 'Device rules 的地域与生效条件尚未核实。')
    result = story(repo, project)['internal_context']
    assert '适用' in result['boundary']
    fragment = repo.list_fragments(source.id)[0]
    repo.list_fragments = lambda source_id: [replace(fragment, text='已移除旧内容。')]
    assert story(repo, project)['internal_context']['materials'] == []
