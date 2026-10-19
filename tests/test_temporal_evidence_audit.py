"""Synthetic source availability controls; not business-event accuracy."""
import hashlib
import json
import socket
from copy import deepcopy

import pytest

from scripts.temporal_evidence_audit import audit_refs, freeze_slice

T0 = '2026-01-02T00:00:00+00:00'
T1 = '2026-01-03T00:00:00+00:00'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('No live temporal-audit networking')
    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def record(version='v0', available_at=T0, **changes):
    parts = [{'locator_kind': 'stored_text', 'locator': 'synthetic', 'text': '合成企业发布合成业务信息。'}]
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    return {'project_id': 'p0', 'evidence_version_id': version, 'available_at': available_at,
            'available_basis': 'evidence_version_created_at', 'visibility': 'public',
            'commercial': False, 'parts': parts, 'parts_sha256': digest, **changes}


def ref(version='v0', **changes):
    return {'evidence_version_id': version, 'part_index': 0, 'start': 0, 'end': 12,
            'quote': '合成企业发布合成业务信息', **changes}


def test_future_acquisition_is_excluded_even_for_an_old_article():
    rows = [record(), record('v1', T1)]
    before = deepcopy(rows)
    pack = freeze_slice('p0', T0, rows)
    assert [s['evidence_version_id'] for s in pack['sources']] == ['v0']
    assert pack['excluded'] == [{'evidence_version_id': 'v1', 'reason': 'not_yet_available'}]
    assert rows == before and pack['input_count'] == 2
    assert audit_refs(pack, [ref('v1')])['rejected'][0]['reason'] == 'outside_slice'


def test_t1_can_use_both_versions_but_t0_must_not_borrow_latest():
    records = [record(), record('v1', T1)]
    before = freeze_slice('p0', T0, records)
    later = freeze_slice('p0', T1, records)
    assert audit_refs(before, [ref('v1')])['status'] == 'rejected'
    assert audit_refs(later, [ref('v0'), ref('v1')])['resolved_count'] == 2


def test_unknown_acquisition_not_backfilled_from_publication_or_upload_date():
    pack = freeze_slice('p0', T0, [record(available_at=None)])
    assert pack['sources'] == [] and pack['ready'] is False
    assert pack['excluded'][0]['reason'] == 'availability_unknown'
    assert audit_refs(pack, [])['status'] == 'no_references'


def test_timezone_cutoff_and_exact_boundary():
    pack = freeze_slice('p0', '2026-01-02T08:00:00+08:00', [record()])
    assert len(pack['sources']) == 1
    assert freeze_slice('p0', '2026-01-01T23:59:59Z', [record()])['ready'] is False


@pytest.mark.parametrize('changes', [
    {'visibility': 'internal'}, {'commercial': True}, {'commercial': None},
    {'project_id': 'other'}, {'parts_sha256': '0' * 64},
    {'available_at': '2026-01-02'}, {'available_basis': 'publication_date'},
    {'parts': [{'text': 'changed content'}]},
])
def test_untrusted_scope_or_receipt_rejected_without_content_diagnostic(changes):
    with pytest.raises(ValueError) as error:
        freeze_slice('p0', T0, [record(**changes)])
    assert '合成业务' not in str(error.value)


def test_duplicate_version_and_naive_cutoff_rejected():
    with pytest.raises(ValueError, match='duplicate_version'):
        freeze_slice('p0', T0, [record(), record()])
    with pytest.raises(ValueError, match='timezone_required'):
        freeze_slice('p0', '2026-01-02', [record()])


@pytest.mark.parametrize('changes', [
    {'quote': '合成 企业发布合成业务信息'}, {'end': 13}, {'start': -1},
    {'start': True}, {'part_index': True}, {'part_index': 3},
    {'end': '12'}, {'quote': ''},
])
def test_bad_exact_reference_retains_full_denominator(changes):
    pack = freeze_slice('p0', T0, [record()])
    result = audit_refs(pack, [ref(), ref(**changes)])
    assert result['input_count'] == 2 and result['resolved_count'] == 1
    assert result['status'] == 'partial' and result['rejected'][0]['id'] == 'R02'
    assert result['semantic_verification'] == 'not_proven'


def test_valid_exact_reference_is_not_semantic_or_business_progress_score():
    pack = freeze_slice('p0', T0, [record()])
    before = deepcopy(pack)
    result = audit_refs(pack, [ref()])
    assert result['status'] == 'resolved' and result['resolved_count'] == 1
    assert result['semantic_verification'] == 'not_proven' and pack == before


def test_tampered_pack_is_rejected_before_reference_checks():
    pack = freeze_slice('p0', T0, [record()])
    pack['sources'][0]['parts'][0]['text'] = '后来才出现的内容'
    with pytest.raises(ValueError, match='parts_changed'):
        audit_refs(pack, [ref()])


@pytest.mark.parametrize('field,value', [('cutoff', T1), ('input_count', 0), ('excluded', [])])
def test_cutoff_or_denominator_mutation_invalidates_slice_receipt(field, value):
    pack = freeze_slice('p0', T0, [record(), record('v1', T1)])
    pack[field] = value
    with pytest.raises(ValueError, match='slice_changed'):
        audit_refs(pack, [ref()])


def test_no_sources_is_not_prepared_and_private_text_is_not_emitted():
    pack = freeze_slice('p0', T0, [])
    assert pack['ready'] is False and pack['input_count'] == 0
    denied = record(visibility='internal')
    with pytest.raises(ValueError, match='record_scope'):
        freeze_slice('p0', T0, [denied])


@pytest.mark.parametrize('records', [None, {}, [None], [record()] * 513])
def test_record_shape_and_bound_do_not_become_empty_success(records):
    with pytest.raises(ValueError):
        freeze_slice('p0', T0, records)


@pytest.mark.parametrize('refs', [None, {}, [ref()] * 4097])
def test_reference_shape_and_bound_do_not_become_success(refs):
    pack = freeze_slice('p0', T0, [record()])
    with pytest.raises(ValueError, match='refs_bound'):
        audit_refs(pack, refs)
