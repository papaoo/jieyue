import json
from datetime import timedelta

import pytest

from tests.test_research_delivery_api import client, PREFIX
from tests.test_research_delivery_aggregation import research, saved_run
from tests.test_research_delivery_outbox import enabled_target
from src.config import Config
from src.storage import ScreeningRun, Base, utc_naive_now
from src.repositories.research_delivery_models import DeliveryOutbox, ResearchLLMAttempt
from src.repositories.research_delivery_repository import ResearchDeliveryRepository, ManagementError
from src.services.research_delivery.publisher import ResearchPublisher, TEST_MESSAGE
from src.services.research_delivery.explanation import ExplanationService
from tests.test_research_delivery_explanation import model_config
from src.notification_sender.feishu_sender import DeliveryOutcome
from api.v1.endpoints import research_delivery as endpoint


def setup_api(client):
    service = client.app.dependency_overrides[endpoint.get_service]()
    publisher = ResearchPublisher(service, config=Config(), sender=lambda *a: DeliveryOutcome("sent"))
    client.app.dependency_overrides[endpoint.get_publisher] = lambda: publisher
    dest = client.post(PREFIX + "/destinations", json={"alias": "test", "kind": "test"}).json()
    strategy = client.get(PREFIX + "/strategies").json()["items"][0]["strategy_id"]
    sub = client.post(PREFIX + "/subscriptions", json={"name": "sub", "destination_id": dest["id"], "strategy_id": strategy}).json()
    with publisher.repository.session_factory() as session:
        Base.metadata.create_all(session.get_bind(), tables=[ScreeningRun.__table__])
    saved_run(publisher, sub)
    return publisher, sub, dest


def test_api_real_source_preview_blocked_actions_and_fixed_test(client):
    publisher, sub, dest = setup_api(client)
    assert client.get(PREFIX + "/sources", params={"strategy_id": sub["strategy_id"]}).json()["items"][0]["source_run_id"] == "run-one"
    report = client.post(PREFIX + f"/subscriptions/{sub['id']}/preview", json={"source_run_id": "run-one"}).json()
    assert report["llm"]["status"] == "off" and report["status"] == "blocked"
    assert client.get(PREFIX + "/publications").json()["items"][0]["id"] == report["id"]
    assert client.get(PREFIX + "/publications/" + report["id"]).json()["content_hash"] == report["content_hash"]
    for action in ("approve", "send"):
        response = client.post(PREFIX + f"/publications/{report['id']}/{action}", json={"content_hash": report["content_hash"]})
        assert response.status_code == 409 and response.json()["error"] == "publication_blocked"
    assert client.get(PREFIX + "/deliveries").json() == {"items": []}
    dest = enabled_target(publisher, dest)
    response = client.post(PREFIX + f"/destinations/{dest['id']}/test", json={"version": dest["version"], "confirm": True, "request_id": "api-request"})
    assert response.status_code == 200 and response.json()["status"] == "sent"
    assert client.get(PREFIX + "/deliveries/" + response.json()["id"]).json()["attempt_count"] == 1
    capability = client.get(PREFIX + "/capabilities").json()
    assert capability["test_message"] == TEST_MESSAGE and capability["formal_publication_available"] is False
    assert "fake" not in response.text


@pytest.mark.parametrize("body", [{"version": 1, "confirm": False, "request_id": "request-one"}, {"version": 1, "confirm": 1, "request_id": "request-one"}, {"version": 1, "confirm": True}, {"version": 1, "confirm": True, "request_id": "request-one", "text": "custom"}])
def test_test_confirmation_strict_no_custom_text(client, body):
    assert client.post(PREFIX + "/destinations/missing/test", json=body).status_code == 422


def test_all_new_mutations_auth_csrf_and_preview_injection(client, monkeypatch):
    _, sub, _ = setup_api(client)
    path = PREFIX + f"/subscriptions/{sub['id']}/preview"
    assert client.post(path, json={"source_run_id": "run-one", "candidates": [{"code": "fake"}], "w1_verified": True}).status_code == 422
    assert client.post(path, json={"source_run_id": "run-one"}, headers={"origin": "https://evil.example"}).status_code == 403
    monkeypatch.setattr(endpoint.auth, "is_auth_enabled", lambda: False)
    for url in ("/publications", "/deliveries", "/sources?strategy_id=unknown"):
        assert client.get(PREFIX + url).status_code == 403
    assert client.post(path, json={"source_run_id": "run-one", "use_llm": True}).status_code == 403


def test_pending_pause_rotation_expiry_and_restart(research):
    publisher, _, dest = research
    dest = enabled_target(publisher, dest)
    pending = publisher.reserve_test(dest["id"], dest["version"], True, "pending-first")
    dest = publisher.service.update_destination(dest["id"], {"version": dest["version"], "enabled": False})
    assert publisher.repository.deliveries(pending["id"])["status"] == "suppressed"
    dest = publisher.service.update_destination(dest["id"], {"version": dest["version"], "enabled": True})
    pending = publisher.reserve_test(dest["id"], dest["version"], True, "pending-rotate")
    dest = publisher.service.set_credentials(dest["id"], {"version": dest["version"], "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake", "signing_secret": "rotated"})
    assert publisher.repository.deliveries(pending["id"])["status"] == "suppressed"
    pending = publisher.reserve_test(dest["id"], dest["version"], True, "pending-expired")
    with publisher.repository.session_factory() as session, session.begin():
        session.get(DeliveryOutbox, pending["id"]).expires_at = utc_naive_now() - timedelta(seconds=1)
    restarted = ResearchDeliveryRepository(publisher.repository.session_factory)
    assert restarted.deliveries(pending["id"])["status"] == "suppressed"
    assert restarted.deliveries(pending["id"])["attempt_count"] == 0


def test_model_failure_once_across_retry_restart_and_usage_redacted(research):
    publisher, sub, _ = research
    saved_run(publisher, sub)
    sub = publisher.service.update_subscription(sub["id"], {"version": sub["version"], "llm_mode": "explain_only"})
    calls = []
    def failed(prompt, *a, **k):
        calls.append(prompt)
        return "not JSON", "openai/vendor/model", {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9, "secret": "must-not-store"}
    publisher.explanations = ExplanationService(publisher.repository, model_config(), completion=failed)
    one = publisher.preview(sub["id"], "run-one", True)
    publisher.explanations = ExplanationService(ResearchDeliveryRepository(publisher.repository.session_factory), model_config(), completion=lambda *a, **k: pytest.fail("already spent"))
    two = publisher.preview(sub["id"], "run-one", True)
    assert len(calls) == 1 and one["llm"] == two["llm"]
    assert one["llm"]["usage"]["total_tokens"] == 9
    assert one["llm"]["reason"] == "invalid_model_output"
    with publisher.repository.session_factory() as session:
        rows = session.query(ResearchLLMAttempt).all()
        assert len(rows) == 1 and "must-not-store" not in rows[0].result_json


def test_off_never_resolves_or_calls_model(research):
    publisher, sub, _ = research
    saved_run(publisher, sub)
    publisher.explanations = ExplanationService(publisher.repository, model_config(), completion=lambda *a, **k: pytest.fail("off must never call"))
    assert publisher.preview(sub["id"], "run-one", True)["llm"]["status"] == "off"
    with publisher.repository.session_factory() as session:
        assert session.query(ResearchLLMAttempt).count() == 0
