"""Conservative operating-statistic suggestions from located spreadsheet rows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from project_watch.domain import FragmentInput
from project_watch.financial_intake import _CELL, _SHEET_ROW, _YEAR, _basis, _number

MAX_OPERATING_CANDIDATES = 24
MAX_OPERATING_ISSUES = 40
_METRICS = frozenset({"设计产能", "实际产量", "产能利用率"})
_HEADERS = frozenset({"产品", "产线", "业务", "机型", "指标", "单位"})
_QUANTITY_UNITS = frozenset({"台", "万台", "件", "万件", "套", "万套", "吨", "万吨", "辆", "万辆"})


@dataclass(frozen=True, slots=True)
class OperatingSuggestion:
    metric: str
    number: Decimal
    unit: str
    period: str
    scope: str
    basis: str
    source_locator: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class OperatingMappingIssue:
    source_locator: str
    reason: str


@dataclass(frozen=True, slots=True)
class OperatingMapping:
    candidates: tuple[OperatingSuggestion, ...]
    issues: tuple[OperatingMappingIssue, ...]
    truncated_count: int = 0


def map_operating_fragments(reference: str, fragments: list[FragmentInput]) -> OperatingMapping:
    """Map only rows with explicit scope, metric, unit, period and basis.

    The output is unconfirmed. No utilization calculation or unit conversion is done.
    """
    if Path(reference).suffix.lower() not in {".xlsx", ".xls"}:
        return OperatingMapping((), ())
    candidates: list[OperatingSuggestion] = []
    issues: list[OperatingMappingIssue] = []
    eligible_count = 0
    headers_by_sheet: dict[str, dict[str, str]] = {}
    for fragment in sorted(fragments, key=lambda item: item.ordinal):
        match = _SHEET_ROW.fullmatch(fragment.locator)
        if match is None:
            continue
        sheet = match.group(1)
        cells = [cell for part in fragment.text.split(" | ") if (cell := _CELL.fullmatch(part))]
        values = {
            cell.group("column"): cell.group("value").strip()
            for cell in cells
            if not cell.group("header")
        }
        header_values = {column: value for column, value in values.items() if value in _HEADERS}
        if (
            "指标" in header_values.values()
            and "单位" in header_values.values()
            and any(value in {"产品", "产线", "业务", "机型"} for value in header_values.values())
        ):
            headers_by_sheet[sheet] = {value: column for column, value in header_values.items()}
            continue
        headers = headers_by_sheet.get(sheet)
        if not headers:
            continue
        metric = values.get(headers["指标"])
        if metric not in _METRICS:
            continue
        scope_column = next(
            headers[name] for name in ("产品", "产线", "业务", "机型") if name in headers
        )
        scope = values.get(scope_column, "")
        unit = values.get(headers["单位"], "")
        for cell in cells:
            number = _number(cell.group("value"))
            if number is None:
                continue
            percent_formatted = cell.group("format") == "percent"
            mapped_number = number * 100 if percent_formatted else number
            hint = cell.group("header") or ""
            years = _YEAR.findall(hint)
            basis = _basis(hint)
            reason: str | None = None
            if not scope:
                reason = "scope_missing"
            elif unit not in (_QUANTITY_UNITS if metric != "产能利用率" else {"%"}):
                reason = "unit_missing"
            elif percent_formatted and metric != "产能利用率":
                reason = "format_unit_conflict"
            elif len(years) != 1:
                reason = "period_missing"
            elif basis not in {"actual", "forecast", "budget"}:
                reason = "basis_missing" if basis == "unknown" else "basis_ambiguous"
            elif metric == "实际产量" and basis != "actual":
                reason = "output_not_actual"
            elif metric == "产能利用率" and 0 < number <= 1 and not percent_formatted:
                # Without an explicit Excel number format, 0.7 is ambiguous.
                reason = "percent_scale_ambiguous"
            elif mapped_number < 0 or (metric == "产能利用率" and mapped_number > 100):
                reason = "invalid_utilization" if metric == "产能利用率" else "invalid_quantity"
            if reason:
                if len(issues) < MAX_OPERATING_ISSUES:
                    issues.append(OperatingMappingIssue(fragment.locator, reason))
                continue
            eligible_count += 1
            if len(candidates) < MAX_OPERATING_CANDIDATES:
                candidates.append(
                    OperatingSuggestion(
                        metric=metric,
                        number=mapped_number,
                        unit=unit,
                        period=years[0],
                        scope=scope,
                        basis=basis,
                        source_locator=fragment.locator,
                        excerpt=fragment.text,
                    )
                )
    return OperatingMapping(tuple(candidates), tuple(issues), eligible_count - len(candidates))


def validate_operating_suggestions(
    reference: str,
    fragments: list[FragmentInput],
    candidates: tuple[OperatingSuggestion, ...],
) -> None:
    if len(candidates) > MAX_OPERATING_CANDIDATES or len(set(candidates)) != len(candidates):
        raise ValueError("invalid operating suggestion count or duplicate")
    allowed = set(map_operating_fragments(reference, fragments).candidates)
    if any(candidate not in allowed for candidate in candidates):
        raise ValueError("operating suggestion does not match located spreadsheet row")


__all__ = [
    "OperatingMapping",
    "OperatingMappingIssue",
    "OperatingSuggestion",
    "map_operating_fragments",
    "validate_operating_suggestions",
]
