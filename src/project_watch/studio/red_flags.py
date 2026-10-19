"""Bad signals for the company and its competitors. Rules only, no model calls.

Sources, each a stored record with its own evidence:
* reports whose title opens with the company name and names a negative event (litigation,
  penalties, recalls, layoffs, departures …); a denial in the title lowers the level;
* attributed event facts that moved to cancelled / delayed / withdrawn / paused;
* official-site team page changes that removed lines;
* the existing silence signals ("该有却没有") for the company itself.

Every item says who, what, when and where it came from. Nothing asks the reader to verify.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from project_watch.studio import race

LEVELS = ("高", "中", "低")
WINDOW_DAYS = 180

_HIGH = re.compile(r"起诉|被诉|诉讼|仲裁|判决|被执行人|失信|行政处罚|处罚|罚款|立案|被调查|调查组|召回|破产|清算|冻结|查封|"
                   r"停产|停工|欠薪|讨薪|拖欠|违约|爆雷|暴雷|退市")
_MID = re.compile(r"裁员|优化人员|降薪|离职|辞职|卸任|出走|离任|关停|关闭|撤出|终止合作|解约|延期交付|推迟交付|跳票|"
                  r"亏损扩大|业绩下滑|下调|流失")
_LOW = re.compile(r"招聘页下线|停止招聘|暂停招聘|招聘缩减")
_DENIED = re.compile(r"否认|辟谣|澄清|不实|回应.{0,6}(传闻|传言)")
_STATE_LABEL = {"cancelled": "取消", "delayed": "延期", "withdrawn": "撤回", "paused": "暂停"}
_SILENCE_LEVEL = {"watch": "中", "info": "低"}


def classify(title: str) -> tuple[str, str] | None:
    """(level, matched word) for a negative report title, else None."""
    if not title:
        return None
    hit = _HIGH.search(title) or _MID.search(title) or _LOW.search(title)
    if not hit:
        return None
    level = "高" if _HIGH.search(title) else "中" if _MID.search(title) else "低"
    if _DENIED.search(title):
        level = "低"
    return level, hit.group(0)


def _item(level, who, is_self, kind, title, day, *, grade="", source="", url="", note=""):
    return {"level": level, "who": who, "is_self": is_self, "kind": kind, "title": title, "date": day,
            "grade": grade, "source": source, "url": url, "note": note}


def _reports(events, names, who, is_self, today, own_hosts):
    out, seen = [], set()
    for e in events:
        title, url, source = race._event_title(e)
        day = race._event_day(e)
        if not race._leads(title, names) or not race._within(day, today, WINDOW_DAYS):
            continue
        found = classify(title)
        key = e.get("id") or title
        if not found or key in seen:
            continue
        seen.add(key)
        level, _word = found
        outlets = race._outlets(e)
        out.append(_item(level, who, is_self, "report", e.get("summary") or title, day,
                         grade=race.grade_of(url, source, own_hosts), source="、".join(outlets[:3]) or source, url=url,
                         note="公司已回应否认" if _DENIED.search(title) else ""))
    return out


def _fact_states(event_states, names, who, is_self, today):
    out = []
    cores = {race._core(n) for n in names}
    for ev in event_states:
        if ev.get("support") != "source_assertion" or ev.get("state") not in _STATE_LABEL:
            continue
        day = race._day(ev.get('effective_at'))
        if not race._within(day, today, WINDOW_DAYS):
            continue
        subject = next((h.get("subject_name") for h in reversed(ev.get("history") or []) if h.get("subject_name")), "")
        if race._core(subject) not in cores:
            continue
        src = (ev.get('sources') or [{}])[0]
        out.append(_item("中", who, is_self, "event_state", f"{ev.get('name') or '事件'}：{_STATE_LABEL[ev['state']]}",
                         day, grade=race.grade_of(src.get('url',''), src.get('source','')),
                         source=src.get('source') or "已归属的事件", url=src.get('url','')))
    return out


def _team_changes(changes, pages, who, today):
    kinds = {p["id"]: p.get("kind") for p in pages}
    out = []
    for c in changes:
        if kinds.get(c.get("page_id")) != "team" or not c.get("removed") or not race._within(c["detected_at"], today, WINDOW_DAYS):
            continue
        removed = "、".join(r[:24] for r in c["removed"][:2])
        added = f"；新增：{'、'.join(a[:24] for a in c['added'][:2])}" if c.get("added") else ""
        out.append(_item("中", who, True, "site_team", f"官网管理团队页删除：{removed}{added}", c["detected_at"][:10],
                         grade="公司宣传", source="公司官网", url=c.get("url") or ""))
    return out


def _silence(signals_list, who):
    return [_item(_SILENCE_LEVEL.get(s.get("level"), "低"), who, True, "silence", s["title"], "", source="该有却没有",
                  note=s.get("detail") or "") for s in signals_list]


def _sort(items):
    """Most severe first; within a level, newest first (undated silence signals last)."""
    by_date = sorted(items, key=lambda i: i["date"] or "", reverse=True)
    return sorted(by_date, key=lambda i: LEVELS.index(i["level"]))


def build(*, project: dict, peers: list[dict], company_events: list[dict], competitor_events: list[dict],
          event_states: list[dict], site_changes: list[dict], site_pages: list[dict], silence: list[dict],
          today: date | None = None, limit: int = 8, commitments: list[dict] | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    site = (project.get("website") or "").strip()
    own_hosts = (race._host(site if "//" in site else "https://" + site),) if site else ()
    me = project.get("short_name") or project.get("name") or ""
    self_names = list(dict.fromkeys(n for n in (project.get("short_name"), project.get("name"),
                                                race._core(project.get("legal_name") or ""), *(project.get("aliases") or [])) if n))
    mine = (_reports(company_events, self_names, me, True, today, own_hosts)
            + _fact_states(event_states, self_names, me, True, today)
            + _team_changes(site_changes, site_pages, me, today)
            + _silence(silence, me))
    theirs = []
    for peer in peers:
        names = list(dict.fromkeys(n for n in [peer["name"], race._core(peer["name"]), *(peer.get("aliases") or [])] if n))
        theirs += _reports(competitor_events, names, peer["name"], False, today, ())
        theirs += _fact_states(event_states, names, peer["name"], False, today)
    own = set(self_names)
    peer_names = {p['name'] for p in peers}
    for p in commitments or []:
        if p['status'] != 'overdue' or p['subject_name'] not in own | peer_names:
            continue
        source = (p.get('sources') or [{}])[0]
        is_self = p['subject_name'] in own
        flag = _item('中', p['subject_name'], is_self, 'commitment', p['statement'], p['due_end'],
                     grade=race.grade_of(source.get('url', ''), source.get('source', '')),
                     source=source.get('source', ''), url=source.get('url', ''),
                     note='逾期无消息；已收录记录中没有对应进展，不等于未完成。')
        (mine if is_self else theirs).append(flag)
    mine, theirs = _sort(mine), _sort(theirs)
    return {"company": mine[:limit], "peers": theirs[:limit],
            "counts": {"company": len(mine), "peers": len(theirs),
                       "high": sum(1 for i in mine if i["level"] == "高")},
            "window_days": WINDOW_DAYS, "generated_at": datetime.now(UTC).isoformat()}
