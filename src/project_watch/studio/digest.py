"""Portfolio digest (weekly report) and group-bot push.

The digest is assembled from stored results only — events, document changes, number
checks and each project's latest brief — so it costs no model tokens and is reproducible.
Push goes to an enterprise group bot (WeCom / Feishu / DingTalk) the user configured.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from project_watch.studio import progress, review
from project_watch.studio.http import Http, HttpError
from project_watch.studio.settings import NotifyConfig
from project_watch.studio.store import StudioStore

_log = logging.getLogger(__name__)

CATEGORY = {"company": "公司", "competitor": "竞品", "industry": "赛道", "policy": "政策",
            "paper": "论文", "trial": "临床", "patent": "专利"}
TONE = {"positive": "向好", "neutral": "平稳", "watch": "需关注", "negative": "偏负面"}
RANK = {"major": 0, "notable": 1, "routine": 2}
MESSAGE_LIMIT = 3800  # UTF-8 bytes, including the notice; below WeCom's 4096-byte limit


def build_digest(store: StudioStore, days: int = 7, *, per_project: int = 6) -> dict:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    events = [e for e in store.list_events(since_days=days, limit=1000)
              if e["importance"] in ("major", "notable")]
    changes = store.doc_changes_since(days)
    projects = []
    quiet, incomplete = [], []
    for p in store.list_projects():
        if p.get("status") == "paused":
            continue
        # list_events is newest first; a stable sort keeps that order within each importance.
        own = sorted((e for e in events if e["project_id"] == p["id"]), key=lambda e: RANK.get(e["importance"], 2))
        doc = [c for c in changes if c["project_id"] == p["id"]]
        issues = review.metric_issues(store.list_metrics(p["id"]))
        brief = store.get_brief(p["id"]) or {}
        state = progress.read(store, p['id'], days=days)
        name = p.get('short_name') or p['name']
        if state['state'] != 'observed':
            incomplete.append({'id': p['id'], 'name': name, 'state': state['state'], 'label': state['label']})
        if not (own or doc):
            if state['state'] == 'observed':
                quiet.append(name)
            continue
        projects.append({
            "id": p["id"], "name": p.get("short_name") or p["name"], "legal_name": p.get("legal_name") or "",
            "tone": brief.get("tone") or "", "headline": brief.get("headline") or "",
            "monitoring_progress": state,
            "major": sum(1 for e in own if e["importance"] == "major"), "notable": sum(1 for e in own if e["importance"] == "notable"),
            "events": [{"title": e["title"], "summary": e.get("summary") or "", "impact": e.get("impact") or "",
                        "category": e["category"], "importance": e["importance"], "url": e.get("url") or "",
                        "source": e.get("source") or "", "source_count": e.get("source_count", 1),
                        "when": e.get("event_at") or e["created_at"]} for e in own[:per_project]],
            "more_events": max(0, len(own) - per_project),
            "doc_changes": [{"documents": c["documents"],
                             "items": [x["text"] for x in (c.get("texts") or []) + (c.get("metrics") or [])][:6]}
                            for c in doc[:2]],
            "issues": [i["text"] for i in issues if i["severity"] == "high"][:3],
        })
    projects.sort(key=lambda x: (-x["major"], -len(x["doc_changes"]), -x["notable"]))
    return {"days": days, "start": start.isoformat(), "end": end.isoformat(), "projects": projects,
            "quiet": quiet, "incomplete": incomplete, "totals": {"projects": len(projects), "major": sum(p["major"] for p in projects),
                                       "notable": sum(p["notable"] for p in projects),
                                       "doc_changes": sum(len(p["doc_changes"]) for p in projects)}}


def _day(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%m-%d")
    except (TypeError, ValueError):
        return ""


def digest_markdown(digest: dict, *, compact: bool = False) -> str:
    """Markdown for download / copy (full) or for a group bot (compact)."""
    t = digest["totals"]
    lines = [f"# ProjectWatch 组合动态（近 {digest['days']} 天）",
             f"{_day(digest['start'])} 至 {_day(digest['end'])} · {t['projects']} 个项目有变化 · "
             f"重大 {t['major']} · 关注 {t['notable']} · 新资料 {t['doc_changes']}", ""]
    if digest.get("attention"):
        from project_watch.studio import attention

        section = attention.markdown(digest["attention"], compact=compact)
        if section:
            lines.append(section)
    if not digest["projects"]:
        lines.append('已收录信息未见重要变化。' if digest['quiet'] and not digest.get('incomplete') else '暂未整理出重要变化。')
    for p in digest["projects"]:
        tone = f"（{TONE[p['tone']]}）" if p["tone"] in TONE else ""
        lines.append(f"## {p['name']}{tone}")
        state = p.get('monitoring_progress') or {}
        if state and state['state'] != 'observed':
            lines.append(f"监控进度：{state['label']}，以下为已有结果。")
        if p["headline"] and not compact:
            lines.append(f"> {p['headline']}")
        for e in p["events"][: 3 if compact else None]:
            mark = "【重大】" if e["importance"] == "major" else ""
            title = f"[{e['title']}]({e['url']})" if e["url"] else e["title"]
            more = f"（{e['source_count']} 篇报道）" if e["source_count"] > 1 else ""
            lines.append(f"- {mark}{CATEGORY.get(e['category'], '')}·{_day(e['when'])} {title}{more}")
            if e["impact"] and not compact:
                lines.append(f"  - 意义：{e['impact']}")
        if p["more_events"] and not compact:
            lines.append(f"- 另有 {p['more_events']} 条值得关注的动态")
        for c in p["doc_changes"]:
            lines.append(f"- 新资料：{'、'.join(c['documents'])}")
            for text in c["items"][: 2 if compact else None]:
                lines.append(f"  - {text}")
        for text in p["issues"][: 1 if compact else None]:
            lines.append(f"- 需核对：{text}")
        lines.append("")
    if digest["quiet"] and not compact:
        lines.append(f"已收录信息未见重要变化：{'、'.join(digest['quiet'])}")
    if digest.get('incomplete'):
        lines.append('监控尚未完成：' + '；'.join(f"{p['name']}（{p['label']}）" for p in digest['incomplete']))
    lines.append("" if compact else "\n> 由 ProjectWatch 根据已采集信息自动整理；AI 判断请点开原文核对。")
    return "\n".join(lines).strip() + "\n"


def major_markdown(events: list[dict], names: dict[str, str]) -> str:
    lines = ["# ProjectWatch 重大事件"]
    for e in events[:10]:
        title = f"[{e['title']}]({e['url']})" if e.get("url") else e["title"]
        lines.append(f"- **{names.get(e['project_id'], '')}** · {CATEGORY.get(e['category'], '')} {title}")
        if e.get("impact"):
            lines.append(f"  - {e['impact']}")
    if len(events) > 10:
        lines.append(f"- 另有 {len(events) - 10} 条，见 ProjectWatch")
    return "\n".join(lines) + "\n"


def _truncate(text: str, limit: int = MESSAGE_LIMIT) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    notice = "\n…（内容较长，完整版请在 ProjectWatch 中查看）\n"
    cut = encoded[:max(0, limit - len(notice.encode("utf-8")))].decode("utf-8", errors="ignore")
    if "\n" in cut:
        cut = cut.rsplit("\n", 1)[0]
    return cut + notice


def webhook_payload(kind: str, title: str, markdown: str) -> dict:
    text = _truncate(markdown)
    if kind == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": text}}
    if kind == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    if kind == "feishu":
        # Feishu custom bots render plain text reliably; links stay as text.
        plain = text.replace("# ", "").replace("**", "")
        return {"msg_type": "text", "content": {"text": plain}}
    raise ValueError("unsupported notify kind")


def send(http: Http, config: NotifyConfig, title: str, markdown: str) -> None:
    if not config.ready:
        raise HttpError("notify_not_configured")
    try:
        response = http.post_json(config.webhook_url, webhook_payload(config.kind, title, markdown), timeout=20)
    except HttpError as exc:
        # A transport or vendor error may echo the secret webhook URL.
        raise HttpError(exc.code, status=exc.status) from None
    if response.status != 200:
        raise HttpError("notify_failed", status=response.status)
    try:
        data = response.json()
    except HttpError:
        raise HttpError("notify_invalid_response") from None
    # WeCom/DingTalk: errcode; Feishu: code / StatusCode. Non-zero means the bot refused it.
    if not isinstance(data, dict):
        raise HttpError("notify_invalid_response")
    fields = ("code", "StatusCode") if config.kind == "feishu" else ("errcode",)
    code = next((data[field] for field in fields if field in data), None)
    if type(code) is not int:
        raise HttpError("notify_invalid_response")
    if code != 0:
        raise HttpError("notify_rejected", detail=f"vendor code {code}")


def weekly_due(config: NotifyConfig, last_sent: str | None, local_now: datetime | None = None) -> str | None:
    """Return the ISO-week tag when this week's push is due and not yet sent."""
    local_now = local_now or datetime.now().astimezone()
    if not (config.ready and config.weekly):
        return None
    year, week, weekday = local_now.isocalendar()
    tag = f"{year}-W{week:02d}"
    if last_sent == tag:
        return None
    if weekday > config.weekday or (weekday == config.weekday and local_now.hour >= config.hour):
        return tag
    return None
