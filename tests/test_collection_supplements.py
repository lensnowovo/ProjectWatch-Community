"""DSH supplements share collection receipts, with synthetic providers only."""

import pytest

from project_watch.studio.collection_scope import watch_direction


@pytest.mark.parametrize('axis,direction', [('science', 'technology'), ('rules', 'policy'),
                                          ('market', 'industry'), ('partners', 'chain'), ('peers', 'competitor')])
def test_saved_axis_maps_to_direction_without_industry_keywords(axis, direction):
    assert watch_direction({'axis': axis}) == direction
