"""Model client for OpenAI-compatible and Anthropic Messages APIs, JSON-out.

No token caps beyond the configured per-reply ceiling (default 1M output tokens, stepped
down automatically to what the provider accepts); usage is recorded so the UI can show
consumption instead of blocking.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit

from project_watch.studio.http import Http, HttpError
from project_watch.studio.settings import ModelConfig
from project_watch.studio.usage import token_count

# Learned per endpoint+model: the largest max_tokens the provider accepted after a rejection.
_CEILINGS: dict[tuple[str, str, str], int] = {}
_LIMIT_HINT = re.compile(r"max[_ ]?(?:completion[_ ])?tokens|max_output|output tokens|token.*(?:limit|range|exceed)|"
                         r"范围|上限|超过", re.I)


def _stated_ceiling(message: str, requested: int) -> int | None:
    """Pull a ceiling like 'range of [1, 8192]' / '100000 > 64000' out of a provider's error text."""
    numbers = [int(n) for n in re.findall(r"(?<![\d.])(\d{4,6})(?![\d.])", message)]
    plausible = [n for n in numbers if 1024 <= n < requested]
    return max(plausible) if plausible else None


class ModelError(RuntimeError):
    def __init__(self, message: str, *, code: str = "model_error", status: int | None = None) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def _extract_json(text: str):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        result = json.loads(text)
    except ValueError:
        result = None
    else:
        if isinstance(result, dict):
            return result
        raise ModelError("模型返回了非对象 JSON，需要重新整理", code="model_output_invalid_shape")
    for opener, closer in (("{", "}"),):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise ModelError("模型返回的内容不是有效 JSON", code="model_output_invalid_json")


def structured_options(config):
    """Established provider capabilities for bounded structured inference."""
    # Official DeepSeek defaults to high thinking effort. On batch extraction,
    # reasoning can consume the entire budget before a single JSON token.
    # Its documented non-thinking mode reserves the budget for structured output.
    options = {"thinking": False, "json_object": True} if (
        config.protocol == "openai"
        and urlsplit(config.base_url).hostname == "api.deepseek.com"
        and config.model in {"deepseek-flash", "deepseek-v4-pro"}
    ) else {}
    # GLM-5.3 defaults to maximum reasoning. Structured extraction needs a
    # bounded thinking effort so large evidence tables can return promptly.
    # https://huggingface.co/zai-org/GLM-5.3/blob/main/README.md
    if (config.protocol == "openai"
            and urlsplit(config.base_url).hostname == "open.bigmodel.cn"
            and config.model == "glm-5.3"):
        options["reasoning_effort"] = "low"
    return options


