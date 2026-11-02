"""Manual versus scheduled intent is durable, never inferred from a UI label."""

import collection_fixtures as fixtures
import pytest


@pytest.fixture
def setup(tmp_path):
    return fixtures.setup.__wrapped__(tmp_path)








