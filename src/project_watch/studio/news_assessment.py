"""Evidence-bound editorial importance; missing assessment is not a low rating."""
LEVELS = ('major', 'notable', 'routine')
GUIDANCE = ('新闻需给出 importance 与 importance_reason：major 表示证据支持对公司经营、'
    '核心产品进度、财务、治理或竞争条件产生实质变化；notable 表示相关且值得跟踪但影响尚有限或不确定；'
    'routine 为一般资讯。结合具体主体、规模、阶段、确定性和项目关联判断，不按公司名或关键词定级。'
    '评级是编辑判断，理由必须限定为所引材料支持的影响，不能把计划当成果、把摘要当完整核验；'
    '证据不足判断时 importance 填 null、理由留空。')


def candidate(args):
    level, reason = args.get('importance'), args.get('importance_reason', '')
    if level is None:
        return {}
    if level not in LEVELS or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 600:
        raise ValueError('importance requires a valid level and bounded evidence-based reason')
    return {'importance': level, 'importance_reason': reason.strip()}


def published_assessment(claims, binding):
    rated = [r for r in claims if r.get('importance_reviewed') and r.get('importance') in LEVELS]
    if not rated:
        return {'status': 'pending'}
    selected = min(rated, key=lambda r: LEVELS.index(r['importance']))
    return {'status': 'assessed', 'level': selected['importance'],
            'reason': selected['importance_reason'], 'refs': selected['refs'], 'binding': binding}


def event_assessment(event, sources):
    """Read compatibility: only identified unassessed DSH defaults become pending.

    Legacy/manual major and notable ratings are preserved. No database backfill.
    """
    assessments = [s.get('extra', {}).get('importance_assessment', {}) for s in sources]
    matching = [a for a in assessments if a.get('status') == 'assessed' and a.get('level') == event['importance']]
    if matching:
        return {'importance_status': 'assessed', 'importance_reason': matching[0].get('reason', '')}
    pending = sources and all(s.get('extra', {}).get('dsh_bootstrap') or s.get('extra', {}).get('dsh_claims')
                              for s in sources)
    return {'importance_status': 'pending' if event['importance'] == 'routine' and pending else 'assessed',
            'importance_reason': ''}
