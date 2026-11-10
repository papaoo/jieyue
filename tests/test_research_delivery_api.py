import asyncio

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.v1.endpoints import research_delivery as endpoint
from src.auth import COOKIE_NAME
from src.repositories.research_delivery_repository import ResearchDeliveryRepository
from src.services.research_delivery.credentials import CredentialVault
from src.services.research_delivery.subscriptions import ResearchDeliveryService


class APIClient:
    """Direct ASGI requests with explicit peer (repository TestClient ignores it)."""
    def __init__(self, app):
        self.app = app
        self.cookies = httpx.Cookies()
        self.headers = {"origin": "http://127.0.0.1"}
        self.peer = ("127.0.0.1", 1234)

    def request(self, method, url, **kwargs):
        headers = {**self.headers, **kwargs.pop("headers", {})}

        async def send():
            transport = httpx.ASGITransport(app=self.app, client=self.peer)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", cookies=self.cookies) as client:
                return await client.request(method, url, headers=headers, **kwargs)
        return asyncio.run(send())

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request("PATCH", url, **kwargs)


@pytest.fixture
def client(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No real configuration, database or network allowed")

    import requests
    import src.config
    from src.storage import DatabaseManager
    monkeypatch.setattr(src.config, "setup_env", forbidden)
    monkeypatch.setattr(src.config, "get_config", forbidden)
    monkeypatch.setattr(DatabaseManager, "get_instance", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(endpoint.auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(endpoint.auth, "verify_session", lambda value: value == "isolated-admin")
    engine = create_engine("sqlite:///" + (tmp_path / "api.db").as_posix(), connect_args={"check_same_thread": False})
    service = ResearchDeliveryService(ResearchDeliveryRepository(sessionmaker(bind=engine)), CredentialVault(tmp_path / "vault"))
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/api/v1/research-delivery")
    app.dependency_overrides[endpoint.get_service] = lambda: service
    result = APIClient(app)
    result.cookies.set(COOKIE_NAME, "isolated-admin")
    yield result
    engine.dispose()


PREFIX = "/api/v1/research-delivery"


def test_management_auth_and_csrf(client, monkeypatch):
    assert client.get(PREFIX + "/strategies").status_code == 200
    client.cookies.clear()
    assert client.get(PREFIX + "/destinations").status_code == 401
    client.cookies.set(COOKIE_NAME, "isolated-admin")
    monkeypatch.setattr(endpoint.auth, "is_auth_enabled", lambda: False)
    assert client.get(PREFIX + "/capabilities").status_code == 403
    monkeypatch.setattr(endpoint.auth, "is_auth_enabled", lambda: True)
    for origin in ("http://evil.example", "null", "http://127.0.0.1.evil.example"):
        assert client.post(PREFIX + "/destinations", json={"alias": "x", "kind": "test"}, headers={"origin": origin}).status_code == 403
    client.headers.pop("origin")
    assert client.post(PREFIX + "/destinations", json={"alias": "x", "kind": "test"}).status_code == 403


def test_permission_diagnostics_never_log_header_or_credential_values(client, caplog, monkeypatch):
    private = "private-diagnostic-value"
    response = client.post(PREFIX + "/destinations", json={"alias": "x", "kind": "test"},
        headers={"origin": "http://" + private + ".example", "referer": "http://bad.example/" + private})
    assert response.json()["error"] == "cross_origin"
    assert "host_match=False" in caplog.text
    assert private not in caplog.text
    monkeypatch.setattr(endpoint.auth, "is_auth_enabled", lambda: False)
    response = client.get(PREFIX + "/destinations")
    assert response.json()["error"] == "auth_required"
    assert "auth_required" in caplog.text
    monkeypatch.setattr(endpoint.auth, "is_auth_enabled", lambda: True)
    client.peer = ("192.0.2.10", 1234)
    response = client.put(PREFIX + "/destinations/missing/credentials", json={
        "version": 1, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/" + private})
    assert response.json()["error"] == "secure_transport_required"
    assert "peer_loopback=False" in caplog.text
    assert private not in caplog.text


@pytest.mark.parametrize("changes", [{"top_n": 0}, {"top_n": 21}, {"planned_time": "25:00"}, {"planned_time": "9:10"}, {"llm_mode": "rerank"}, {"enabled": True}, {"top_n": "5"}, {"top_n": True}])
def test_subscription_validation(client, changes):
    dest = client.post(PREFIX + "/destinations", json={"alias": "x", "kind": "test"}).json()
    strategy = client.get(PREFIX + "/strategies").json()["items"][0]["strategy_id"]
    body = {"name": "sub", "destination_id": dest["id"], "strategy_id": strategy, "top_n": 5, "planned_time": "15:10", "llm_mode": "off", **changes}
    assert client.post(PREFIX + "/subscriptions", json=body).status_code == 422


def test_safe_validation_no_credentials_echo(client):
    dest = client.post(PREFIX + "/destinations", json={"alias": "x", "kind": "test"}).json()
    for body in ({"version": "fake-secret", "webhook_url": "fake-secret"}, {"version": 1, "webhook_url": "https://evil.example/fake-secret", "signing_secret": "fake-secret"}, {"version": 1, "webhook_url": "fake-secret", "unexpected": "fake-secret"}):
        response = client.put(PREFIX + "/destinations/" + dest["id"] + "/credentials", json=body)
        assert response.status_code == 422
        assert "fake-secret" not in response.text


def test_credentials_refuse_remote_cleartext_and_forwarded_spoof(client):
    dest = client.post(PREFIX + "/destinations", json={"alias": "x", "kind": "test"}).json()
    response = client.put(PREFIX + "/destinations/" + dest["id"] + "/credentials", json={"version": 1, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake"}, headers={"host": "remote.example", "origin": "http://remote.example", "x-forwarded-proto": "https"})
    assert response.status_code == 403


def test_capabilities_honest_and_save_never_sends(client):
    result = client.get(PREFIX + "/capabilities").json()
    assert result["scheduler_active"] is False
    assert result["sending_enabled"] is False
    assert result["llm_modes"] == ["off", "explain_only"]


@pytest.mark.parametrize("peer", [("remote-hostname", 1234), ("192.0.2.10", 1234)])
def test_loopback_host_not_enough_without_loopback_peer(client, peer):
    dest = client.post(PREFIX + "/destinations", json={"alias": "x", "kind": "test"}).json()
    client.peer = peer
    response = client.put(PREFIX + "/destinations/" + dest["id"] + "/credentials", json={"version": 1, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake"}, headers={"x-forwarded-for": "127.0.0.1", "x-forwarded-proto": "https"})
    assert response.status_code == 403


def test_https_remote_credentials_and_public_lists(client, caplog):
    dest = client.post(PREFIX + "/destinations", json={"alias": "x", "kind": "test"}).json()
    client.peer = ("192.0.2.10", 1234)
    response = client.put("https://remote.example" + PREFIX + "/destinations/" + dest["id"] + "/credentials", json={"version": 1, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake-sensitive-token", "signing_secret": "fake-sensitive-secret"}, headers={"origin": "https://remote.example"})
    assert response.status_code == 200
    public = client.get(PREFIX + "/destinations").text
    assert "fake-sensitive" not in response.text + public + caplog.text
    stale = client.patch(PREFIX + "/destinations/" + dest["id"], json={"version": 1, "enabled": True})
    assert stale.status_code == 409


def test_safe_malformed_json_and_secret_in_wrong_fields(client):
    response = client.put(PREFIX + "/destinations/missing/credentials", content='{"fake-sensitive-secret":', headers={"content-type": "application/json"})
    assert response.status_code == 422 and "fake-sensitive-secret" not in response.text


def test_signed_webhook_update_without_new_key_refused_and_keyword_retained(client):
    dest = client.post(PREFIX + "/destinations", json={"alias": "x", "kind": "test"}).json()
    endpoint_path = PREFIX + "/destinations/" + dest["id"] + "/credentials"
    saved = client.put(endpoint_path, json={"version": 1, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake", "signing_secret": "fake-sensitive-secret", "keyword": "custom"})
    assert saved.status_code == 200
    changed = client.put(endpoint_path, json={"version": 2, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/other"})
    assert changed.status_code == 422 and changed.json()["error"] == "signing_secret_required"
    retained = client.put(endpoint_path, json={"version": 2, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake"})
    assert retained.status_code == 200
    assert retained.json()["keyword"] == "custom" and retained.json()["signing_configured"] is True
    response = client.post(PREFIX + "/subscriptions", json={"fake-sensitive-secret": "fake-sensitive-secret"})
    assert response.status_code == 422 and "fake-sensitive-secret" not in response.text


@pytest.mark.parametrize("record_type,field", [("destinations", "alias"), ("subscriptions", "name")])
def test_null_labels_are_safe_api_validation_errors(client, record_type, field):
    dest = client.post(PREFIX + "/destinations", json={"alias": "x", "kind": "test"}).json()
    record_id = dest["id"]
    if record_type == "subscriptions":
        strategy = client.get(PREFIX + "/strategies").json()["items"][0]["strategy_id"]
        sub = client.post(PREFIX + "/subscriptions", json={"name": "sub", "destination_id": dest["id"], "strategy_id": strategy}).json()
        record_id = sub["id"]
    for body in ({"version": 1, field: None}, {"version": 1, field: None, "signing_secret": "fake-sensitive-secret"}):
        response = client.patch(PREFIX + "/" + record_type + "/" + record_id, json=body)
        assert response.status_code == 422 and response.json()["error"] == "invalid_request"
        assert "fake-sensitive-secret" not in response.text
    record = next(item for item in client.get(PREFIX + "/" + record_type).json()["items"] if item["id"] == record_id)
    assert record["version"] == 1 and record[field] == ("x" if field == "alias" else "sub")
    updated = client.patch(PREFIX + "/" + record_type + "/" + record_id, json={"version": 1, "enabled": True})
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert updated.json()[field] == record[field]


def test_uppercase_scheme_duplicate_is_rejected_without_secret_echo(client):
    first = client.post(PREFIX + "/destinations", json={"alias": "first", "kind": "test"}).json()
    second = client.post(PREFIX + "/destinations", json={"alias": "second", "kind": "test"}).json()
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/fake-sensitive-target"
    path = PREFIX + "/destinations/"
    assert client.put(path + first["id"] + "/credentials", json={"version": 1, "webhook_url": url}).status_code == 200
    response = client.put(path + second["id"] + "/credentials", json={"version": 1, "webhook_url": url.replace("https://", "HTTPS://", 1)})
    assert response.status_code == 422 and response.json()["error"] == "invalid_webhook"
    assert "fake-sensitive-target" not in response.text
