"""Which staleness a published section survives as an incremental seed; synthetic, no network."""
from contextlib import closing

from test_studio_agent_instance import make_service

from project_watch.studio import incremental_drafting as inc
from project_watch.studio.dependencies import mark_stale
from project_watch.studio.monitoring_contracts import (
    Alias,
    EvidenceInput,
    EvidencePart,
    LocatedRef,
    ObjectInput,
)
from project_watch.studio.objects import ObjectStore

KEY = 'snapshot-section:chain'


def _source(service, pid, key, text):
    return service.evidence.save(EvidenceInput(
        project_id=pid, source_key=key, extraction_version='parser-v1', read_scope='partial', origin='media',
        source_type='public_page', visibility='public',
        parts=[EvidencePart(locator_kind='stored_text', locator='text', text=text)]))


def _ref(source):
    text = source['parts'][0]['text']
    return LocatedRef(evidence_version_id=source['id'], part_index=0, start=0, end=len(text), quote=text)


def _world(tmp_path):
    service, pid = make_service(tmp_path / 'stale.sqlite3')
    objects = ObjectStore(service.store, service.evidence)
    first = _source(service, pid, 'news:1', '甲公司 推出新一代设备')
    peer = objects.save(pid, ObjectInput(kind='company', name='甲公司', visibility='public', refs=[_ref(first)]),
                        actor='automatic')
    deps = service.evidence.dependencies
    with closing(service.store._connect()) as db, db:
        deps.track(pid, KEY, 'insight', [first['id']], db=db)
        deps.track_objects(pid, KEY, {peer['id']: peer['version']}, db=db)
    return service, pid, objects, peer


def _seedable(service, pid):
    with closing(service.store._connect()) as db:
        return inc.seedable(db, pid, KEY, service.evidence.dependencies.get(pid, KEY, db=db))


def test_new_supporting_quotes_for_a_cited_company_keep_the_seed(tmp_path):
    service, pid, objects, peer = _world(tmp_path)
    assert _seedable(service, pid)
    later = _source(service, pid, 'news:2', '甲公司 中标省级集采')
    objects.save(pid, ObjectInput(kind='company', name='甲公司', visibility='public', refs=[_ref(later)]),
                 actor='automatic', object_id=peer['id'], expected_version=peer['version'])
    dependency = service.evidence.dependencies.get(pid, KEY)
    assert dependency['status'] == 'stale' and dependency['stale_reasons'] == ['object_revised']
    assert _seedable(service, pid)


def test_a_changed_identity_drafts_in_full(tmp_path):
    service, pid, objects, peer = _world(tmp_path)
    objects.save(pid, ObjectInput(kind='company', name='甲公司', visibility='public', refs=peer_refs(peer),
                                  aliases=[Alias(name='Jia Corp', visibility='public')]),
                 actor='automatic', object_id=peer['id'], expected_version=peer['version'])
    assert not _seedable(service, pid)


def peer_refs(peer):
    return [LocatedRef(**ref) for ref in peer['refs']]


def test_an_earlier_reason_is_not_hidden_by_a_later_seedable_one(tmp_path):
    service, pid, objects, peer = _world(tmp_path)
    deps = service.evidence.dependencies
    with closing(service.store._connect()) as db, db:
        deps.on_relationship_corrected(pid, db=db)
    later = _source(service, pid, 'news:3', '甲公司 发布年报')
    objects.save(pid, ObjectInput(kind='company', name='甲公司', visibility='public', refs=[_ref(later)]),
                 actor='automatic', object_id=peer['id'], expected_version=peer['version'])
    dependency = deps.get(pid, KEY)
    assert dependency['stale_reason'] == 'object_revised'
    assert dependency['stale_reasons'] == ['object_revised', 'relationship_corrected']
    assert not _seedable(service, pid)


def test_staleness_recorded_before_reasons_were_kept_drafts_in_full_once(tmp_path):
    service, pid, _, _ = _world(tmp_path)
    legacy = {'status': 'stale', 'stale_reason': 'source_revised'}
    with closing(service.store._connect()) as db:
        assert not inc.seedable(db, pid, KEY, legacy)
        assert inc.seedable(db, pid, KEY, {**legacy, 'stale_reasons': ['source_revised']})
    assert 'stale_reasons' not in mark_stale(legacy, 'object_revised')
    assert mark_stale({'status': 'current', 'stale_reasons': ['x']}, 'source_revised')['stale_reasons'] == ['source_revised']


def test_a_section_published_after_its_cited_company_gained_quotes_stays_current(tmp_path):
    """Section publication binds object versions frozen earlier; new quotes alone must not abort it."""
    service, pid, objects, peer = _world(tmp_path)
    later = _source(service, pid, 'news:4', '甲公司 签约新医院')
    objects.save(pid, ObjectInput(kind='company', name='甲公司', visibility='public', refs=[_ref(later)]),
                 actor='automatic', object_id=peer['id'], expected_version=peer['version'])
    deps = service.evidence.dependencies
    first = service.evidence.dependencies.get(pid, KEY)['evidence_ids']
    with closing(service.store._connect()) as db, db:
        deps.track(pid, KEY, 'insight', first, db=db)
        assert deps.track_objects(pid, KEY, {peer['id']: peer['version']}, db=db, refs_matter=False)['status'] == 'current'
        deps.track(pid, 'fact:x', 'fact', first, db=db)
        assert deps.track_objects(pid, 'fact:x', {peer['id']: peer['version']}, db=db)['status'] == 'stale'
