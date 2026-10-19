"""Offline, extractive research-topic hints from untrusted located text."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from project_watch.domain import RESEARCH_TOPICS, FragmentInput

MAX_CANDIDATES = 8
MAX_PER_TOPIC = 2
MAX_EXCERPT_CHARS = 240

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "公司与团队": ("创始人", "管理团队", "研发团队", "员工", "股权结构", "公司成立"),
    "产品与技术": ("产品", "研发", "专利", "核心技术", "注册证", "临床试验"),
    "经营情况": ("客户", "订单", "销量", "出货", "产能", "合同金额"),
    "财务表现": ("财务", "营业收入", "营收", "净利润", "毛利率", "现金流", "应收", "资产负债"),
    "行业与竞争": ("行业", "市场规模", "竞品", "竞争格局", "市占率", "渗透率"),
    "风险与合规": ("风险", "诉讼", "合规", "处罚", "纠纷", "违约", "质量事故"),
}
_SENTENCE_BREAK = re.compile(r"(?<=[。！？；;])|[\r\n]+")
_SERIALIZED_CELL = re.compile(r"(?:^|\|\s*)[A-Z]+\d+(?:\[[^\]]*\])*=")


def excerpt_quality_hint(text: str, locator: str = "") -> str | None:
    """Presentation/selection hint, never a review decision or truth verdict."""
    text = text.strip()
    if _SERIALIZED_CELL.search(text):
        return "表格原始行，请从财务字段或原表核对"
    if re.fullmatch(r"(?:资产负债表|现金流量表|利润表)[\s.·…\d—–-]+", text):
        return "目录项，不是业务陈述"
    if any(token in text for token in (
        "作为发表审计意见的基础", "获取充分、适当的审计证据",
        "由于错误导致的重大错报", "将已收或应收客户对价",
        "将已向客户转让商品或服务",
    )):
        return "审计或会计政策通用表述"
    if locator.startswith("page ") and len(text) >= 28 and not re.search(r"[。！？；;.!?]$", text):
        return "可能为截断段落，请阅读上下文"
    if len(text) < 36 and not re.search(r"[。！？；;.!?\d]", text) and not any(token in text for token in ("已", "未", "提供", "生产", "研发", "制造", "销售", "成立", "增长", "下降")):
        return "标题或短标签，缺少具体陈述"
    return None
# Document purpose is a stronger filing hint than repeated product words inside
# a budget. This affects only the virtual index, never the extracted statements.
_DOCUMENT_PURPOSE = {
    "公司与团队": ("历史沿革", "公司章程", "高管简历", "个人简历", "股权结构"),
    "财务表现": ("财务报表", "审计报告", "资金预算", "资金投资计划", "科目余额表", "序时簿"),
    "经营情况": ("营销策略", "定价政策", "在手订单", "客户车型统计"),
}


@dataclass(frozen=True, slots=True)
class NativeCandidate:
    subject: str
    statement: str
    source_locator: str


@dataclass(frozen=True, slots=True)
class NativeAnalysis:
    primary_topic: str
    topic_basis: str
    candidates: tuple[NativeCandidate, ...]


def _matches(text: str) -> dict[str, int]:
    lowered = text.lower()
    return {
        topic: sum(keyword.lower() in lowered for keyword in keywords)
        for topic, keywords in _KEYWORDS.items()
    }


def analyze_document(reference: str, fragments: Sequence[FragmentInput]) -> NativeAnalysis:
    """Return a hint and verbatim review candidates, without inference or I/O."""
    filename_matches = _matches(reference)
    content_scores: Counter[str] = Counter()
    scores = Counter({topic: 3 * score for topic, score in filename_matches.items()})
    candidates: list[NativeCandidate] = []
    counts: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()

    for fragment in sorted(fragments, key=lambda item: item.ordinal):
        for sentence in _SENTENCE_BREAK.split(fragment.text):
            sentence = sentence.strip()
            if not sentence:
                continue
            matches = _matches(sentence)
            scores.update(matches)
            content_scores.update(matches)
            if excerpt_quality_hint(sentence, fragment.locator):
                continue
            if len(sentence) > MAX_EXCERPT_CHARS or len(candidates) >= MAX_CANDIDATES:
                continue
            topic = max(RESEARCH_TOPICS, key=lambda item: matches[item])
            if matches[topic] == 0 or counts[topic] >= MAX_PER_TOPIC:
                continue
            key = (topic, sentence)
            if key in seen:
                continue
            seen.add(key)
            counts[topic] += 1
            candidates.append(NativeCandidate(topic, sentence, fragment.locator))

    primary = max(RESEARCH_TOPICS, key=lambda item: scores[item])
    purpose = [topic for topic, hints in _DOCUMENT_PURPOSE.items() if any(hint in reference for hint in hints)]
    if len(purpose) == 1:
        primary = purpose[0]
        return NativeAnalysis(
            primary, "filename_and_content" if content_scores[primary] else "filename",
            tuple(candidates),
        )
    if not scores[primary]:
        return NativeAnalysis("未分类", "none", tuple(candidates))
    from_name = filename_matches[primary] > 0
    from_content = content_scores[primary] > 0
    basis = (
        "filename_and_content"
        if from_name and from_content
        else "filename"
        if from_name
        else "content"
    )
    return NativeAnalysis(primary, basis, tuple(candidates))


def validate_native_candidates(
    candidates: Sequence[NativeCandidate], fragments: Sequence[FragmentInput]
) -> None:
    """Fail closed before the repository writes system-authored suggestions."""
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError("too many native candidates")
    counts: Counter[str] = Counter()
    by_locator: dict[str, list[str]] = {}
    for fragment in fragments:
        by_locator.setdefault(fragment.locator.strip(), []).append(fragment.text.strip())
    for candidate in candidates:
        if candidate.subject not in RESEARCH_TOPICS:
            raise ValueError("invalid native candidate topic")
        counts[candidate.subject] += 1
        if counts[candidate.subject] > MAX_PER_TOPIC:
            raise ValueError("too many native candidates for topic")
        if (
            not candidate.statement
            or len(candidate.statement) > MAX_EXCERPT_CHARS
            or candidate.statement != candidate.statement.strip()
            or not any(
                candidate.statement in text for text in by_locator.get(candidate.source_locator, ())
            )
        ):
            raise ValueError("native candidate is not an exact source excerpt")
