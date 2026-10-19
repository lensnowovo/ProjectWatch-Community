"""Reading order only: headline topics are neither verified events nor impact analysis.

No model calls, writes, numeric risk score or inference from incidental body text.
Negated/planned events retain their original headline and only get a topic label.
"""
import re

VERSION = 'headline-topic-v1'
_TOPICS = (
    ('capital', '融资与资本', '可进一步查看交易条款、资金安排与股权变化。',
     r'融资|并购|收购|股权转让|增资|回购|首次公开发行|上市申请|赴港上市|上市融资|上市计划|\b(?:fundrais\w*|funding round|series [a-f] funding|'
     r'ipo|acquisition|merger|share repurchase|share buyback)\b'),
    ('performance', '经营业绩', '可对照报告期、收入利润与现金回款；公开报道不替代财务核对。',
     r'营业收入|营收|净利润|业绩|财报|现金流|亏损|\b(?:revenue|earnings|financial results|'
     r'quarterly results|net profit|net loss|cash flow)\b'),
    ('regulatory', '监管与合规', '可核对适用主体、地区和生效条件；尚未判断对项目的影响。',
     r'监管|政策|处罚|诉讼|召回|临床试验|注册获批|获批上市|\b(?:regulat\w*|policy|policies|'
     r'lawsuit|recall|clinical trial|fda approval|regulatory approval)\b'),
    ('commercial', '客户与交付', '可进一步核对客户关系、合同金额及实际交付进度。',
     r'订单|中标|交付|量产|投产|扩产|产能|签约|\b(?:purchase orders?|contract award|mass production|'
     r'production capacity|supply agreement|customer contract)\b'),
    ('leadership', '团队与治理', '可查看职责、任期及治理安排是否变化。',
     r'董事长|首席执行官|高管|实际控制人|实控人|\b(?:ceo|cfo|chief executive|board of directors)\b'),
    ('product', '产品与技术', '可进一步查看产品定位、验证结果和商业化进展。',
     r'新产品|产品发布|产品布局|技术储备|技术突破|研发进展|新药|专利|\b(?:product launch|new product|patent|new drug|product roadmap|technology development)\b'),
)
_RULES = [(key, label, reason, re.compile(pattern, re.IGNORECASE)) for key, label, reason, pattern in _TOPICS]

_MARKET = re.compile(r'目标价|买入评级|卖出评级|持有评级|成交额|换手率|后市.*机会|股价[上涨跌]|[涨跌]\s*\d+(?:\.\d+)?%|\bprice target|\bstock rating', re.I)
_BUSINESS = re.compile(r'营收|收入|净利润|业绩|财报|融资|收购|订单|量产|扩产|产品发布|处罚|诉讼|\brevenue|\bearnings|\borders?\b', re.I)


def market_commentary_only(title):
    return bool(_MARKET.search(title) and not _BUSINESS.search(title))


def headline_attention(title: str, match_term: str) -> dict:
    # Ignore the matched entity/query itself, which may contain words like "CEO".
    masked = re.sub(re.escape(match_term), lambda m: ' ' * len(m[0]), title,
                    flags=re.IGNORECASE) if match_term else title
    base = {'version': VERSION, 'basis': 'headline_topic_only', 'event_verified': False, 'impact': None}
    if market_commentary_only(title):
        return {**base, 'level': 'all_news', 'topic': 'market', 'label': '行情与评级', 'reason': '', 'cue': None}
    for key, label, reason, pattern in _RULES:
        match = pattern.search(masked)
        if match:
            return {**base, 'level': 'read_first', 'topic': key, 'label': label,
                    'reason': reason, 'cue': title[match.start():match.end()]}
    return {**base, 'level': 'all_news', 'topic': 'other', 'label': '其他消息',
            'reason': '', 'cue': None}
