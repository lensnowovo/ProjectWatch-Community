"""Request/fee accounting survives removal of app quotas. Synthetic providers only."""
import json
from decimal import Decimal

from project_watch.model_gateway import BudgetGate, BudgetPolicy


def test_legacy_gateway_counts_and_reserves_beyond_historical_ceiling():
    gate = BudgetGate(BudgetPolicy('synthetic', 1, 2000, 200, Decimal('0.000001'), Decimal('1'), Decimal('10')))
    body = json.dumps(dict(model='synthetic', stream=True, max_tokens=200,
                           messages=[dict(role='user', content='synthetic')])).encode()
    for _ in range(3):
        gate.admit(body)
    assert gate.receipt()['requests_admitted'] == 3


def test_review_plan_covers_more_than_twenty_four_batches_without_dropping_fields():
    from project_watch.studio.judgment_review import review_batches
    fields = {f'field.{i}': 'x' * 6000 for i in range(30)}
    batches = review_batches(dict(fields=fields, evidence={}))
    assert len(batches) == 31
    reviewed = {key for batch in batches if batch['phase'] == 'fields' for key in batch['fields']}
    assert reviewed == set(fields)


