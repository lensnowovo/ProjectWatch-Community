import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.model_settings import ModelSettings, attach_model_settings
from project_watch.model_transport import PublicModelOpener


def test_bad_validation_never_echoes_input(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_WATCH_SETTINGS_TOKEN", "test-operator")
    client = TestClient(create_app(tmp_path / "data.sqlite3"))
    response = client.post(
        "/api/settings/model/test",
        headers={"Origin": "http://testserver", "X-Settings-Token": "test-operator"},
        json={"key": {"PRIVATE-KEY": "secret"}},
    )
    assert response.status_code == 422
    assert "PRIVATE-KEY" not in response.text and "secret" not in response.text


def test_activation_is_single_use_and_preserves_prior_config(monkeypatch):
    import time

    from fastapi import FastAPI

    monkeypatch.setenv("PROJECT_WATCH_SETTINGS_TOKEN", "test-operator")

    class Model:
        def disclosure(self):
            return {"enabled": True, "model": "synthetic"}

    old = Model()
    new = Model()
    settings = ModelSettings(old)
    previous = settings.revision
    settings._tested = ("ticket", new)
    settings._test_until = time.monotonic() + 60
    app = FastAPI()
    attach_model_settings(app, settings)
    client = TestClient(app)
    headers = {"Origin": "http://testserver", "X-Settings-Token": "test-operator"}
    assert (
        client.post(
            "/api/settings/model/activate", headers=headers, json={"ticket": "wrong"}
        ).status_code
        == 409
    )
    assert settings.get() is old
    assert (
        client.post(
            "/api/settings/model/activate",
            headers={**headers, "Origin": "https://evil.example"},
            json={"ticket": "ticket"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/settings/model/activate", headers=headers, json={"ticket": "ticket"}
        ).status_code
        == 200
    )
    assert settings.get() is new and settings.revision != previous
    assert (
        client.post(
            "/api/settings/model/activate", headers=headers, json={"ticket": "ticket"}
        ).status_code
        == 409
    )


def test_transport_rejects_private_dns_before_sending(monkeypatch):
    monkeypatch.setattr("project_watch.model_transport._resolve_public", lambda host: ["127.0.0.1"])
    with pytest.raises(urllib.error.URLError):
        PublicModelOpener().open(
            urllib.request.Request("https://example.com/v1/messages", data=b"private")
        )


def test_transport_rejects_redirect_without_forwarding_secret(monkeypatch):
    from project_watch import model_transport

    calls = []
    monkeypatch.setattr(model_transport, "_resolve_public", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(
        model_transport.http.client.HTTPSConnection,
        "request",
        lambda self, *a, **k: calls.append(a),
    )

    class Response:
        status = 302

    monkeypatch.setattr(
        model_transport.http.client.HTTPSConnection, "getresponse", lambda self: Response()
    )
    with pytest.raises(urllib.error.HTTPError):
        PublicModelOpener().open(
            urllib.request.Request("https://example.com/v1/messages", data=b"synthetic")
        )
    assert len(calls) == 1


def test_fake_ip_dns_cannot_resolve_to_private_destination(monkeypatch):
    monkeypatch.setattr(
        "project_watch.model_transport._resolve_public", lambda host: ["198.18.0.33"]
    )
    monkeypatch.setattr("project_watch.model_transport._public_dns", lambda host: ["10.0.0.1"])
    with pytest.raises(urllib.error.URLError):
        PublicModelOpener().open(
            urllib.request.Request("https://example.com/v1/messages", data=b"synthetic")
        )
