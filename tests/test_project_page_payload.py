"""The project page receives only the relationship fields it shows."""
from project_watch.studio.app import _RELATIONSHIP_PAGE_FIELDS, _relationships_for_page


def test_relationship_rows_keep_page_fields_and_drop_internal_detail():
    row = {field: 'x' for field in _RELATIONSHIP_PAGE_FIELDS}
    row.update(matching={'detail': 'y' * 500}, object_versions={'o': 1}, fact_ids=['f'], evidence_ids=['e'],
               assertion_basis='z', project_id='p')
    view = {'schema_version': 1, 'items': [row], 'exclusions': [dict(row)]}
    shown = _relationships_for_page(view)
    assert shown['schema_version'] == 1
    for key in ('items', 'exclusions'):
        assert set(shown[key][0]) == _RELATIONSHIP_PAGE_FIELDS
    assert 'matching' in view['items'][0]  # the stored view itself is not changed
