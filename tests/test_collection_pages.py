"""Structured pagination and date semantics against synthetic wire responses."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from project_watch.studio.collection_pages import clinical_page, cninfo_page, openalex_page
from project_watch.studio.http import Http, HttpError, Response


def work(n=1):
    return {'id': f'https://openalex.org/W{n}', 'doi': f'https://doi.org/10.0/{n}',
            'display_name': f'Synthetic paper {n}', 'publication_date': '2026-10-20',
            'authorships': [], 'abstract_inverted_index': {'Synthetic': [0], 'abstract': [1]}}


def study():
    return {'protocolSection': {'identificationModule': {'nctId': 'NCT00000001', 'briefTitle': 'Synthetic trial'},
                               'statusModule': {'overallStatus': 'RECRUITING',
                                                'studyFirstPostDateStruct': {'date': '2024-01-02'},
                                                'lastUpdatePostDateStruct': {'date': '2026-10-20'}},
                               'sponsorCollaboratorsModule': {'leadSponsor': {'name': 'Synthetic Hospital'}}}}


def wire(data, calls=None):
    def send(_method, url, *_args):
        if calls is not None:
            calls.append(parse_qs(urlsplit(url).query))
        return Response(200, {}, json.dumps(data).encode())
    return Http(send)


def announcement(n=1, *, attachment=True):
    return {'announcementId': str(n), 'announcementTitle': f'Synthetic filing {n}',
            'secName': 'Synthetic Issuer', 'announcementTime': 1792454400000,
            'adjunctUrl': f'finalpage/2026-10-20/{n}.PDF' if attachment else ''}


def test_announcement_metadata_is_retained_without_claiming_pdf_read():
    calls = []
    result = cninfo_page(wire({'announcements': [announcement(attachment=False)],
                               'totalAnnouncement': 2}, calls), 'Synthetic Issuer', per_page=1)
    item = result['items'][0]
    assert item['extra']['source_identity'] == 'cninfo:1'
    assert not item['extra']['attachment_available']
    assert item['extra']['source_record'] == announcement(attachment=False)
    assert item['url'].startswith('https://www.cninfo.com.cn/new/fulltextSearch/full?')
    assert result['next_cursor'] == 2 and result['truncated']
    assert calls[0]['pageNum'] == ['1']


def test_announcement_zero_and_unknown_total_are_distinct():
    empty = cninfo_page(wire({'announcements': [], 'totalAnnouncement': 0}), 'Synthetic')
    assert not empty['truncated'] and empty['items'] == []
    unknown = cninfo_page(wire({'announcements': [announcement()]}), 'Synthetic')
    assert unknown['truncated'] and unknown['next_cursor'] is None
    assert unknown['total_reported'] is None


def test_announcement_explicit_zero_metadata_can_explain_null_results():
    data = {'announcements': None, 'totalAnnouncement': 0, 'totalRecordNum': 0,
            'totalpages': 0, 'hasMore': False}
    result = cninfo_page(wire(data), 'Synthetic')
    assert result['items'] == []
    assert result['total_reported'] == 0
    assert result['truncated'] is False and result['next_cursor'] is None
    assert data['announcements'] is None


@pytest.mark.parametrize('change', [
    {'totalAnnouncement': 1}, {'totalAnnouncement': None}, {'totalAnnouncement': False},
    {'hasMore': True}, {'hasMore': None}, {'totalRecordNum': 1}, {'totalpages': 1},
])
def test_null_announcement_rows_without_consistent_zero_metadata_still_fail(change):
    data = {'announcements': None, 'totalAnnouncement': 0, 'totalRecordNum': 0,
            'totalpages': 0, 'hasMore': False} | change
    with pytest.raises(HttpError):
        cninfo_page(wire(data), 'Synthetic')


@pytest.mark.parametrize('data', [{}, {'announcements': [], 'totalAnnouncement': 1},
                                 {'announcements': [], 'totalAnnouncement': True},
                                 {'announcements': [announcement() | {'adjunctUrl': '../private'}]}])
def test_announcement_invalid_page_cannot_report_successful_zero(data):
    with pytest.raises(HttpError):
        cninfo_page(wire(data), 'Synthetic')




def test_paper_page_keeps_scope_and_next_page():
    calls = []
    result = openalex_page(wire({'results': [work()], 'meta': {'count': 2}}, calls),
                           'Synthetic Institute', mode='affiliation', per_page=1,
                           start='2026-01-01', end='2026-10-26')
    assert result['truncated'] and result['next_cursor'] == 2
    assert calls[0]['filter'] == ['raw_affiliation_strings.search:Synthetic Institute,from_publication_date:2026-01-01,to_publication_date:2026-10-26']
    assert result['items'][0]['extra']['source_record'] == work()
    assert result['items'][0]['extra']['paper_search']['mode'] == 'affiliation'


@pytest.mark.parametrize('data', [{}, {'results': [], 'meta': {'count': 1}},
                               {'results': [], 'meta': {'count': True}}, {'results': [None], 'meta': {'count': 1}}])
def test_paper_malformed_or_inconsistent_page_is_not_zero(data):
    with pytest.raises(HttpError):
        openalex_page(wire(data), 'Synthetic')


def test_paper_empty_complete_page_and_basic_pagination_limit():
    assert not openalex_page(wire({'results': [], 'meta': {'count': 0}}), 'Synthetic')['truncated']
    capped = openalex_page(wire({'results': [work()], 'meta': {'count': 10001}}),
                           'Synthetic', page=10000, per_page=1)
    assert capped['truncated'] and capped['next_cursor'] is None
    with pytest.raises(ValueError):
        openalex_page(wire({}), 'Synthetic', page=10001, per_page=1)


def test_clinical_intervention_route_and_distinct_dates():
    calls = []
    result = clinical_page(wire({'studies': [study()], 'nextPageToken': 'next-page'}, calls),
                           'Synthetic Intervention', mode='term')
    assert calls[0]['query.term'] == ['Synthetic Intervention']
    assert 'query.spons' not in calls[0]
    item = result['items'][0]
    assert item['published_at'].startswith('2024-01-02')
    assert item['extra']['source_updated_at'] == '2026-10-20'
    assert item['extra']['sponsor'] == 'Synthetic Hospital'
    assert result['truncated'] and result['next_cursor'] == 'next-page'


def test_clinical_token_loop_and_missing_studies_are_failures():
    for data in ({}, {'studies': [study()], 'nextPageToken': 'old-token'}):
        with pytest.raises(HttpError):
            clinical_page(wire(data), 'Synthetic', cursor='old-token')
    assert not clinical_page(wire({'studies': []}), 'Synthetic')['truncated']










