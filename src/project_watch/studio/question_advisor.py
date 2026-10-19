"""Optional Insight advisor: model-suggested questions and per-round judgements.

Injected by the composition root into the Core question ledger. One call suggests the
initial questions; bounded batches assess every current input for affected questions. Inputs
are public monitoring context and, when the project's model binding allows it, the
analysed materials summary; commercial-source results are never included.
"""

from __future__ import annotations

import json

from project_watch.studio.llm import ModelError
from project_watch.studio.questions import STATUSES, SUGGEST_COUNT, validate_advisor_results

_SUGGEST = f"""你是投资机构的研究助理。根据公司信息，提出投资人接下来几个月最值得持续跟踪的 3-{SUGGEST_COUNT} 个问题。
要求：
- 每个问题具体、可由公开信息观察到进展（产品量产/交付、关键客户或订单、融资、审批/认证、产能、核心人员、竞品动作等），
  不写泛泛的“公司前景如何”，不给投资建议或评级；
- 与已有问题不重复；
- queries 给 2-3 个中文检索词：关于公司自身的问题包含公司名或简称；关于竞品、患者/客户需求、技术路线、
  供应链、政策的问题，用这些对象本身检索（竞品与产品名、靶点或病种、标准名、原材料、客户车型等），不必含公司名，
  因为这类重要变化的原文往往不提被监控公司；criteria 一句话说明什么样的信息算命中，以及它与公司的关系。
输出 JSON：{{"questions":[{{"text":"问题（不超过40字）","label":"不超过8字的短名","queries":["检索词"],"criteria":"命中标准"}}]}}"""

_ASSESS = """你是投资机构的研究助理。下面每个问题附带了本轮新命中的事件。逐个判断这些事件对问题意味着什么。
status 只能取：progress（有进展）、setback（出现反面信息）、unchanged（与问题相关但没有实质变化）、insufficient（证据不足以判断）。
规则：
- 只能引用该问题下给出的 event_ids 或 material_ids，至少引用一个；materials 中的逐字 quote 是内部材料，日期未知时不推断新进展；
- 事件其实与问题无关时，不要输出这个问题；
- note 一句话（不超过80字），说清依据哪件事得出判断；不给投资建议，不夸大单条消息。
材料出现相关词不证明经营进展，预测/计划/未完成仍保留其性质。未知或历史补充使用 unchanged 或 insufficient。
输出 JSON：{"results":[{"id":"问题id","status":"progress","note":"","event_ids":["事件id"],"material_ids":["材料命中id"]}]}"""


class ModelQuestionAdvisor:
    def suggest(self, llm, project, plan, profile, existing):
        context = {
            'company': project.get('legal_name') or project.get('name') or '',
            'short_name': project.get('short_name') or '',
            'industry': project.get('industry') or '',
            'description': project.get('description') or '',
            'competitors': [c.get('name') for c in (plan or {}).get('competitors') or []][:6],
            'materials_summary': (profile or {}).get('summary') or '',
            'existing_questions': existing,
        }
        data = llm.json(_SUGGEST, json.dumps(context, ensure_ascii=False), max_tokens=2000)
        rows = data.get('questions') if isinstance(data, dict) else None
        return [r for r in rows if isinstance(r, dict)][:SUGGEST_COUNT] if isinstance(rows, list) else []

    def assess(self, llm, project, questions):
        payload = {'company': project.get('legal_name') or project.get('name') or '', 'questions': questions}
        data = llm.json(_ASSESS, json.dumps(payload, ensure_ascii=False), max_tokens=3000)
        rows = data.get('results') if isinstance(data, dict) else None
        allowed = {q['id'] for q in questions}
        try:
            validate_advisor_results(rows, allowed)
        except ValueError as exc:
            raise ModelError(str(exc), code='model_output_invalid_shape') from exc
        for row in rows:
            if row['status'] not in STATUSES:
                raise ModelError('invalid question assessment status', code='model_output_invalid_shape')
        return rows
