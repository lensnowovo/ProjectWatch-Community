"""Conservative source date normalization; never infer an occurrence from collection time."""

import calendar
import re
from datetime import date


def date_in_quote(day: date, quote: str) -> bool:
    if day.isoformat() in quote:
        return True
    return bool(re.search(rf'{day.year}年0?{day.month}月0?{day.day}日', quote))


def due_interval(text: str, said_at: date) -> tuple[date, date] | None:
    """Only explicit days/months/quarters/year-end. Relative 'next year' needs a dated statement."""
    text = re.sub(r'\s+', '', text)
    if not re.fullmatch(r'(?:(?:20\d{2}年|今年|明年))?(?:第?[一二三四1-4]季度|\d{1,2}月(?:\d{1,2}日)?|年底|年末)|20\d{2}年|\d{4}-\d{2}-\d{2}', text):
        return None
    year = said_at.year
    if '明年' in text:
        year += 1
    m = re.search(r'(20\d{2})年', text)
    if m:
        year = int(m[1])
    m = re.fullmatch(r'\d{4}-\d{2}-\d{2}', text)
    if m:
        try:
            return (date.fromisoformat(text),) * 2
        except ValueError:
            return None
    m = re.search(r'(?:(20\d{2})年)?(\d{1,2})月(?:(\d{1,2})日)?', text)
    if m:
        try:
            month = int(m[2])
            if m[3]:
                return (date(year, month, int(m[3])),) * 2
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        except ValueError:
            return None
    m = re.search(r'第?([一二三四1-4])季度', text)
    if m:
        q = '一二三四'.index(m[1]) + 1 if m[1] in '一二三四' else int(m[1])
        end = q * 3
        return date(year, end-2, 1), date(year, end, calendar.monthrange(year, end)[1])
    if '年底' in text or '年末' in text:
        return (date(year, 12, 31),) * 2
    if re.fullmatch(r'20\d{2}年', text):
        return date(year, 1, 1), date(year, 12, 31)
    return None
