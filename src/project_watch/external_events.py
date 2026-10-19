"""Conservative report clusters, NOT verified event identity or merged facts.

Only a named-company scope, an explicit date, matching action/stage/figures and
shared long event wording can propose a cluster. Every pair must agree. Missing
evidence stays separate; policy/industry scopes are never treated as actors.
"""

import hashlib
import re
import unicodedata
from datetime import datetime

VERSION = "report-groups-v1"
_DATE = re.compile(r"(?:20\d{2}年)?\d{1,2}月\d{1,2}日")
_ACTIONS = ("签约", "收购", "融资", "投产", "量产", "获批", "召回")


def _identity(item):
    actor = item.get("match_term", "")
    if (
        item.get("category") not in {"company", "competitors"}
        or len(actor) < 2
        or actor not in item.get("statement", "")
    ):
        return None
    sentence = re.split(r"[。！？!?\n]", item.get("summary", ""), maxsplit=1)[0][:1200]
    if actor not in sentence:
        return None
    dates = _DATE.findall(sentence)
    actions = [a for a in _ACTIONS if a in sentence]
    if len(dates) != 1 or len(actions) != 1:
        return None
    try:
        published = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError):
        return None
    # Publication only bounds the grouping window, never supplies an event date.
    stage = tuple(re.findall(r"拟|计划|预计|将|否认|未|取消|终止|完成|已", sentence))
    numbers = tuple(re.findall(r"\d+(?:[.,]\d+)*%?", _DATE.sub("", sentence)))
    locations = tuple(
        sorted(re.findall(r"(?:在|于)([\u4e00-\u9fff]{2,12}(?:高新区|开发区|园区))", sentence))
    )
    parties = tuple(
        sorted(re.findall(r"(?:与|和|携手)([^，。；\s]{2,40}?(?:公司|集团|研究院|医院))", sentence))
    )
    normalized = re.sub(
        r"[^\w\u4e00-\u9fff]", "", unicodedata.normalize("NFKC", _DATE.sub("", sentence)).casefold()
    )
    normalized = normalized.replace(actor.casefold(), "")
    # A long common object phrase is necessary, but still not proof of identity.
    grams = {normalized[i : i + 12] for i in range(max(0, len(normalized) - 11))}
    return (actor, dates[0], actions[0], stage, numbers, locations, parties, published, grams)


def group_reports(project_id, items):
    eligible = sorted(
        (i for i in items if i.get("project_id") == project_id), key=lambda i: i["id"]
    )
    clusters = []
    for item in eligible:
        identity = _identity(item)

        def agrees(other, identity=identity):
            if not identity or not other or identity[:7] != other[:7]:
                return False
            a, b = identity[7], other[7]
            if a.tzinfo != b.tzinfo:
                return False
            return (
                a.year == b.year
                and abs((a - b).total_seconds()) <= 7 * 86400
                and bool(identity[8] & other[8])
            )

        cluster = next((c for c in clusters if all(agrees(old) for _, old in c)), None)
        if cluster is None:
            clusters.append([(item, identity)])
        else:
            cluster.append((item, identity))
    result = []
    for cluster in clusters:
        ids = [i["id"] for i, _ in cluster]
        result.append(
            dict(
                id=hashlib.sha256((project_id + "\0" + "\0".join(ids)).encode()).hexdigest()[:24],
                member_ids=ids,
                status="candidate" if len(ids) > 1 else "single",
                event_verified=False,
                basis="explicit_date_actor_action_and_shared_wording",
                version=VERSION,
            )
        )
    return result
