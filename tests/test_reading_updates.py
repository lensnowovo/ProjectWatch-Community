from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

from test_company_state import material, publish, sample

from project_watch.company_state import CompanyState, input_packets
from project_watch.reading_updates import reading_updates


def test_single_source_is_not_a_change_and_new_material_keeps_both_versions(tmp_path):
    repo, project, source, packet, output = sample(tmp_path)
    publish(repo, source, packet, output)
    before = CompanyState(repo).read(project.id)
    assert reading_updates(project.id, [source], before)['items'] == []
    later = material(repo, project, '公司新增光学检测模组业务。', 'c'*64)
    later_packet = input_packets(repo, later)[0]
    updated = deepcopy(output)
    updated['items'][0].update(text='公司新增光学检测模组业务。', evidence=[{
        'ref_id': later_packet['fragments'][0]['ref_id'], 'quote': '公司新增光学检测模组业务。'}])
    publish(repo, later, later_packet, updated)
    compiled = CompanyState(repo).read(project.id)
    result = reading_updates(project.id, [source, later], compiled)
    item = result['items'][0]
    assert item['status'] == 'unreviewed'
    assert item['change_kind'] == 'material_statement'
    assert item['current'][0]['text'] == updated['items'][0]['text']
    assert item['previous'][0]['text'] == output['items'][0]['text']
    assert item['business_change_verified'] is False
    assert item['occurred_at'] is None
    assert reading_updates('other', [source, later], compiled)['items'] == []
    assert reading_updates(project.id, [source], compiled)['items'] == []
    assert reading_updates(project.id, [source, replace(later, content_sha256='e'*64)], compiled)['items'] == []
    assert result == reading_updates(project.id, [later, source], {**compiled, 'batches':list(reversed(compiled['batches']))})
    bad = deepcopy(compiled)
    for batch in bad['batches']:
        if batch['source_id'] == later.id:
            batch['items'][0]['source_refs'][0]['source_id'] = source.id
    assert reading_updates(project.id, [source, later], bad)['items'] == []
    # Compilation finish time must not redefine document receipt order.
    compiled['batches'][-1]['recorded_at'] = (later.created_at+timedelta(days=10)).isoformat()
    assert reading_updates(project.id, [source, later], compiled)['items'][0]['current_source']['id'] == later.id


def test_rephrasing_same_quote_does_not_make_a_new_business_update(tmp_path):
    repo, project, source, packet, output = sample(tmp_path)
    publish(repo, source, packet, output)
    later = material(repo, project, packet['fragments'][0]['text']+'补充页。', 'd'*64)
    later_packet = input_packets(repo, later)[0]
    updated = deepcopy(output)
    updated['items'][0].update(text='工业相机由公司生产。')
    updated['items'][0]['evidence'][0]['ref_id'] = later_packet['fragments'][0]['ref_id']
    publish(repo, later, later_packet, updated)
    assert reading_updates(project.id, [source, later], CompanyState(repo).read(project.id))['items'] == []


def test_same_source_multiple_packets_are_not_multiple_documents(tmp_path):
    repo, project, source, packet, output = sample(tmp_path)
    publish(repo, source, packet, output)
    compiled = CompanyState(repo).read(project.id)
    duplicate = deepcopy(compiled['batches'][0])
    duplicate['id'] = 'other-packet'
    duplicate['items'][0]['text'] = '同一文件的另一段内容。'
    compiled['batches'].append(duplicate)
    assert reading_updates(project.id, [source], compiled)['items'] == []
