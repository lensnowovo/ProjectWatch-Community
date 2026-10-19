"""Compiled concerns reach the profile without making formal watch records."""
from contextlib import closing

from project_watch.company_state import input_packets
from project_watch.monitor_profile import project_profile
from project_watch.repository import ProjectRepository
from tests.test_company_state import material, publish


def compiled(repo, project, text, sha='a'*64, topic='risks', headline='客户验收'):
    source = material(repo, project, text, sha)
    packet = input_packets(repo, source)[0]
    publish(repo, source, packet, {'items': [{'topic': topic, 'headline': headline,
            'text': text, 'evidence': [{'ref_id': packet['fragments'][0]['ref_id'],
                                      'quote': text}], 'as_of': None}]})
    return source


def test_compiled_concern_is_visible_cited_and_not_formally_adopted(tmp_path):
    repo = ProjectRepository(tmp_path/'watch.sqlite3')
    project = repo.create_project('Synthetic optics')
    source = compiled(repo, project, '客户试用不等于订单，需核实验收条件与收入确认依据。')
    focus = project_profile(repo, project.id)['focus']
    assert len(focus) == 1
    assert focus[0]['text'] == '客户试用不等于订单，需核实验收条件与收入确认依据。'
    assert focus[0]['origin'] == 'ai_synthesis' and focus[0]['review_status'] == 'unreviewed'
    assert focus[0]['source_refs'][0]['source_id'] == source.id
    assert focus[0]['source_refs'][0]['version'] == source.content_sha256
    assert repo.list_watch_items(project.id) == []
    assert repo.list_observations(project.id) == []
    assert repo.monitor_profile_history(project.id) == []


def test_human_clear_survives_new_material_restart_and_explicit_restore(tmp_path):
    repo = ProjectRepository(tmp_path/'watch.sqlite3')
    project = repo.create_project('Synthetic optics')
    compiled(repo, project, '客户验收尚未完成。')
    repo.edit_monitor_profile(project.id, expected_version=0, focus=[])
    compiled(repo, project, '注册检验计划尚待确认。', 'b'*64, 'milestones', '注册检验')
    reopened = ProjectRepository(repo.db_path)
    p = project_profile(reopened, project.id)
    assert p['focus'] == [] and len(p['suggested_focus']) == 2
    reopened.edit_monitor_profile(project.id, expected_version=1, restore_focus=True)
    assert len(project_profile(reopened, project.id)['focus']) == 2


def test_mutated_evidence_hides_suggestion_and_other_project_is_isolated(tmp_path):
    repo = ProjectRepository(tmp_path/'watch.sqlite3')
    project = repo.create_project('Synthetic optics')
    source = compiled(repo, project, '客户验收尚未完成。')
    assert project_profile(repo, project.id)['focus']
    assert project_profile(repo, repo.create_project('Other').id)['focus'] == []
    with closing(repo._connect()) as db, db:
        db.execute("UPDATE document_fragments SET text='更正内容' WHERE source_id=?", (source.id,))
    assert project_profile(repo, project.id)['focus'] == []


def test_long_concern_keeps_full_detail_without_exceeding_edit_limit(tmp_path):
    repo = ProjectRepository(tmp_path/'watch.sqlite3')
    project = repo.create_project('Synthetic optics')
    text = '客户验收条件尚待核实。'*30
    compiled(repo, project, text)
    focus = project_profile(repo, project.id)['focus'][0]
    assert focus['text'] == '客户验收' and focus['detail'] == text


def test_duplicate_concerns_merge_sources_and_ordinary_descriptions_are_not_tasks(tmp_path):
    repo = ProjectRepository(tmp_path/'watch.sqlite3')
    project = repo.create_project('Synthetic optics')
    compiled(repo, project, '客户验收尚未完成。')
    compiled(repo, project, '客户验收尚未完成。', 'b'*64)
    compiled(repo, project, '公司生产工业相机。', 'c'*64, 'business', '公司业务')
    focus = project_profile(repo, project.id)['focus']
    assert len(focus) == 1 and len(focus[0]['source_refs']) == 2


def test_rejected_watch_evidence_does_not_return_as_ai_concern(tmp_path):
    repo = ProjectRepository(tmp_path/'watch.sqlite3')
    project = repo.create_project('Synthetic optics')
    source = compiled(repo, project, '客户验收尚未完成。')
    observation = repo.add_observation(project.id, source_id=source.id, subject='风险与待核实',
            statement='客户验收尚未完成。', source_locator='page:1')
    repo.confirm_observation(observation.id, reviewer='synthetic', expected_version=1)
    watch = repo.propose_watch_item(project.id, observation_id=observation.id,
                                   title='客户验收', question='验收是否完成？')
    repo.review_watch_item(project.id, watch.id, action='reject', expected_version=1, reviewer='synthetic')
    assert project_profile(repo, project.id)['focus'] == []


def test_resolved_watch_does_not_revive_its_original_risk_after_new_evidence(tmp_path):
    repo = ProjectRepository(tmp_path/'watch.sqlite3')
    project = repo.create_project('Synthetic optics')
    source = compiled(repo, project, '客户验收尚未完成。')
    old = repo.add_observation(project.id, source_id=source.id, subject='风险与待核实',
                              statement='客户验收尚未完成。', source_locator='page:1')
    repo.confirm_observation(old.id, reviewer='synthetic', expected_version=1)
    watch = repo.propose_watch_item(project.id, observation_id=old.id,
                                   title='客户验收', question='验收是否完成？')
    watch = repo.review_watch_item(project.id, watch.id, action='watch', expected_version=1, reviewer='synthetic')
    new_source = material(repo, project, '客户已经完成验收。', 'b'*64)
    new = repo.add_observation(project.id, source_id=new_source.id, subject='客户',
                              statement='客户已经完成验收。', source_locator='page:1')
    repo.confirm_observation(new.id, reviewer='synthetic', expected_version=1)
    change = repo.propose_watch_item_change(project.id, watch.id, expected_version=watch.version,
            target_status='resolved', question=watch.question, owner=None, due_on=None,
            reason='合成验收完成', evidence_observation_id=new.id)
    repo.review_watch_item_change(project.id, watch.id, change.id, action='accept',
                                  expected_version=watch.version, reviewer='synthetic')
    assert project_profile(repo, project.id)['focus'] == []


def test_compiled_focus_has_a_small_reading_budget(tmp_path):
    repo = ProjectRepository(tmp_path/'watch.sqlite3')
    project = repo.create_project('Synthetic optics')
    for i in range(7):
        compiled(repo, project, f'合成客户{i}的验收尚未完成。', str(i)*64)
    assert len(project_profile(repo, project.id)['focus']) == 5
