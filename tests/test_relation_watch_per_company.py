"""One relation watch per company: further roles join it; lists of names are not watched."""
from test_studio_evidence_versions import sample

from project_watch.studio.app import create_studio_app
from project_watch.studio.monitoring_contracts import FactInput, LocatedRef, ObjectInput
from project_watch.studio.relation_delivery import sync


def project(tmp_path):
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    created = p.repository.create_project('Synthetic Optics')
    p.store.upsert_project(created.id, {'legal_name': created.name}, consent=True)
    return p, created.id, p.objects.ensure_subject(created.id)


def relation(p, pid, subject, name, role, key, stage='order', text=None):
    text = text or f'Synthetic Optics and {name} signed a {role} agreement {key}.'
    e = p.evidence.save(sample(pid, text).model_copy(update={'source_key': 'source-' + key,
                                                           'visibility': 'public', 'read_scope': 'full'}))
    ref = LocatedRef(evidence_version_id=e['id'], part_index=0, start=0, end=len(text), quote=text)
    listed = p.objects.page(pid)
    peers = [o for o in (listed.get('items', []) if isinstance(listed, dict) else listed) if o['name'] == name]
    peer = peers[0] if peers else p.objects.save(
        pid, ObjectInput(kind='company', name=name, visibility='public', refs=(ref,)), actor='automatic')
    p.objects.save_fact(pid, FactInput(subject_id=subject['id'], predicate='business_relation', text=text,
        basis='actual', subject_attribution='explicit', visibility='public', refs=(ref,),
        attributes={'kind': 'business_relation', 'peer_id': peer['id'], 'role': role, 'stage': stage}))
    return peer, e


def relation_watches(p, pid):
    return [w for w in p.monitoring_plan.items(pid) if w['scope_kind'] == 'relation']


def test_a_second_role_of_the_same_company_joins_its_watch(tmp_path):
    p, pid, subject = project(tmp_path)
    relation(p, pid, subject, 'Customer Beta', 'customer', 'a')
    assert sync(p, pid)['added'] == 1
    relation(p, pid, subject, 'Customer Beta', 'supplier', 'b')
    assert sync(p, pid)['added'] == 0
    watch, = relation_watches(p, pid)
    assert watch['status'] != 'retired'


def test_the_shared_watch_retires_only_when_every_role_is_gone(tmp_path):
    p, pid, subject = project(tmp_path)
    _, first = relation(p, pid, subject, 'Customer Beta', 'customer', 'a')
    sync(p, pid)
    _, second = relation(p, pid, subject, 'Customer Beta', 'supplier', 'b')
    sync(p, pid)
    p.evidence.dependencies.withdraw(pid, first['id'], 'correction', actor='test')
    assert sync(p, pid)['retired'] == 0  # still a supplier
    assert relation_watches(p, pid)[0]['status'] != 'retired'
    p.evidence.dependencies.withdraw(pid, second['id'], 'correction', actor='test')
    assert sync(p, pid)['retired'] == 1
    assert relation_watches(p, pid)[0]['status'] == 'retired'


def test_a_phrase_naming_several_companies_is_not_watched(tmp_path):
    p, pid, subject = project(tmp_path)
    relation(p, pid, subject, 'Alpha Labs、Beta Labs、Gamma Labs', 'customer', 'a')
    assert sync(p, pid)['added'] == 0 and not relation_watches(p, pid)


def test_name_rules():
    from project_watch.studio.relation_delivery import _listing
    assert _listing('华大基因（300676.SZ）、吉因加、贝瑞科技') and _listing('20家生态伙伴') and _listing('华大研究院旗下机构')
    assert _listing('Alpha Labs, Beta Labs')
    assert not _listing('印度斋浦尔分子实验室（Jaipur Molecular Lab，JML）') and not _listing('上海凌恩生物科技有限公司')
    assert not _listing('Illumina, Inc.') and not _listing('Thermo Fisher Scientific, Inc.') and not _listing('Acme, Co.')


def test_a_university_and_its_hospital_keep_their_own_watches(tmp_path):
    p, pid, subject = project(tmp_path)
    relation(p, pid, subject, '北京大学', 'partner', 'a', stage='strategic')
    relation(p, pid, subject, '北京大学第一医院', 'customer', 'b')
    assert sync(p, pid)['added'] == 2
    assert {w['relation_target'] for w in relation_watches(p, pid)} == {'北京大学', '北京大学第一医院'}


def test_a_dispute_keyword_holds_an_existing_watch_rather_than_retiring_it(tmp_path):
    """A keyword match adds no watch, but never retires one for good ("为保险公司提供索赔审核")."""
    p, pid, subject = project(tmp_path)
    relation(p, pid, subject, 'Insurer Delta', 'customer', 'b',
             text='Synthetic Optics sells claims review (索赔审核) software to Insurer Delta.')
    _, plain = relation(p, pid, subject, 'Insurer Delta', 'customer', 'a')
    sync(p, pid)
    p.evidence.dependencies.withdraw(pid, plain['id'], 'correction', actor='test')
    assert sync(p, pid)['retired'] == 0
    assert relation_watches(p, pid)[0]['status'] != 'retired'


def test_duplicates_from_earlier_rounds_are_merged_into_the_oldest(tmp_path):
    from contextlib import closing
    p, pid, subject = project(tmp_path)
    peer, _ = relation(p, pid, subject, 'Customer Beta', 'customer', 'a')
    sync(p, pid)
    first, = relation_watches(p, pid)
    relation(p, pid, subject, 'Customer Beta', 'supplier', 'b')
    # As an earlier release did: a second watch for the same company under another role.
    second = p.monitoring_plan.add(pid, {'text': '关注Customer Beta的供应商动态', 'label': 'Customer Beta', 'axis': 'partners',
        'scope_kind': 'relation', 'relation_type': 'supplier', 'relation_target': 'Customer Beta',
        'objects': ['Customer Beta'], 'queries': ['Customer Beta'], 'sources': 'fact:synthetic',
        'reason': '供应商线索，跟踪核实关系', 'tier': 'background', 'cadence': 'medium'},
        actor='system', origin='system', visibility='public', reason='synthetic earlier release')
    with closing(p.store._connect()) as db, db:
        db.execute('INSERT INTO studio_relation_delivery VALUES (?,?,?,?,?)',
                   (pid, peer['id'], 'supplier', second['id'], str(second['lifecycle_version'])))
    assert sync(p, pid)['retired'] == 1
    status = {w['id']: w['status'] for w in relation_watches(p, pid)}
    assert status[second['id']] == 'retired' and status[first['id']] != 'retired'
    with closing(p.store._connect()) as db:
        assert {r[0] for r in db.execute('SELECT watch_id FROM studio_relation_delivery WHERE project_id=?', (pid,))} == {first['id']}


def test_a_party_in_a_dispute_is_not_watched_as_a_partner(tmp_path):
    """Live M2: a patent litigant was recorded as a partner and watched."""
    p, pid, subject = project(tmp_path)
    relation(p, pid, subject, 'Rival Gamma', 'partner', 'a', stage='strategic',
             text='Synthetic Optics and Rival Gamma settled the patent litigation in Europe.')
    assert sync(p, pid)['added'] == 0 and not relation_watches(p, pid)
