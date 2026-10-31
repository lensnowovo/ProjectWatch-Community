"""Synthetic full projection checks; never call a provider or real instance."""
import socket

import pytest

from project_watch.studio.news_assessment import candidate, event_assessment


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('network prohibited')
    monkeypatch.setattr(socket, 'create_connection', deny)


def test_generic_legacy_detection_and_explicit_rating_validation():
    assert candidate({}) == {}
    with pytest.raises(ValueError):
        candidate({'importance': 'major', 'importance_reason': ''})
    dsh = [{'extra': {'dsh_claims': [{'text': 'Synthetic'}]}}]
    assert event_assessment({'importance': 'routine'}, dsh)['importance_status'] == 'pending'
    for sources in [[], [{'extra': {}}], [*dsh, {'extra': {}}]]:
        assert event_assessment({'importance': 'routine'}, sources)['importance_status'] == 'assessed'
    assert event_assessment({'importance': 'major'}, dsh)['importance_status'] == 'assessed'


