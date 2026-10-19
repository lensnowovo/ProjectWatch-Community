"""Synthetic subject/role/source cases; no provider calls or private materials."""
from datetime import date

import pytest
from test_studio_race import build, ev

from project_watch.studio import race


@pytest.mark.parametrize('suffix', ['客户', '供应商', '子公司', '母公司', '竞争对手', '控股股东'])
def test_counterpart_is_not_company_actor(suffix):
    title = f'合成视界的{suffix}完成3亿元B轮融资'
    row = build(company_events=[ev(title, '2026-10-06')])['rows'][0]
    assert row['funding'] is None
    assert row['move'] is None


def test_long_exact_legal_name_is_not_cut_off():
    name = '合成超长名称汽车连接系统科技股份有限公司'
    assert race._leads(f'【快讯】{name}获车企定点', [name])
    row = build(project={'name': name, 'legal_name': name, 'industry': '连接器'},
                company_events=[ev(f'{name}获车企定点', '2026-10-06')])['rows'][0]
    assert row['stage']['label'] == '定点'


def test_prefix_of_other_legal_entity_is_not_actor():
    title = '合成视界汽车制造有限公司完成3亿元B轮融资'
    row = build(company_events=[ev(title, '2026-10-06')])['rows'][0]
    assert row['funding'] is None
    assert row['move'] is None


def test_exact_known_legal_name_still_matches():
    row = build(company_events=[ev('合成视界科技有限公司完成3亿元B轮融资', '2026-10-06')])['rows'][0]
    assert row['funding']['round'] == 'B轮'


@pytest.mark.parametrize('title', ['ABCDevices完成B轮融资', 'ABC2完成B轮融资'])
def test_latin_alias_needs_token_boundary(title):
    assert not race._leads(title, ['ABC'])


@pytest.mark.parametrize('title', ['ABC 完成B轮融资', '合成视界XG-500获车企定点', '合成视界905nm产品获车企定点'])
def test_valid_subject_and_product_suffix_preserved(title):
    assert race._leads(title, ['ABC', '合成视界'])


@pytest.mark.parametrize('title', ['合成视界产品出货量10万台', '合成视界交付一台原型机',
                                 '合成视界交付软件测试报告', '合成视界已累计交付100万份资料'])
def test_quantity_or_delivery_alone_does_not_prove_mass_production(title):
    assert race.classify_step(title, 'hardware') != 4
    assert race.classify_step(title, 'hardware') != 5


@pytest.mark.parametrize('title', ['合成视界采购设备完成批量交付', '合成視界启动软件采购项目招标',
                                 '合成视界收到供应商交付的量产设备'])
def test_receiving_or_procurement_is_not_own_commercial_stage(title):
    assert race.classify_step(title, 'hardware') is None
    assert race.classify_step(title, 'software') is None


@pytest.mark.parametrize('title,step', [('合成视界实现量产交付', 4), ('合成视界完成小批量供货', 4),
                                     ('合成视界实现大批量生产', 5), ('合成视界中标某车企采购项目', 3)])
def test_explicit_supplier_progress_remains(title, step):
    assert race.classify_step(title, 'hardware') == step


@pytest.mark.parametrize('url,source', [('https://news.example/a', '公司官网'),
                                     ('https://cde.org.cn.evil.example/a', '官方'),
                                     ('ftp://www.cninfo.com.cn/a', '公示'),
                                     ('https://user@www.cninfo.com.cn/a', '公示')])
def test_untrusted_labels_and_unsafe_hosts_do_not_upgrade_source(url, source):
    assert race.grade_of(url, source) == '媒体'


def test_public_and_declared_company_hosts_remain_classified():
    assert race.grade_of('https://www.cninfo.com.cn/a') == '公示'
    assert race.grade_of('https://news.hecheng.example/a', own_hosts=('hecheng.example',)) == '公司宣传'
    assert race.grade_of('https://hecheng.example.evil.example/a', own_hosts=('hecheng.example',)) == '媒体'


def test_quantity_counterpart_does_not_become_company_observation():
    title = '合成视界的客户2026年Q1 中国ADAS“激光雷达”产品出货量10万台'
    data = build(company_events=[ev(title, '2026-09-01')])
    assert data['quantities']['records'] == []


def test_shipment_quantity_retained_without_stage_upgrade():
    title = '合成视界2026年Q1 中国ADAS“激光雷达”产品出货量10万台'
    data = build(company_events=[ev(title, '2026-09-01')], today=date(2026, 10, 10))
    assert len(data['quantities']['records']) == 1
    assert data['rows'][0]['stage'] is None