class LLM:
    def __init__(self, config: ModelConfig, http: Http, *, on_usage=None) -> None:
        self.config = config
        self.http = http
        self.on_usage = on_usage

    @property
    def ready(self) -> bool:
        return self.config.ready

    def _record(self, input_tokens: int | None, output_tokens: int | None) -> None:
        if self.on_usage:
            self.on_usage(input_tokens, output_tokens)

    def complete(self, system: str, user: str, *, max_tokens: int = 4096,
                 temperature: float = 0.2, thinking: bool | None = None, json_object: bool = False,
                 reasoning_effort: str | None = None) -> str:
        if not self.ready:
            raise ModelError("尚未配置模型，请先在设置中填写模型服务")
        cfg = self.config
        last_error: Exception | None = None
        key = (cfg.protocol, cfg.base_url.rstrip("/"), cfg.model)
        # The configured ceiling remains an upper bound, including for legacy callers.
        budget = int(getattr(cfg, "max_output_tokens", 0) or max_tokens)
        if key in _CEILINGS:
            budget = min(budget, _CEILINGS[key])
        negotiated = False
        attempt = 0
        while attempt < 3:
            try:
                if cfg.protocol == "anthropic":
                    response = self.http.post_json(
                        f"{cfg.base_url}/v1/messages",
                        {"model": cfg.model, "max_tokens": budget, "temperature": temperature,
                         "system": system, "messages": [{"role": "user", "content": user}]},
                        headers={"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01"},
                        timeout=300,
                    )
                else:
                    payload = {"model": cfg.model, "max_tokens": budget, "temperature": temperature,
                               "messages": [{"role": "system", "content": system},
                                            {"role": "user", "content": user}]}
                    if thinking is not None:
                        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
                    if json_object:
                        payload["response_format"] = {"type": "json_object"}
                    if reasoning_effort is not None:
                        payload["reasoning_effort"] = reasoning_effort
                    response = self.http.post_json(
                        f"{cfg.base_url}/chat/completions",
                        payload,
                        headers={"Authorization": f"Bearer {cfg.api_key}"},
                        timeout=300,
                    )
            except HttpError as exc:
                if exc.code in ("unsafe_redirect", "disallowed_url"):
                    raise ModelError("服务地址发生不安全跳转，请检查模型地址；未自动重发", code=exc.code) from exc
                if exc.code in ("provider_backoff", "request_budget", "request_outcome_unknown", "project_paused"):
                    raise ModelError("请求已暂停，等待检查服务或显式重试", code=exc.code) from exc
                if exc.code == "connection_failed" and re.search(r"timed?\s*out|timeout", exc.detail, re.I):
                    raise ModelError("模型响应超时，请稍后重试；本次长请求未重复发送", code="model_timeout") from exc
                if exc.code == 'connection_failed':
                    raise ModelError('模型请求结果不明，未重复发送', code='request_outcome_unknown') from exc
                last_error = exc
                attempt += 1
                time.sleep(2 * attempt)
                continue
            # A conclusive content refusal is not an output-budget negotiation.
            # Scope the provider code to the official endpoint and never echo its
            # message (it can contain submitted text), repair or resend it.
            if response.status == 400 and urlsplit(cfg.base_url).hostname == 'open.bigmodel.cn':
                try:
                    error = response.json().get('error')
                except (HttpError, ValueError, AttributeError):
                    error = None
                if isinstance(error, dict) and str(error.get('code')) == '1301':
                    raise ModelError('模型服务拒绝分析本批内容；已保留资料和响应，未自动重发',
                                     code='model_content_rejected', status=response.status)
            if response.status in (400, 422) and budget > 1024:
                message = response.text()[:2000] if hasattr(response, "text") else ""
                if _LIMIT_HINT.search(message):
                    # The provider's output ceiling is lower than requested: step down and remember it.
                    lower = _stated_ceiling(message, budget) or budget // 2
                    budget = max(1024, lower)
                    negotiated = True
                    continue
            if response.status in (429, 500, 502, 503, 504, 529):
                last_error = ModelError(f"模型服务暂时不可用（{response.status}）")
                attempt += 1
                time.sleep(3 * attempt)
                continue
            if response.status in (401, 403):
                raise ModelError("模型 Key 无效或没有权限", code="model_access_denied", status=response.status)
            if response.status != 200:
                raise ModelError(f"模型服务返回错误（{response.status}）", code="model_http_failure", status=response.status)
            if negotiated:
                _CEILINGS[key] = budget
            data = response.json()
            if cfg.protocol == "anthropic":
                text = "".join(b.get("text", "") for b in data.get("content", [])
                               if b.get("type") == "text")
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                self._record(token_count(usage.get("input_tokens")), token_count(usage.get("output_tokens")))
            else:
                choices = data.get("choices") or [{}]
                text = (choices[0].get("message") or {}).get("content") or ""
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                self._record(token_count(usage.get("prompt_tokens")), token_count(usage.get("completion_tokens")))
            truncated = (data.get("stop_reason") == "max_tokens" if cfg.protocol == "anthropic"
                         else choices[0].get("finish_reason") == "length")
            if truncated:
                raise ModelError(f"模型输出未完成：已用满单次回复上限（{budget} tokens）。可在设置中调高上限，或换用输出上限更高的模型",
                                 code="model_output_truncated", status=response.status)
            if not text.strip():
                raise ModelError("模型没有返回内容")
            return text
        raise ModelError(f"模型服务连续失败：{last_error}")

    def json(self, system: str, user: str, *, max_tokens: int = 4096):
        system = system + "\n\n只输出 JSON，不要输出任何解释或 Markdown。"
        options = structured_options(self.config)
        text = self.complete(system, user, max_tokens=max_tokens, **options)
        try:
            return _extract_json(text)
        except ModelError:
            repair = self.complete(
                "将下面内容修正为符合原始要求的 JSON 对象，不能输出顶层数组。只输出 JSON。\n原始要求：\n" + system,
                text[:20000],
                max_tokens=max_tokens, **options)
            return _extract_json(repair)
