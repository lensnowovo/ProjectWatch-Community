"""Keep evidence gaps in generated brief prose with the system, preserving source quotations."""

import re

_DEFER = re.compile(r'(?:请|需要|需|有待|尚待|待)(?:进一步|再|重新)?(?:核实|核验|确认)')


def brief_view(value):
    if isinstance(value, list):
        return [brief_view(item) for item in value]
    if isinstance(value, dict):
        return {key: (_DEFER.sub('系统依据不足，暂不判断', item) if key in ('text', 'summary', 'headline', 'impact')
                      and isinstance(item, str) else brief_view(item) if key not in ('refs', 'quote') else item)
                for key, item in value.items()}
    return value
