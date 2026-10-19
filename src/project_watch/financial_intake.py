"""Conservative numeric suggestions from located spreadsheet extraction rows.

This parser proposes candidates only. It does not verify a statement or infer
actuals from a file name; a reviewer still owns business meaning and approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from project_watch.domain import FragmentInput
from project_watch.financial_vocabulary import FINANCIAL_METRIC_NAMES

MAX_FINANCIAL_CANDIDATES = 24
MAX_MAPPING_ISSUES = 40
_SHEET_ROW = re.compile(r"^sheet (.+) row (\d+)$")
_CELL = re.compile(
    r"^(?P<column>[A-Z]+)(?P<row>\d+)"
    r"(?:\[header:(?P<header>[^\]]+)\])?"
    r"(?:\[format:(?P<format>percent)\])?=(?P<value>.*)$"
)
_STATEMENT_TITLE = re.compile(r"^(合并|母公司|单体)(?:资产负债表|利润表|现金流量表|科目余额表)$")
_UNIT = re.compile(r"单位\s*[:：]\s*(?:人民币\s*)?(亿元|万元|元)(?![\w])")
_FOREIGN_CURRENCY = re.compile(
    r"(?:币种|货币|单位)\s*[:：]\s*(?:(?:万|亿)?(?:美元|港元|欧元|日元|英镑)"
    r"|(?:USD|HKD|EUR|JPY|GBP|SGD|AUD)\b)",
    re.IGNORECASE,
)
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_NUMBER = re.compile(
    r"(?:[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"|\(\s*[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*\))\Z"
)


@dataclass(frozen=True, slots=True)
class FinancialSuggestion:
    metric: str
    number: Decimal
    unit: str
    period: str
    scope: str
    basis: str
    source_locator: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class MappingIssue:
    source_locator: str
    reason: str


@dataclass(frozen=True, slots=True)
class FinancialMapping:
    candidates: tuple[FinancialSuggestion, ...]
    issues: tuple[MappingIssue, ...]
    truncated_count: int = 0


def _basis(header: str) -> str | None:
    matches = {
        value
        for token, value in (
            ("实际", "actual"),
            ("审计", "actual"),
            ("预测", "forecast"),
            ("预算", "budget"),
        )
        if token in header
    }
    return matches.pop() if len(matches) == 1 else "unknown" if not matches else None


def _number(value: str) -> Decimal | None:
    value = value.strip()
    if not _NUMBER.fullmatch(value):
        return None
    parenthesized = value.startswith("(") and value.endswith(")")
    try:
        number = Decimal(value.strip("() ").replace(",", ""))
    except InvalidOperation:
        return None
    if not number.is_finite() or len(number.as_tuple().digits) > 48:
        return None
    return -abs(number) if parenthesized else number


def map_financial_fragments(reference: str, fragments: list[FragmentInput]) -> FinancialMapping:
    """Require explicit sheet scope, unit and period before proposing a value."""
    if Path(reference).suffix.lower() not in {".xlsx", ".xls"}:
        return FinancialMapping((), ())
    candidates_by_sheet: dict[str, list[FinancialSuggestion]] = {}
    eligible_count = 0
    issues: list[MappingIssue] = []
    units: dict[str, str] = {}
    ordered = sorted(fragments, key=lambda item: item.ordinal)
    title_scopes: dict[str, set[str]] = {}
    early_consolidated_titles: set[str] = set()
    trial_sheets: set[str] = set()
    for fragment in ordered:
        match = _SHEET_ROW.fullmatch(fragment.locator)
        if match is None:
            continue
        sheet, row_number = match.group(1), int(match.group(2))
        for part in fragment.text.split(" | "):
            cell = _CELL.fullmatch(part)
            if cell is None or cell.group("header"):
                continue
            title = _STATEMENT_TITLE.fullmatch(cell.group("value").strip())
            if title is None:
                continue
            title_scopes.setdefault(sheet, set()).add(title.group(1))
            if title.group(0).endswith("科目余额表"):
                trial_sheets.add(sheet)
            if title.group(1) == "合并" and row_number <= 5:
                early_consolidated_titles.add(sheet)
    trial_debit_credit_columns: dict[str, set[str]] = {}
    for fragment in ordered:
        match = _SHEET_ROW.fullmatch(fragment.locator)
        if match is None:
            continue
        sheet = match.group(1)
        if "科目余额表" in sheet:
            trial_sheets.add(sheet)
        if sheet not in trial_sheets:
            continue
        for part in fragment.text.split(" | "):
            cell = _CELL.fullmatch(part)
            if (
                cell is not None
                and not cell.group("header")
                and any(token in cell.group("value") for token in ("借方", "贷方"))
            ):
                trial_debit_credit_columns.setdefault(sheet, set()).add(cell.group("column"))
    foreign_sheets = {
        match.group(1)
        for fragment in ordered
        if (match := _SHEET_ROW.fullmatch(fragment.locator))
        and _FOREIGN_CURRENCY.search(fragment.text)
    }
    trial_nonclosing_reported: set[str] = set()
    for fragment in ordered:
        match = _SHEET_ROW.fullmatch(fragment.locator)
        if match is None:
            continue
        sheet = match.group(1)
        unit_match = _UNIT.search(fragment.text)
        if unit_match:
            units[sheet] = unit_match.group(1)
        cells = [cell for part in fragment.text.split(" | ") if (cell := _CELL.fullmatch(part))]
        metric: str | None = None
        for cell in cells:
            value = _number(cell.group("value"))
            if value is None:
                label = cell.group("value").strip()
                metric = (
                    label if not cell.group("header") and label in FINANCIAL_METRIC_NAMES else None
                )
                continue
            if metric is None:
                continue
            reason: str | None = None
            if (
                ("合并" not in sheet and sheet not in early_consolidated_titles)
                or any(token in sheet for token in ("单体", "母公司", "非合并"))
                or title_scopes.get(sheet, set()) - {"合并"}
            ):
                reason = "scope_or_unit_missing"
            elif cell.group("format") == "percent":
                reason = "format_unit_conflict"
            elif sheet in foreign_sheets:
                reason = "currency_unsupported"
            elif not units.get(sheet):
                reason = "unit_missing"
            else:
                header = cell.group("header") or ""
                years = _YEAR.findall(header)
                if sheet in trial_sheets and (
                    not any(token in header for token in ("期末余额", "年末余额"))
                    or any(token in header for token in ("借方", "贷方", "期初", "发生额"))
                    or cell.group("column") in trial_debit_credit_columns.get(sheet, set())
                ):
                    reason = "trial_balance_not_closing"
                elif len(years) != 1:
                    reason = "period_missing"
                elif _basis(header) is None:
                    reason = "basis_ambiguous"
            if reason:
                if reason == "trial_balance_not_closing":
                    if sheet in trial_nonclosing_reported:
                        continue
                    trial_nonclosing_reported.add(sheet)
                if len(issues) < MAX_MAPPING_ISSUES:
                    issues.append(MappingIssue(fragment.locator, reason))
                continue
            eligible_count += 1
            sheet_candidates = candidates_by_sheet.setdefault(sheet, [])
            if len(sheet_candidates) < MAX_FINANCIAL_CANDIDATES:
                sheet_candidates.append(
                    FinancialSuggestion(
                        metric=metric,
                        number=value,
                        unit=units[sheet],
                        period=years[0],
                        scope="合并",
                        basis=_basis(header) or "unknown",
                        source_locator=fragment.locator,
                        excerpt=fragment.text,
                    )
                )
    candidates: list[FinancialSuggestion] = []
    position = 0
    while len(candidates) < MAX_FINANCIAL_CANDIDATES:
        added = False
        for sheet_candidates in candidates_by_sheet.values():
            if position < len(sheet_candidates):
                candidates.append(sheet_candidates[position])
                added = True
                if len(candidates) == MAX_FINANCIAL_CANDIDATES:
                    break
        if not added:
            break
        position += 1
    return FinancialMapping(
        tuple(candidates), tuple(issues), truncated_count=eligible_count - len(candidates)
    )


def validate_financial_suggestions(
    reference: str,
    fragments: list[FragmentInput],
    candidates: tuple[FinancialSuggestion, ...],
) -> None:
    """Reject any altered proposal before atomic intake persistence."""
    if len(candidates) > MAX_FINANCIAL_CANDIDATES or len(set(candidates)) != len(candidates):
        raise ValueError("invalid financial suggestion count or duplicate")
    allowed = set(map_financial_fragments(reference, fragments).candidates)
    if any(candidate not in allowed for candidate in candidates):
        raise ValueError("financial suggestion does not match located spreadsheet row")


__all__ = [
    "FinancialMapping",
    "FinancialSuggestion",
    "MappingIssue",
    "map_financial_fragments",
    "validate_financial_suggestions",
]
