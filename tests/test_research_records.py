"""Typed research facts on the actual source -> review -> event -> insight path.

Synthetic transport only. No real company response or credentials in this file.
"""
import json
from datetime import UTC, datetime

import pytest

from project_watch.studio import (
    research_records,
)
from project_watch.studio.http import HttpError
from project_watch.studio.patent_registry import PatentRecord, normalize_page


def paper(affiliation='Synthetic Host', when=None):
    return dict(id='https://openalex.org/W1234567', display_name='Synthetic protein characterization',
        publication_date=when or datetime.now(UTC).date().isoformat(),
        authorships=[{'author': {'display_name': 'Synthetic Author'},
                     'institutions': [{'display_name': affiliation}], 'raw_affiliation_strings': []}],
        primary_location={'source': {'display_name': 'Synthetic Journal'}})










def test_query_echo_never_becomes_affiliation():
    raw = paper(affiliation='Other Institution')
    raw['request'] = {'queryString': 'AFFILIATION:"Synthetic Host"'}
    value = research_records.metadata({'source_record': raw})
    assert value['affiliations'] == ['Other Institution']
    assert 'Synthetic Host' not in json.dumps(value)


def test_registry_is_off_until_real_provider_mapping_is_approved():
    with pytest.raises(HttpError, match='patent_registry_not_configured'):
        normalize_page([])


def registry():
    return dict(contract='patent-registry-record-v1', registry='Synthetic Registry', publication_number='US123456A1',
        title='Synthetic protein application', url='https://registry.example.invalid/US123456A1',
        applicants=['Synthetic Host'], inventors=['Synthetic Author'], status='application',
        event_date='2026-01-02', date_basis='application', status_quote='Application filed; not granted.',
        original_text='US123456A1 Synthetic protein application. Applicant: Synthetic Host. '
                      'Inventor: Synthetic Author. Application filed; not granted. 2026-01-02')


def test_patent_applicant_inventor_and_application_stay_separate():
    [item] = normalize_page([registry()], enabled=True)
    value = research_records.metadata(item | {'source_record': item['extra']['source_record']})
    assert value['applicants'] == ['Synthetic Host'] and value['inventors'] == ['Synthetic Author']
    assert value['authors'] == [] and value['legal_status'] == 'application'
    with pytest.raises(ValueError, match='date/status mismatch'):
        PatentRecord.model_validate(registry() | {'status': 'grant'})
    with pytest.raises(ValueError, match='verbatim evidence'):
        PatentRecord.model_validate(registry() | {'applicants': ['Other Company']})










