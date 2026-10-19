"""Executor-independent process-only configuration and authenticated activation.

Factories are explicitly injected by the application/compatibility adapter.
Reading configuration never imports an executor or implicitly enables a service.
"""

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import asdict

from fastapi import HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from project_watch.executors import EvidencePacket, ExecutorUnavailable
from project_watch.feeds import GuardedHTTPS
from project_watch.model_provider import ModelProviderConfig


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=2048)
    protocol: str = Field(max_length=40)
    model: str = Field(max_length=160)
    key: SecretStr = Field(max_length=4096)
    max_runs: int = Field(default=20, ge=1, le=100)
    thinking_mode: str = "provider_default"


class ModelConfiguration:
    def __init__(self, model=None, *, model_factory=None, executor_name="unconfigured"):
        self.model = model
        self.model_factory = model_factory
        self.executor_name = executor_name
        self._tested = None
        self._test_until = 0
        self._lock = threading.Lock()
        self._last_test = 0.0
        self.revision = secrets.token_hex(16)

    def get(self):
        return self.model

    def status(self):
        if self.model is None:
            return {"enabled": False, "storage": "session"}
        return {
            **self.model.disclosure(),
            "storage": "session",
            "receipt": getattr(self.model, "receipt", {}),
        }

    def test(self, body):
        if self.model_factory is None:
            raise HTTPException(503, "当前安装未配置连接测试执行器。")
        try:
            config = ModelProviderConfig(
                body.base_url, body.protocol, body.model, thinking_mode=body.thinking_mode
            )
            GuardedHTTPS._target(config.upstream_url)
        except ValueError:
            raise HTTPException(422, "请检查 HTTPS 地址、协议和模型名称。") from None
        except Exception:
            raise HTTPException(422, "模型地址必须是公开 HTTPS 服务。") from None
        key = body.key.get_secret_value()
        if not key or "\n" in key or "\r" in key:
            raise HTTPException(422, "请填写有效密钥。")
        if not self._lock.acquire(blocking=False):
            raise HTTPException(409, "连接测试正在运行。")
        try:
            if time.monotonic() - self._last_test < 10:
                raise HTTPException(429, "请稍后再测试。")
            self._last_test = time.monotonic()
            model = self.model_factory(config, key, max_runs=body.max_runs)
            text = "合成样例：示例公司主营工业连接器，此信息只用于接口测试。"
            packet = EvidencePacket(
                "synthetic", "synthetic", hashlib.sha256(text.encode()).hexdigest(), "line 1", text
            )
            result = model.propose(packet)
            if not result or any(
                result.get(k) != v
                for k, v in asdict(packet).items()
                if k in {"source_id", "source_sha256", "source_locator"}
            ):
                raise ExecutorUnavailable("invalid proposal")
            token = secrets.token_urlsafe(24)
            self._tested = (token, model)
            self._test_until = time.monotonic() + 600
            return {
                "ticket": token,
                "model": config.model,
                "receipt": model.receipt,
                "status": "ready",
            }
        except ExecutorUnavailable:
            raise HTTPException(
                502, "测试未通过：请检查认证、模型权限、网络及输出协议；未改变当前配置。"
            ) from None
        finally:
            self._lock.release()


def attach_model_settings(app, settings, *, team_mode=False):
    operator_token = os.environ.get("PROJECT_WATCH_SETTINGS_TOKEN", "")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path.startswith("/api/settings/"):
            # Pydantic's default error includes raw invalid input, possibly credentials.
            return JSONResponse(
                status_code=422, content={"detail": "配置格式无效，请检查字段类型和长度。"}
            )
        return await request_validation_exception_handler(request, exc)

    def authorize(request):
        if team_mode or not operator_token:
            raise HTTPException(403, "模型配置需要本机管理口令；请通过管理启动器启用。")
        if not hmac.compare_digest(request.headers.get("x-settings-token", ""), operator_token):
            raise HTTPException(403, "管理口令无效。")
        if request.method != "GET" and request.headers.get("origin") != str(
            request.base_url
        ).rstrip("/"):
            raise HTTPException(403, "请从本机设置页操作。")

    @app.get("/api/settings/status")
    def status():
        return {
            **settings.status(),
            "editable": bool(operator_token) and not team_mode and settings.model_factory is not None,
            "executor": settings.executor_name,
        }

    @app.get("/api/settings/model")
    def model(request: Request):
        authorize(request)
        return settings.status()

    @app.post("/api/settings/model/test")
    def test(request: Request, body: ModelInput):
        authorize(request)
        return settings.test(body)

    @app.post("/api/settings/model/activate")
    def activate(request: Request, body: dict):
        authorize(request)
        with settings._lock:
            if (
                not settings._tested
                or time.monotonic() > settings._test_until
                or body.get("ticket") != settings._tested[0]
            ):
                raise HTTPException(409, "测试结果已过期，请重新测试。")
            settings.model = settings._tested[1]
            settings.revision = secrets.token_hex(16)
            settings._tested = None
        return settings.status()
