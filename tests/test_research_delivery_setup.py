"""Real isolated API/storage setup acceptance; no real configuration or transport."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from tests.test_research_delivery_api import client, PREFIX  # noqa: F401
from api.v1.endpoints import research_delivery as endpoint
from src.repositories.research_delivery_repository import ResearchDeliveryRepository
from src.services.research_delivery.credentials import CredentialError


def payload(request_id="setup-request-01", **changes):
    return {"request_id": request_id, "name": "S01 放量突破", "strategy_id": "volume_breakout",
            "top_n": 5, "llm_mode": "off", "planned_time": "17:10",
            "new_destination": {"alias": "突破研究群", "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake-setup-token", "signing_secret": "fake-setup-secret", "keyword": "阶跃"}, **changes}


def rows(client):
    return [client.get(PREFIX + "/" + name).json()["items"] for name in ("destinations", "subscriptions")]


def test_setup_commits_pair_and_survives_database_reopen(client):
    body = payload()
    response = client.post(PREFIX + "/setups", json=body)
    assert response.status_code == 201
    saved = response.json()
    assert saved["destination"]["credential_configured"] is True
    assert saved["destination"]["enabled"] is False
    assert saved["subscription"]["destination_id"] == saved["destination"]["id"]
    assert saved["subscription"]["enabled"] is False
    assert "fake-setup" not in response.text
    service = client.app.dependency_overrides[endpoint.get_service]()
    with service.repository.session_factory() as session:
        old_engine = session.get_bind()
        database_url = old_engine.url
    old_engine.dispose()
    reopened = create_engine(database_url, connect_args={"check_same_thread": False})
    try:
        service.repository = ResearchDeliveryRepository(sessionmaker(bind=reopened))
        assert client.get(PREFIX + "/setups/" + body["request_id"]).json() == saved
        assert [len(r) for r in rows(client)] == [1, 1]
    finally:
        reopened.dispose()


def test_setup_two_groups_keep_independent_categories(client):
    first = client.post(PREFIX + "/setups", json=payload()).json()
    second_body = payload("setup-request-02", strategy_id="shrink_pullback")
    second_body["new_destination"]["webhook_url"] += "-second"
    second_body["new_destination"]["alias"] = "回踩研究群"
    response = client.post(PREFIX + "/setups", json=second_body)
    assert response.status_code == 201
    second = response.json()
    assert first["destination"]["id"] != second["destination"]["id"]
    assert second["subscription"]["destination_id"] == second["destination"]["id"]
    assert [len(r) for r in rows(client)] == [2, 2]


def test_setup_vault_failure_has_no_half_saved_records(client, monkeypatch):
    service = client.app.dependency_overrides[endpoint.get_service]()

    def unavailable(*args):
        raise CredentialError()

    monkeypatch.setattr(service.vault, "store", unavailable)
    response = client.post(PREFIX + "/setups", json=payload())
    assert response.status_code == 503
    assert response.json()["error"] == "credential_storage_unavailable"
    assert "fake-setup" not in response.text
    assert [len(r) for r in rows(client)] == [0, 0]
    assert client.get(PREFIX + "/setups/setup-request-01").status_code == 404


def test_setup_replay_and_changed_payload(client):
    body = payload()
    first = client.post(PREFIX + "/setups", json=body)
    assert first.status_code == 201
    assert client.post(PREFIX + "/setups", json=body).json() == first.json()
    changed = client.post(PREFIX + "/setups", json={**body, "top_n": 6})
    assert changed.status_code == 409
    assert changed.json()["error"] == "setup_request_conflict"
    assert [len(r) for r in rows(client)] == [1, 1]


def test_setup_concurrent_replay_creates_one_pair(client):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(PREFIX + "/setups", json=payload()), range(2)))
    assert [r.status_code for r in results] == [201, 201]
    assert results[0].json() == results[1].json()
    assert [len(r) for r in rows(client)] == [1, 1]


def test_setup_duplicate_webhook_does_not_leave_half_group(client):
    assert client.post(PREFIX + "/setups", json=payload()).status_code == 201
    response = client.post(PREFIX + "/setups", json=payload("setup-request-02", strategy_id="shrink_pullback"))
    assert response.status_code == 409
    assert response.json()["error"] == "duplicate_destination"
    assert [len(r) for r in rows(client)] == [1, 1]


@pytest.mark.parametrize("changes", [{"strategy_id": "fake"}, {"top_n": True}, {"planned_time": "25:00"}, {"new_destination": None}, {"destination_id": "x", "destination_version": 1}, {"new_destination": {"alias": "x", "webhook_url": "https://evil.example/fake-setup-token"}}])
def test_setup_rejects_invalid_without_writes_or_secret_echo(client, changes):
    response = client.post(PREFIX + "/setups", json=payload(**changes))
    assert response.status_code == 422
    assert "fake-setup" not in response.text
    assert [len(r) for r in rows(client)] == [0, 0]


def test_setup_existing_target_version_and_duplicate_binding(client):
    first = client.post(PREFIX + "/setups", json=payload())
    assert first.status_code == 201
    dest = first.json()["destination"]
    body = payload("setup-existing-02", new_destination=None, destination_id=dest["id"], destination_version=dest["version"], strategy_id="shrink_pullback")
    assert client.post(PREFIX + "/setups", json={**body, "destination_version": 99}).status_code == 409
    assert client.post(PREFIX + "/setups", json=body).status_code == 201
    assert client.post(PREFIX + "/setups", json={**body, "request_id": "setup-existing-03"}).json()["error"] == "duplicate_subscription"
    assert [len(r) for r in rows(client)] == [1, 2]


def test_setup_database_failure_rolls_back_both_rows(client):
    service = client.app.dependency_overrides[endpoint.get_service]()
    with service.repository.session_factory() as session:
        engine = session.get_bind()

    def fail_subscription(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO research_delivery_subscriptions"):
            from sqlalchemy.exc import OperationalError
            raise OperationalError("isolated failure", {}, Exception("isolated"))

    event.listen(engine, "before_cursor_execute", fail_subscription)
    try:
        response = client.post(PREFIX + "/setups", json=payload())
    finally:
        event.remove(engine, "before_cursor_execute", fail_subscription)
    assert response.status_code == 503
    assert [len(r) for r in rows(client)] == [0, 0]
    assert client.get(PREFIX + "/setups/setup-request-01").status_code == 404


def test_setup_auth_origin_and_secret_transport(client):
    assert client.post(PREFIX + "/setups", json=payload(), headers={"origin": "http://evil.example"}).status_code == 403
    client.peer = ("192.0.2.10", 1234)
    assert client.post(PREFIX + "/setups", json=payload()).status_code == 403
    client.cookies.clear()
    assert client.get(PREFIX + "/setups/setup-request-01").status_code == 401
    assert client.post(PREFIX + "/setups", json=payload()).status_code == 401
