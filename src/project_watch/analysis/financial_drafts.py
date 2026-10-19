"""Read-only review drafts, deliberately separate from strict finance ingestion.

Unknown context remains empty. These drafts never become observations merely
by reading them, and never relax the confirmed-financial screening rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from project_watch.analysis.financial_mapping import (
    _CELL,
    _FOREIGN_CURRENCY,
    _SHEET_ROW,
    _UNIT,
    _YEAR,
    _basis,
    _number,
)
from project_watch.analysis.financial_projection import FINANCIAL_METRIC_NAMES
from project_watch.domain import FragmentInput

MAX_REVIEW_DRAFTS = 80
_EXTRA_METRICS = {"应收票据", "应付票据", "应付账款", "预付款项", "其他应收款", "其他应付款", "研发费用", "财务费用", "销售费用", "管理费用", "营业利润", "利润总额"}
_HEADER = re.compile(r"期末|年末|年初|期初|本期|上期|本年|上年|本月|年度|实际|预测|预算|金额|余额|发生额|行次|序号|借方|贷方")
_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")


@dataclass(frozen=True, slots=True)
class FinancialReviewDraft:
    metric: str
    number: str
    unit: str | None
    period: str | None
    scope: str | None
    basis: str
    source_locator: str
    source_cell: str
    column_header: str
    missing_fields: tuple[str, ...]
    context_locators: tuple[str, ...]


def _metric(text: str) -> str | None:
    label = re.sub(r"\s+", "", text)
    label = re.sub(r"^[一二三四五六七八九十\d]+[、.．：:)]", "", label)
    label = re.sub(r"[（(][\d.%％\s]+[）)]$", "", label).rstrip("：:")
    return label if label in FINANCIAL_METRIC_NAMES | _EXTRA_METRICS else None


def _period(header: str, titles: str, sheet: str) -> str | None:
    # Never turn a monthly/quarterly/relative flow into an annual actual.
    if re.search(r"月|季度|Q[1-4]|本期|上期", header, re.I):
        return None
    years = _YEAR.findall(header)
    if len(years) == 1:
        return years[0]
    if years:
        return None
    dates = set(_DATE.findall(titles))
    if "资产负债表" in sheet and len(dates) == 1:
        year, month, day = next(iter(dates))
        if int(month) == 12 and int(day) == 31:
            if header in {"期末余额", "年末余额"}:
                return year
            if header == "年初余额":
                return str(int(year) - 1)
    title_years = set(_YEAR.findall(titles))
    if len(title_years) == 1 and re.fullmatch(r"(?:本年累计|本年累计数|年度预算金额)", header):
        return next(iter(title_years))
    return None


def financial_review_drafts(reference: str, fragments: list[FragmentInput]) -> tuple[list[FinancialReviewDraft], int]:
    if Path(reference).suffix.lower() not in {".xls", ".xlsx"}:
        return [], 0
    sheets: dict[str, list[FragmentInput]] = {}
    for fragment in sorted(fragments, key=lambda item: item.ordinal):
        if match := _SHEET_ROW.fullmatch(fragment.locator):
            sheets.setdefault(match.group(1), []).append(fragment)
    groups: list[list[FinancialReviewDraft]] = []
    total = 0
    for sheet, rows in sheets.items():
        # Currency is never borrowed from another worksheet.
        if any(_FOREIGN_CURRENCY.search(row.text) for row in rows):
            continue
        title_rows = [row for row in rows if int(_SHEET_ROW.fullmatch(row.locator).group(2)) <= 5]
        titles = "\n".join(row.text for row in title_rows)
        units = {m.group(1) for row in rows for m in _UNIT.finditer(row.text)}
        unit = next(iter(units)) if len(units) == 1 else None
        scopes = {token for token in ("合并", "母公司", "单体") if token in sheet + titles}
        scope = next(iter(scopes)) if len(scopes) == 1 and "非合并" not in sheet + titles else None
        headers: dict[str, str] = {}
        header_locations: dict[str, str] = {}
        drafts: list[FinancialReviewDraft] = []
        for row in rows:
            metric = None
            cells = [m for part in row.text.split(" | ") if (m := _CELL.fullmatch(part.strip()))]
            for cell in cells:
                text = cell.group("value").strip()
                number = _number(text)
                column = cell.group("column")
                if number is None:
                    metric = _metric(text)
                    if not metric and not cell.group("header") and len(text) <= 36 and (_HEADER.search(text) or _YEAR.search(text)):
                        headers[column] = text
                        header_locations[column] = row.locator
                    continue
                if metric is None or cell.group("format") == "percent":
                    continue
                header = cell.group("header") or headers.get(column, "")
                if _number(header) is not None:
                    header = headers.get(column, "")
                if any(token in header for token in ("行次", "序号", "借方", "贷方", "发生额")):
                    continue
                if "科目余额表" in sheet + titles and not re.search(r"期末余额|年末余额", header):
                    continue
                period = _period(header, titles, sheet)
                # Only the column explicitly says actual/forecast/budget; a
                # filename or document title cannot silently establish basis.
                basis = _basis(header) or "unknown"
                missing = tuple(key for key, value in (("unit", unit), ("period", period), ("scope", scope), ("basis", basis if basis != "unknown" else None)) if not value)
                context = tuple(dict.fromkeys([r.locator for r in title_rows] + ([header_locations[column]] if column in header_locations else [])))
                total += 1
                if len(drafts) < MAX_REVIEW_DRAFTS:
                    drafts.append(FinancialReviewDraft(metric, str(number), unit, period, scope, basis, row.locator, column + cell.group("row"), header, missing, context))
        groups.append(drafts)
    # Round robin prevents a large first sheet from hiding the other statements.
    result = []
    for position in range(MAX_REVIEW_DRAFTS):
        for group in groups:
            if position < len(group) and len(result) < MAX_REVIEW_DRAFTS:
                result.append(group[position])
    return result, total - len(result)
