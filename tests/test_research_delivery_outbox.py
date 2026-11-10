import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import requests

from tests.test_research_delivery_aggregation import research
from src.notification_sender.feishu_sender import send_once, DeliveryOutcome
from src.repositories.research_delivery_models import DeliveryOutbox
from src.repositories.research_delivery_repository import ManagementError
from src.storage import utc_naive_now


def enabled_target(publisher, dest):
    saved = publisher.service.set_credentials(dest["id"], {"version": dest["version"], "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake", "signing_secret": "fake-secret"})
    return publisher.service.update_destination(dest["id"], {"version": saved["version"], "enabled": True})


def test_durable_sending_and_idempotent_concurrent_test(research):
    publisher, _, dest = research
    dest = enabled_target(publisher, dest)
    calls = []
    def sender(credentials, payload):
        with publisher.repository.session_factory() as session:
            assert session.query(DeliveryOutbox).one().status == "sending"
        calls.append(copy.deepcopy(payload))
        return DeliveryOutcome("sent", None)
    publisher.sender = sender
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda _: publisher.test(dest["id"], dest["version"], True, "same-request"), range(2)))
    assert len(calls) == 1
    assert len({r["id"] for r in records}) == 1
    result = publisher.test(dest["id"], dest["version"], True, "same-request")
    assert result["status"] == "sent" and result["attempt_count"] == 1
    assert "非投资建议" in calls[0]["content"]["text"]


def test_unknown_no_retry_and_version_request_binding(research):
    publisher, _, dest = research
    dest = enabled_target(publisher, dest)
    calls = []
    publisher.sender = lambda *a: calls.append(a) or DeliveryOutcome("delivery_unknown", "network_unknown")
    one = publisher.test(dest["id"], dest["version"], True, "unknown-request")
    two = publisher.test(dest["id"], dest["version"], True, "unknown-request")
    assert one["id"] == two["id"] and len(calls) == 1 and two["status"] == "delivery_unknown"
    changed = publisher.service.update_destination(dest["id"], {"version": dest["version"], "enabled": False})
    with pytest.raises(ManagementError):
        publisher.test(dest["id"], changed["version"], True, "new-request")
    with pytest.raises(ManagementError) as error:
        publisher.test(dest["id"], changed["version"], True, "unknown-request")
    assert error.value.code == "idempotency_conflict"


def test_sending_recovery_never_requeues(research):
    publisher, _, dest = research
    dest = enabled_target(publisher, dest)
    record = publisher.reserve_test(dest["id"], dest["version"], True, "crash-request")
    with publisher.repository.session_factory() as session, session.begin():
        row = session.get(DeliveryOutbox, record["id"])
        row.status = "sending"
        row.sending_at = utc_naive_now() - timedelta(minutes=5)
        row.attempt_count = 1
    publisher.sender = lambda *a: pytest.fail("must not resend")
    assert publisher.test(dest["id"], dest["version"], True, "crash-request")["status"] == "delivery_unknown"


@pytest.mark.parametrize("http_code, body, state", [(200, {"code": 0}, "sent"), (200, {"code": False}, "delivery_unknown"), (200, {"code": "0"}, "delivery_unknown"), (200, {"code": 1}, "rejected"), (500, {"code": 1}, "delivery_unknown"), (302, {"code": 0}, "rejected"), (200, [], "delivery_unknown")])
def test_send_once_strict_business_code_and_exact_payload(http_code, body, state):
    from src.services.research_delivery.credentials import Credentials
    payload = {"msg_type": "text", "content": {"text": "approved"}}
    original = copy.deepcopy(payload)
    calls = []
    class Response:
        status_code = http_code
        def json(self): return body
    def post(url, **kwargs):
        calls.append(kwargs)
        return Response()
    outcome = send_once(Credentials("https://open.feishu.cn/open-apis/bot/v2/hook/fake", "signing"), payload, transport=post, now=lambda: 100)
    assert outcome.status == state and len(calls) == 1
    assert calls[0]["allow_redirects"] is False and calls[0]["timeout"] <= 30
    wire = calls[0]["json"]
    assert {k: v for k, v in wire.items() if k not in ("timestamp", "sign")} == original
    assert payload == original
    import base64, hashlib, hmac
    assert wire["sign"] == base64.b64encode(hmac.new(b"100\nsigning", digestmod=hashlib.sha256).digest()).decode()


def test_timeout_and_invalid_json_unknown_no_secret_exception():
    from src.services.research_delivery.credentials import Credentials
    def post(*a, **k): raise requests.Timeout("SECRET URL")
    result = send_once(Credentials("https://open.feishu.cn/open-apis/bot/v2/hook/fake", ""), {"msg_type": "text", "content": {"text": "x"}}, transport=post)
    assert result.status == "delivery_unknown" and "SECRET" not in repr(result)
