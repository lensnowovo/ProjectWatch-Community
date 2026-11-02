"""Exercise real Collectors via a synthetic transport and isolated query cache."""
import json

import pytest

from project_watch.studio.collection_sources import _validate_search_response
from project_watch.studio.http import HttpError, Response


@pytest.mark.parametrize('value', [{}, {'results': None}, {'results': {}}, {'results': [None]}])
def test_malformed_wire_response_is_not_empty_success(value):
    with pytest.raises(HttpError, match='invalid_search_response'):
        _validate_search_response('https://api.tavily.com/search', Response(200, {}, json.dumps(value).encode()))


@pytest.mark.parametrize('url,value', [
    ('https://api.tavily.com/search', {'results': []}),
    ('https://api.deepseek.com/v1/tools/search', {'results': []}),
    ('https://api.bochaai.com/v1/web-search', {'data': {'webPages': {'value': []}}}),
    ('https://api.search.brave.com/res/v1/web/search', {'web': {'results': []}}),
    ('https://api.search.brave.com/res/v1/news/search', {'results': []}),
])
def test_explicit_empty_wire_list_is_a_valid_result(url, value):
    _validate_search_response(url, Response(200, {}, json.dumps(value).encode()))




