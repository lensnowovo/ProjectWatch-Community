"""M3A contracts: legacy payloads, optional fields, explicit version rejection."""

import pytest
from pydantic import ValidationError

from project_watch.studio.monitoring_contracts import Alias, EvidenceInput, FactInput, ObjectInput
from project_watch.studio.peer_contracts import Discovery
from project_watch.studio.questions import Assessment
from project_watch.studio.relationship_contracts import PeerSelection


def test_discovery_legacy_payload_equals_explicit_v1():
    old = {'candidates': [{'name': '合成星辰', 'source': 'D1', 'quote': '合成星辰研发光学模组'}]}
    assert Discovery.model_validate(old) == Discovery.model_validate({**old, 'schema_version': 1})


@pytest.mark.parametrize('version', [2, '1', True, 1.0])
def test_discovery_rejects_unknown_or_coerced_version(version):
    with pytest.raises(ValidationError):
        Discovery.model_validate({'schema_version': version, 'candidates': []})


def test_old_object_defaults_remain_internal_with_unknown_purpose():
    item = ObjectInput(kind='company', name='合成主体')
    assert item.visibility == 'internal' and item.purpose == 'unknown'
    enriched = ObjectInput.model_validate({**item.model_dump(mode='json'),
        'website': 'https://synthetic.example/', 'aliases': [{'name': '合成旧名',
        'valid_from': '2020-01-01', 'valid_to': '2025-12-31'}]})
    assert enriched.visibility == enriched.aliases[0].visibility == 'internal'
    assert ObjectInput.model_validate_json(enriched.model_dump_json()) == enriched
    with pytest.raises(ValidationError):
        Alias(name='反向区间', valid_from='2026-01-01', valid_to='2025-01-01')


REF = {'evidence_version_id': 'synthetic-v1', 'part_index': 0, 'start': 0, 'end': 6, 'quote': '合成来源正文'}
PAYLOADS = [
    (EvidenceInput, {'project_id': 'synthetic', 'source_key': 'doc:synthetic',
        'extraction_version': 'v1', 'read_scope': 'full',
        'parts': [{'locator_kind': 'fragment', 'locator': '1', 'text': '合成来源正文'}]}),
    (ObjectInput, {'kind': 'company', 'name': '合成主体'}),
    (FactInput, {'subject_id': 'synthetic', 'predicate': 'description', 'text': '合成来源正文', 'refs': [REF]}),
    (Assessment, {'status': 'insufficient', 'idempotency_key': 'legacy-action'}),
    (PeerSelection, {'name': '合成同行', 'decision': 'include', 'expected_version': 1,
        'idempotency_key': 'legacy-selection'}),
]


@pytest.mark.parametrize('contract,payload', PAYLOADS)
def test_legacy_contract_roundtrip_and_unknown_version_fail_closed(contract, payload):
    item = contract.model_validate(payload)
    assert contract.model_validate_json(item.model_dump_json()) == item
    # These pre-existing commands are unversioned. Storage metadata is not an
    # accepted input envelope; do not ignore new versions or change request hashes.
    with pytest.raises(ValidationError):
        contract.model_validate({**payload, 'schema_version': 2})
    with pytest.raises(ValidationError):
        contract.model_validate({**payload, 'unexpected_extension': 'ignored?'})


def test_evidence_optional_provenance_does_not_change_visibility_or_scope():
    old = PAYLOADS[0][1]
    enriched = EvidenceInput.model_validate({**old, 'raw_content_hash': 'a' * 64,
        'propagation_family': 'synthetic-family', 'family_basis': 'exact_text'})
    assert enriched.visibility == 'internal' and enriched.read_scope == 'full'
    with pytest.raises(ValidationError):
        EvidenceInput.model_validate({**old, 'source_type': 'search_summary'})
