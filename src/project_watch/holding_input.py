"""Strict proposal validation for fund-specific, source-bound positions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from project_watch.literal_metrics import _literal_numbers


def validated_holding_input(
    *,
    fund_name: str,
    as_of: date,
    source_locator: str,
    fragment_locator: str,
    fragment_text: str,
    invested_cost_wan: Decimal | str | None,
    ownership_pct: Decimal | str | None,
    current_value_wan: Decimal | str | None,
) -> tuple[str, Decimal | None, Decimal | None, Decimal | None]:
    """Check bounds and exact literal occurrence, not business meaning or accounting basis."""
    fund_name = " ".join(fund_name.split())
    if not 1 <= len(fund_name) <= 200:
        raise ValueError("fund name must contain 1-200 characters")
    if as_of > datetime.now(UTC).date():
        raise ValueError("holding as-of date cannot be in the future")
    if not source_locator or source_locator != fragment_locator:
        raise ValueError("holding source locator must select an extracted fragment")
    if fund_name not in fragment_text:
        raise ValueError("fund name does not occur in the selected source fragment")
    if (
        invested_cost_wan is not None or current_value_wan is not None
    ) and "万元" not in fragment_text:
        raise ValueError("holding amount requires an explicit 万元 unit in the selected fragment")
    if ownership_pct is not None and ("%" not in fragment_text and "％" not in fragment_text):
        raise ValueError(
            "holding ownership requires an explicit percent unit in the selected fragment"
        )
    values: list[Decimal | None] = []
    literals = set(_literal_numbers(fragment_text))
    for name, raw in (
        ("invested cost", invested_cost_wan),
        ("ownership percentage", ownership_pct),
        ("current value", current_value_wan),
    ):
        if raw is None:
            values.append(None)
            continue
        if isinstance(raw, (bool, float)):
            raise ValueError(f"{name} must be an exact decimal")
        try:
            value = Decimal(raw)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an exact decimal") from exc
        if not value.is_finite() or value <= 0 or value.adjusted() > 15:
            raise ValueError(f"{name} must be positive, finite and bounded")
        if name == "ownership percentage" and value > 100:
            raise ValueError("ownership percentage cannot exceed 100")
        if value not in literals:
            raise ValueError(f"{name} does not occur in the selected source fragment")
        values.append(value)
    if all(value is None for value in values):
        raise ValueError("holding snapshot requires at least one measured field")
    return fund_name, values[0], values[1], values[2]
