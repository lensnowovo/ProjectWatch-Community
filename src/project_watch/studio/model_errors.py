"""Safe model failure categories: never display provider-supplied prose."""
from urllib.parse import urlsplit

MESSAGES = {
    'model_balance_insufficient': '模型账户余额不足，请充值或切换有余额的模型账户。',
    'model_resource_unavailable': '余额不足或没有可用资源包，请检查套餐权益及 Key 类型是否匹配。',
    'model_key_invalid': '模型 Key 验证未通过，请检查 Key 是否失效、填错或与服务地址不匹配。',
    'model_access_denied': '当前账户没有访问权限，请检查套餐、模型权限及 Key 类型。',
    'model_account_unavailable': '模型账户的可用额度或付费状态异常，请检查余额、套餐和服务商用量限制。',
    'model_rate_limited': '模型服务暂时限流，请稍后再试。',
    'model_service_unavailable': '模型服务暂时不可用，请稍后再试。',
    'connection_not_sent': '网络连接未建立，请检查网络、代理和服务地址；本次请求未发出。',
    'model_timeout': '模型响应超时，结果待确认；请先查看服务商记录，不要直接重复请求。',
    'request_outcome_unknown': '连接中断，请求结果不明；请先查看服务商记录，不要重复请求。',
}
ACCOUNT_ERRORS = frozenset(('model_balance_insufficient', 'model_resource_unavailable',
                            'model_key_invalid', 'model_access_denied', 'model_account_unavailable'))


def classify(response, base_url):
    """HTTP is the fallback; only recognised structured provider identifiers refine it."""
    if response.status < 400:
        return None
    try:
        error = response.json().get('error', {})
    except (ValueError, AttributeError, RuntimeError):
        error = {}
    if not isinstance(error, dict):
        error = {}
    identifiers = {str(error.get(k, '')).lower() for k in ('code', 'type')}
    if response.status == 429 and urlsplit(base_url).hostname == 'api.openai.com':
        if 'credit_balance_exhausted' in identifiers:
            return 'model_balance_insufficient'
        if identifiers & {'insufficient_quota', 'organization_spend_limit_exceeded',
                          'project_spend_limit_exceeded', 'organization_usage_limit_exceeded'}:
            return 'model_account_unavailable'
    if response.status == 429 and urlsplit(base_url).hostname == 'open.bigmodel.cn' and '1113' in identifiers:
        return 'model_resource_unavailable'
    if response.status in (402, 429) and identifiers & {'insufficient_balance_error', 'insufficient_balance'}:
        return 'model_balance_insufficient'
    if response.status == 401:
        return 'model_key_invalid'
    if response.status == 403:
        return 'model_access_denied'
    if response.status == 402:
        return 'model_account_unavailable'
    return None
