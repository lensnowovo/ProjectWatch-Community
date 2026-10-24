"""Local settings file for the single-user Studio (model, search, registry).

Secrets live only in ``settings.json`` inside the private data directory, never
in Git or the browser. The API returns masked values only.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

MODEL_PROTOCOLS = ("openai", "anthropic")
SEARCH_PROVIDERS = ("none", "deepseek", "bocha", "brave", "tavily")
REGISTRY_PROVIDERS = ("none", "tianyancha")
COMMERCIAL_PRESETS = (
    {"id": "xiniu", "label": "烯牛数据", "description": "公司融资轮次、金额与投资方",
     "endpoint": "https://vip.xiniudata.com/mcp"},
    {"id": "pharmcube", "label": "医药魔方", "description": "医药领域融资记录",
     "endpoint": "https://mcp-openapi.pharmcube.com/mcp"},
    {"id": "wind", "label": "万得 Wind", "description": "融资与公司财经报道",
     "endpoint": "https://mcp.wind.com.cn/vserver_financial_docs/mcp/"},
)
COMMERCIAL_PROVIDERS = tuple(p["id"] for p in COMMERCIAL_PRESETS)
# Group-bot webhooks: only the vendors' official hosts are accepted.
NOTIFY_KINDS = {
    "none": (),
    "wecom": ("qyapi.weixin.qq.com",),
    "feishu": ("open.feishu.cn", "open.larksuite.com"),
    "dingtalk": ("oapi.dingtalk.com",),
}

# Presets shown in the setup wizard. Users can still type any compatible endpoint.
OUTPUT_LIMITS = (8000, 16000, 32000, 64000, 100000, 128000, 256000, 512000, 1000000)
MODEL_PRESETS = (
    {"id": "deepseek", "label": "DeepSeek", "protocol": "openai",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash"},
    {"id": "glm", "label": "GLM 标准 API", "protocol": "openai",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-5.3"},
    {"id": "glm-coding", "label": "GLM Coding Plan", "protocol": "openai",
     "base_url": "https://open.bigmodel.cn/api/coding/paas/v4", "model": "glm-5.3",
     "hint": "专用端点；能否抵扣套餐以智谱费用明细为准。官方要求自建应用使用标准 API。"},
    {"id": "kimi", "label": "Kimi (Moonshot)", "protocol": "openai",
     "base_url": "https://api.moonshot.cn/v1", "model": "kimi-k2-turbo-preview"},
    {"id": "qwen", "label": "通义千问", "protocol": "openai",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"id": "openai", "label": "OpenAI", "protocol": "openai",
     "base_url": "https://api.openai.com/v1", "model": "gpt-4.1"},
    {"id": "claude", "label": "Claude", "protocol": "anthropic",
     "base_url": "https://api.anthropic.com", "model": "claude-sonnet-4-5"},
)


@dataclass
class ModelConfig:
    protocol: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    # Upper bound for one model reply. A higher bound permits more generation;
    # actual usage and the provider's rules determine billing and plan consumption.
    # Providers with a lower ceiling are stepped down automatically (see llm.LLM).
    max_output_tokens: int = 1000000

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)


@dataclass
class SearchConfig:
    provider: str = "none"
    api_key: str = ""
    # None preserves the legacy single-provider settings and positional API.
    providers: list[str] | None = None
    keys: dict[str, str] = field(default_factory=dict)
    mode: str = "auto"
    # Google News links cannot be read as originals (R40: 297/347 items unread);
    # domestic Bing News RSS and the configured search service are the default.
    google_news: bool = False

    def enabled(self) -> list[str]:
        return list(self.providers) if self.providers is not None else (
            [self.provider] if self.provider != "none" else [])

    def credentials(self) -> dict[str, str]:
        keys = dict(self.keys)
        if self.provider != "none" and self.api_key:
            keys.setdefault(self.provider, self.api_key)
        return keys

    def ready_providers(self) -> list[tuple[str, str]]:
        keys = self.credentials()
        return [(p, keys[p]) for p in self.enabled() if keys.get(p)]


@dataclass
class RegistryConfig:
    provider: str = "none"
    api_key: str = ""


@dataclass
class CommercialConfig:
    providers: list[str] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)
    daily_limit: int = 12
    # Owner consent: vendor results may be sent to the configured model as deep-analysis evidence.
    model_use: bool = False

    def ready_providers(self) -> list[str]:
        return [p for p in COMMERCIAL_PROVIDERS if p in self.providers and self.keys.get(p)]


@dataclass
class NotifyConfig:
    kind: str = "none"
    webhook_url: str = ""
    weekly: bool = True
    weekday: int = 1  # ISO weekday, 1 = Monday
    hour: int = 9  # local time
    instant_major: bool = False

    @property
    def ready(self) -> bool:
        return self.kind != "none" and bool(self.webhook_url)


@dataclass
class StudioSettings:
    preferred_mode: str = "api"
    model: ModelConfig = field(default_factory=ModelConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    registry: RegistryConfig = field(default_factory=RegistryConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    commercial: CommercialConfig = field(default_factory=CommercialConfig)
    proxy_url: str = ""
    refresh_hours: int = 24


def mask(secret: str) -> str:
    if not secret:
        return ""
    return ("•" * 6) + secret[-4:] if len(secret) > 8 else "•" * 6


def _https_url(value: str, *, label: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{label} 必须是 https 地址")
    if parts.query or parts.fragment:
        raise ValueError(f"{label} 不能包含查询参数")
    return value


def _proxy_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("代理地址格式应为 http://127.0.0.1:7890")
    return value


def _webhook_url(value: str, kind: str) -> str:
    """The webhook URL carries the bot's secret token; accept only the vendor's official host."""
    value = value.strip()
    parts = urlsplit(value)
    hosts = NOTIFY_KINDS.get(kind, ())
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.hostname.lower() not in hosts or len(value) > 600 or any(ord(c) < 33 for c in value)):
        raise ValueError("Webhook 地址应为该平台机器人的 https 地址（" + "、".join(hosts) + "）")
    return value


def _clean_secret(value: str) -> str:
    value = value.strip()
    if any(ord(c) < 33 for c in value) or len(value) > 500:
        raise ValueError("Key 格式不正确")
    return value


class SettingsUnavailable(RuntimeError):
    """A private settings file must never silently become an empty configuration."""


def _validate_structure(raw: dict) -> None:
    """Validate known JSON fields without coercion or echoing submitted values.

    Ignore unknown legacy/presentation fields, as the previous settings API did.
    Enum, URL and range checks for updates remain in update().
    """
    if not isinstance(raw, dict):
        raise ValueError("配置应为对象")
    defaults = StudioSettings()
    for section in ("model", "search", "registry", "notify", "commercial"):
        value = raw.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError("配置分组应为对象")
        config = getattr(defaults, section)
        for item in fields(config):
            if item.name not in value:
                continue
            actual = value[item.name]
            expected = getattr(config, item.name)
            if item.name == "providers":
                valid = (actual is None and section == "search") or (
                    isinstance(actual, list) and all(isinstance(x, str) for x in actual))
            elif item.name == "keys":
                valid = isinstance(actual, dict) and all(
                    isinstance(k, str) and isinstance(v, str) for k, v in actual.items())
            else:
                valid = type(actual) is type(expected)
            if not valid:
                raise ValueError("配置字段类型不正确")
    for name in ("proxy_url", "refresh_hours", "preferred_mode"):
        if name in raw and type(raw[name]) is not type(getattr(defaults, name)):
            raise ValueError("配置字段类型不正确")
    if raw.get('preferred_mode', 'api') not in ('api', 'agent'):
        raise ValueError('不支持的运行模式')


def _atomic_write(path: Path, text: str) -> None:
    """Unique, private staging file; failed replacement leaves the original intact."""
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class SettingsStore:
    """Thread-safe atomic settings; Windows ACLs inherit from the private home."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> StudioSettings:
        with self._lock:
            return self._load()

    def _load(self) -> StudioSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return StudioSettings()
        except (OSError, ValueError) as exc:
            raise SettingsUnavailable("配置文件无法读取，请检查权限或从本地备份恢复；原文件未覆盖") from exc

        try:
            _validate_structure(raw)
        except ValueError as exc:
            raise SettingsUnavailable("配置文件格式不正确，请从本地备份恢复；原文件未覆盖") from exc

        def pick(cls, data):
            names = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in (data or {}).items() if k in names})

        return StudioSettings(
            preferred_mode=raw.get('preferred_mode', 'api'),
            model=pick(ModelConfig, raw.get("model")),
            search=pick(SearchConfig, raw.get("search")),
            registry=pick(RegistryConfig, raw.get("registry")),
            notify=pick(NotifyConfig, raw.get("notify")),
            commercial=pick(CommercialConfig, raw.get("commercial")),
            proxy_url=str(raw.get("proxy_url", "")),
            refresh_hours=int(raw.get("refresh_hours", 24) or 24),
        )

    def save(self, settings: StudioSettings) -> None:
        with self._lock:
            self._save(settings)

    def _save(self, settings: StudioSettings) -> None:
        raw = asdict(settings)
        _validate_structure(raw)
        # Validate the existing file even for direct save(), not just update().
        self._load()
        text = json.dumps(raw, ensure_ascii=False, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                previous = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                previous = text
            # Back up only valid content, never rotate a damaged file over it.
            _validate_structure(json.loads(previous))
            _atomic_write(self.path.with_suffix(self.path.suffix + ".bak"), previous)
            _atomic_write(self.path, text)
        except (OSError, ValueError) as exc:
            raise SettingsUnavailable("配置保存失败；原配置保留，请检查文件权限和磁盘空间") from exc

    def update(self, patch: dict) -> StudioSettings:
        """Apply a partial update. Empty secret fields keep the stored secret."""
        with self._lock:
            _validate_structure(patch)
            current = self._load()
            if 'preferred_mode' in patch:
                current.preferred_mode = patch['preferred_mode']
            model = patch.get("model")
            if model is not None:
                previous_endpoint = (current.model.protocol, current.model.base_url.rstrip("/"))
                protocol = model.get("protocol", current.model.protocol)
                if protocol not in MODEL_PROTOCOLS:
                    raise ValueError("不支持的模型协议")
                current.model.protocol = protocol
                if "base_url" in model:
                    current.model.base_url = _https_url(model["base_url"], label="模型地址")
                if "model" in model:
                    name = str(model["model"]).strip()
                    if len(name) > 160 or any(c.isspace() for c in name):
                        raise ValueError("模型名称格式不正确")
                    current.model.model = name
                if "max_output_tokens" in model:
                    limit = int(model["max_output_tokens"] or 0)
                    if limit not in OUTPUT_LIMITS:
                        raise ValueError("单次回复上限只支持 8K/16K/32K/64K/100K/128K/256K/512K/1M")
                    current.model.max_output_tokens = limit
                if model.get("api_key"):
                    current.model.api_key = _clean_secret(model["api_key"])
                elif previous_endpoint != (current.model.protocol, current.model.base_url.rstrip("/")):
                    current.model.api_key = ""
            search = patch.get("search")
            if search is not None:
                if "providers" in search or "keys" in search:
                    providers = search.get("providers", current.search.enabled())
                    if (not isinstance(providers, list) or len(providers) > len(SEARCH_PROVIDERS) - 1
                            or any(p not in SEARCH_PROVIDERS[1:] for p in providers)
                            or len(set(providers)) != len(providers)):
                        raise ValueError("搜索服务应为 DeepSeek、博查、Tavily、Brave 的不重复列表")
                    keys = current.search.credentials()
                    updates = search.get("keys", {})
                    if not isinstance(updates, dict) or any(p not in SEARCH_PROVIDERS[1:] for p in updates):
                        raise ValueError("不支持的搜索密钥")
                    for name, value in updates.items():
                        if not isinstance(value, str):
                            raise ValueError("Key 格式不正确")
                        if value:
                            keys[name] = _clean_secret(value)
                    current.search = SearchConfig(
                        providers[0] if providers else "none",
                        keys.get(providers[0], "") if providers else "",
                        providers, keys, current.search.mode, current.search.google_news)
                else:
                    if "provider" in search or "api_key" in search:
                        self._update_legacy_search(current, search)
                if "google_news" in search:
                    current.search.google_news = bool(search["google_news"])
                if "mode" in search:
                    if search["mode"] not in ("auto", "combined"):
                        raise ValueError("搜索方式应为自动搭配或联合检索")
                    current.search.mode = search["mode"]
            registry = patch.get("registry")
            if registry is not None:
                provider = registry.get("provider", current.registry.provider)
                if provider not in REGISTRY_PROVIDERS:
                    raise ValueError("不支持的工商数据服务")
                if provider != current.registry.provider and not registry.get("api_key"):
                    current.registry.api_key = ""
                current.registry.provider = provider
                if registry.get("api_key"):
                    current.registry.api_key = _clean_secret(registry["api_key"])
            notify = patch.get("notify")
            if notify is not None:
                kind = notify.get("kind", current.notify.kind)
                if kind not in NOTIFY_KINDS:
                    raise ValueError("不支持的推送渠道")
                if kind != current.notify.kind and not notify.get("webhook_url"):
                    current.notify.webhook_url = ""
                current.notify.kind = kind
                if notify.get("webhook_url"):
                    current.notify.webhook_url = _webhook_url(str(notify["webhook_url"]), kind)
                if kind == "none":
                    current.notify.webhook_url = ""
                for name in ("weekly", "instant_major"):
                    if name in notify:
                        setattr(current.notify, name, bool(notify[name]))
                if "weekday" in notify:
                    weekday = int(notify["weekday"])
                    if not 1 <= weekday <= 7:
                        raise ValueError("推送日期应为周一至周日")
                    current.notify.weekday = weekday
                if "hour" in notify:
                    hour = int(notify["hour"])
                    if not 0 <= hour <= 23:
                        raise ValueError("推送时间应为 0-23 点")
                    current.notify.hour = hour
            commercial = patch.get("commercial")
            if commercial is not None:
                if not isinstance(commercial, dict) or set(commercial) - {"providers", "keys", "clear_keys", "daily_limit", "model_use"}:
                    raise ValueError("专业来源只支持预设服务")
                providers = commercial.get("providers", current.commercial.providers)
                if (not isinstance(providers, list) or len(providers) > 3
                        or any(p not in COMMERCIAL_PROVIDERS for p in providers)
                        or len(set(providers)) != len(providers)):
                    raise ValueError("不支持的专业数据来源")
                updates = commercial.get("keys", {})
                clear = commercial.get("clear_keys", [])
                if (not isinstance(updates, dict) or any(p not in COMMERCIAL_PROVIDERS for p in updates)
                        or not isinstance(clear, list) or any(p not in COMMERCIAL_PROVIDERS for p in clear)):
                    raise ValueError("不支持的专业来源密钥")
                keys = dict(current.commercial.keys)
                for name, value in updates.items():
                    if not isinstance(value, str):
                        raise ValueError("Key 格式不正确")
                    if value:
                        keys[name] = _clean_secret(value)
                for name in clear:
                    keys.pop(name, None)
                limit = commercial.get('daily_limit', current.commercial.daily_limit)
                if type(limit) is not int or limit not in (3, 6, 12, 24, 60):
                    raise ValueError('专业查询上限只支持每日 3/6/12/24/60 次')
                model_use = commercial.get("model_use", current.commercial.model_use)
                if type(model_use) is not bool:
                    raise ValueError("“交给 AI 分析”只能开启或关闭")
                current.commercial = CommercialConfig(list(providers), keys, limit, model_use)
            if "proxy_url" in patch:
                current.proxy_url = _proxy_url(str(patch["proxy_url"] or ""))
            if "refresh_hours" in patch:
                hours = int(patch["refresh_hours"])
                if hours not in (6, 12, 24, 72, 168):
                    raise ValueError("更新频率只支持 6/12/24/72/168 小时")
                current.refresh_hours = hours
            self._save(current)
            return current

    @staticmethod
    def _update_legacy_search(current: StudioSettings, search: dict) -> None:
        provider = search.get("provider", current.search.provider)
        if provider not in SEARCH_PROVIDERS:
            raise ValueError("不支持的搜索服务")
        key = current.search.api_key if provider == current.search.provider else ""
        if search.get("api_key"):
            key = _clean_secret(search["api_key"])
        current.search = SearchConfig(provider, key)

    @staticmethod
    def public_view(settings: StudioSettings) -> dict:
        return {
            "model": {
                "protocol": settings.model.protocol,
                "base_url": settings.model.base_url,
                "model": settings.model.model,
                "api_key": mask(settings.model.api_key),
                "max_output_tokens": settings.model.max_output_tokens,
                "ready": settings.model.ready,
            },
            "search": {
                "provider": settings.search.provider,
                "api_key": mask(settings.search.api_key),
                "providers": settings.search.enabled(),
                "keys": {p: mask(k) for p, k in settings.search.credentials().items()},
                "ready": bool(settings.search.ready_providers()),
                "mode": settings.search.mode,
                "google_news": settings.search.google_news,
            },
            "registry": {
                "provider": settings.registry.provider,
                "api_key": mask(settings.registry.api_key),
            },
            "notify": {
                "kind": settings.notify.kind,
                "webhook_url": mask(settings.notify.webhook_url),
                "weekly": settings.notify.weekly, "weekday": settings.notify.weekday,
                "hour": settings.notify.hour, "instant_major": settings.notify.instant_major,
                "ready": settings.notify.ready,
            },
            "commercial": {
                "providers": settings.commercial.providers,
                "keys": {p: mask(k) for p, k in settings.commercial.keys.items() if p in COMMERCIAL_PROVIDERS},
                "presets": list(COMMERCIAL_PRESETS),
                "ready": settings.commercial.ready_providers(),
                "daily_limit": settings.commercial.daily_limit,
                "model_use": settings.commercial.model_use,
            },
            "preferred_mode": settings.preferred_mode,
            "proxy_url": settings.proxy_url,
            "refresh_hours": settings.refresh_hours,
            "presets": list(MODEL_PRESETS),
        }
