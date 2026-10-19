"""Offline topic regression, explicitly NOT an independent model quality score."""

import json
from pathlib import Path

import pytest

from project_watch.external_attention import headline_attention

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/intelligence-quality-cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["id"])
def test_topic_reading_order_and_review_boundaries(case):
    result = headline_attention(case["title"], "")
    assert (result["level"] == "read_first") is case["read_first"]
    assert result["event_verified"] is False and result["impact"] is None
    assert case["profile"] in FIXTURE["profiles"] and case["boundary"]


def test_sample_is_large_enough_but_not_mislabelled_as_model_acceptance():
    cases = FIXTURE["cases"]
    assert FIXTURE["synthetic"] is True
    assert FIXTURE["review_status"] == "awaiting_independent_review"
    assert len(cases) >= 30 and len({c["id"] for c in cases}) == len(cases)
    assert all(sum(c["profile"] == profile for c in cases) >= 10 for profile in FIXTURE["profiles"])
    assert sum(c["read_first"] for c in cases) >= 20
    # Important counterexamples: a priority topic can still have no direct relevance.
    assert any(c["read_first"] and c["relevance"] == "background" for c in cases)
