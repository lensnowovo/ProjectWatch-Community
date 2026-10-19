"""Literal numeric checks for executor-proposed, unconfirmed observations."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_PERIOD = re.compile(r"(?:\d{4}|\d{4}-Q[1-4]|\d{4}-(?:0[1-9]|1[0-2]))\Z")
_NUMBER = re.compile(
    r"(?<![\d.])(?:\(\s*[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*\)"
    r"|[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?![\d.])"
)


def _literal_numbers(text: str):
    for match in _NUMBER.finditer(text):
        token = match.group().strip()
        parenthesized = token.startswith("(") and token.endswith(")")
        token = token.strip("() ").replace(",", "")
        try:
            value = Decimal(token)
        except InvalidOperation:
            continue
        yield -abs(value) if parenthesized else value


def validate_candidate_metric(
    *,
    number: Decimal | str | None,
    unit: str | None,
    period: str | None,
    scope: str | None,
    statement: str,
    fragment_text: str,
) -> tuple[Decimal | None, str | None, str | None, str | None]:
    """Normalize a literal metric; this never verifies its business meaning."""
    unit = unit.strip() if unit else None
    period = period.strip() if period else None
    scope = scope.strip() if scope else None
    if scope and len(scope) > 100:
        raise ValueError("metric scope must contain at most 100 characters")
    if number is None:
        if unit or period:
            raise ValueError("metric number, unit and period must be provided together")
        return None, None, None, scope
    if isinstance(number, (bool, float)):
        raise ValueError("metric number must be an exact decimal string")
    try:
        value = Decimal(number)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("metric number must be an exact decimal string") from exc
    if (
        not value.is_finite()
        or (not value.is_zero() and not -12 <= value.adjusted() <= 18)
        or len(value.as_tuple().digits) > 48
    ):
        raise ValueError("metric number must be finite and bounded")
    if not unit or len(unit) > 32:
        raise ValueError("numeric candidate requires a unit of at most 32 characters")
    if not period or _PERIOD.fullmatch(period) is None:
        raise ValueError("numeric candidate requires a YYYY, YYYY-Qn or YYYY-MM period")
    if not statement.strip() or len(statement.strip()) > 80:
        raise ValueError("numeric candidate requires a short metric name")
    if value not in _literal_numbers(fragment_text):
        raise ValueError("metric number does not occur in the selected source fragment")
    return value, unit, period, scope
