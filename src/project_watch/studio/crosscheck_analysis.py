"""Optional investment cross-check policy. Evidence validation and storage stay in Core."""

SYSTEM = """你在做投后核对：把“公开信息”与“公司给投资人的内部资料”逐条对照，找出需要投资经理追问的地方。
只输出三类：
- mismatch：同一件事两边说法或数字不一致（如公开报道融资额/产能/客户与内部资料不同）；
- undisclosed：公开信息出现了重大事项，但内部资料完全没有提及（融资、诉讼、核心人员变动、重大客户得失等）；
- off_plan：内部计划的里程碑与公开进展明显不符（提前、推迟或方向改变）。
规则：每条必须同时引用至少一个公开编号（N…）和——除 undisclosed 外——至少一个内部编号（M… 或 K…）；
不要把同行或行业信息当成公司自身事实；主体/合并范围、期间、币种、含税口径、合同额与收入不同，不算矛盾。
万元与亿元必须先换算。实际数与预算/预测不是事实矛盾。没有足够的同口径依据就不输出 mismatch。
每个编号必须附原文摘录：public_quotes 和 internal_quotes 是编号到连续原文摘录的对象；不能改写数字或拼接原文。
undisclosed 只能说明本次提供的片段未找到提及，不代表所有内部资料都未提及；严重性为 low。
找不到就返回空列表，不要凑数。最多 5 条。
输出 JSON：{"checks":[{"kind":"mismatch|undisclosed|off_plan","severity":"high|medium|low","topic":"不超过12字",
"public_says":"公开信息怎么说（≤60字）","internal_says":"内部资料怎么说；undisclosed 写“内部资料未提及”（≤60字）",
"question":"投后沟通时可以直接问的一个问题（≤50字）","public":["N1"],"internal":["M2"],
"public_quotes":{"N1":"连续原文"},"internal_quotes":{"M2":"连续原文"}}]}"""

KINDS = frozenset({"mismatch", "undisclosed", "off_plan"})
SEVERITIES = ("high", "medium", "low")


def annotations(candidate: dict) -> tuple[str, str]:
    """Preserve the original investment severity and question projection."""
    severity = ("low" if candidate["kind"] == "undisclosed"
                else candidate.get("severity") if candidate.get("severity") in SEVERITIES
                else "medium")
    return severity, str(candidate.get("question") or "")[:120]
