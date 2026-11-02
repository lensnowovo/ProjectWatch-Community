"""Public transport failures are terminal source receipts, never paid retries."""

import collection_fixtures as host_fixtures
import pytest

from project_watch.studio.collection_pages import public_read_request
from project_watch.studio.http import HttpError


@pytest.fixture
def setup(tmp_path):
    return host_fixtures.setup.__wrapped__(tmp_path)


def timeout(phase='connect_or_headers'):
    return HttpError('connection_failed', detail='private credential must not persist',
                     transport_phase=phase, failure_kind='timeout')












@pytest.mark.parametrize('method,url,headers', [
    ('POST', 'https://api.openalex.org/works', {}),
    ('GET', 'https://api.openalex.org/write', {}),
    ('GET', 'https://api.openalex.org/works?api_key=synthetic', {}),
    ('GET', 'https://api.openalex.org/works', {'Authorization': 'synthetic'}),
    ('GET', 'https://api.openalex.org:8443/works', {}),
    ('GET', 'https://api.openalex.org.evil.invalid/works', {}),
])
def test_public_contract_does_not_leak_to_other_requests(method, url, headers):
    assert not public_read_request(method, url, headers, None)




