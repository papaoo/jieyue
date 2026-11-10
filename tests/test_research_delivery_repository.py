from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.repositories.research_delivery_repository import ResearchDeliveryRepository, ManagementError
from src.services.research_delivery.credentials import CredentialVault
from src.services.research_delivery.subscriptions import ResearchDeliveryService


@pytest.fixture
def service(tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "isolated.db").as_posix(), connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE prior_data(value TEXT)"))
        conn.execute(text("INSERT INTO prior_data VALUES ('preserve')"))
    repo = ResearchDeliveryRepository(sessionmaker(bind=engine))
    result = ResearchDeliveryService(repo, CredentialVault(tmp_path / "vault"))
    yield result
    with engine.connect() as conn:
        assert conn.execute(text("SELECT value FROM prior_data")).scalar_one() == "preserve"
    engine.dispose()


def test_ten_strategies_disabled_defaults_and_versions(service):
    strategies = service.strategies()
    assert len(strategies) == 10
    destination = service.create_destination({"alias": "T00", "kind": "test"})
    assert destination["enabled"] is False and destination["version"] == 1
    for strategy in strategies:
        subscription = service.create_subscription({"name": strategy["display_name"], "destination_id": destination["id"], "strategy_id": strategy["strategy_id"], "top_n": 5, "planned_time": "15:10", "llm_mode": "off"})
        assert subscription["enabled"] is False
    changed = service.update_destination(destination["id"], {"version": 1, "enabled": True})
    assert changed["version"] == 2
    with pytest.raises(ManagementError) as error:
        service.update_destination(destination["id"], {"version": 1, "alias": "stale"})
    assert error.value.status_code == 409


def test_credentials_write_only_duplicates_and_rotation(service):
    one = service.create_destination({"alias": "one", "kind": "test"})
    two = service.create_destination({"alias": "two", "kind": "strategy"})
    data = {"version": 1, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake-secret-url", "signing_secret": "fake-secret", "keyword": "阶跃"}
    configured = service.set_credentials(one["id"], data)
    assert configured["version"] == 2 and configured["signing_configured"] is True
    assert "fake-secret" not in repr(configured)
    with pytest.raises(ManagementError) as error:
        service.set_credentials(two["id"], data)
    assert error.value.code == "duplicate_destination"
    with service.repository.session_factory() as session:
        raw = session.execute(text("SELECT * FROM research_delivery_destinations")).all()
        assert "fake-secret" not in repr(raw)
    updated = service.set_credentials(one["id"], {**data, "version": 2, "signing_secret": "rotated"})
    assert updated["version"] == 3


def test_unknown_records_and_strategies(service):
    with pytest.raises(ManagementError) as error:
        service.update_destination("missing", {"version": 1, "enabled": True})
    assert error.value.status_code == 404
    dest = service.create_destination({"alias": "x", "kind": "test"})
    with pytest.raises(ManagementError) as error:
        service.create_subscription({"name": "bad", "destination_id": dest["id"], "strategy_id": "unknown", "top_n": 5, "planned_time": "15:10", "llm_mode": "off"})
    assert error.value.code == "invalid_strategy"


def test_planned_defaults_and_blank_secret_preserved(service):
    dest = service.create_destination({"alias": "x", "kind": "test"})
    sub = service.create_subscription({"name": "sub", "destination_id": dest["id"], "strategy_id": service.strategies()[0]["strategy_id"]})
    assert sub["llm_mode"] == "explain_only" and sub["planned_time"] == "17:10"
    saved = service.set_credentials(dest["id"], {"version": 1, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake", "signing_secret": "original"})
    old = service.repository.credential_binding(dest["id"])
    with pytest.raises(ManagementError) as error:
        service.set_credentials(dest["id"], {"version": saved["version"], "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/new", "signing_secret": ""})
    assert error.value.code == "signing_secret_required"
    service.set_credentials(dest["id"], {"version": saved["version"], "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake", "signing_secret": ""})
    new = service.repository.credential_binding(dest["id"])
    assert old["credential_ref"] != new["credential_ref"]
    assert service.vault.resolve(old["credential_ref"]).webhook_url.endswith("fake")
    assert service.vault.resolve(new["credential_ref"]).signing_secret == "original"


def test_keyword_preserved_when_credentials_rotated(service):
    dest = service.create_destination({"alias": "x", "kind": "test"})
    saved = service.set_credentials(dest["id"], {"version": 1, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake", "keyword": "custom", "signing_secret": "key"})
    updated = service.set_credentials(dest["id"], {"version": saved["version"], "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake"})
    assert updated["keyword"] == "custom" and updated["signing_configured"] is True


def test_concurrent_cas_only_one_writer(service):
    dest = service.create_destination({"alias": "x", "kind": "test"})

    def edit(name):
        try:
            return service.update_destination(dest["id"], {"version": 1, "alias": name})["version"]
        except ManagementError as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, ["first", "second"])) == [2, 409]


def test_concurrent_duplicate_webhook_only_one_owner(service):
    destinations = [service.create_destination({"alias": alias, "kind": "test"}) for alias in ("first", "second")]

    def save(dest):
        try:
            service.set_credentials(dest["id"], {"version": 1, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake"})
            return 200
        except ManagementError as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, destinations)) == [200, 409]


def test_uppercase_scheme_cannot_register_same_target_twice(service):
    destinations = [service.create_destination({"alias": alias, "kind": "test"}) for alias in ("first", "second")]
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/fake-case-target"
    service.set_credentials(destinations[0]["id"], {"version": 1, "webhook_url": url})
    with pytest.raises(ManagementError) as error:
        service.set_credentials(destinations[1]["id"], {"version": 1, "webhook_url": url.replace("https://", "HTTPS://", 1)})
    assert error.value.code == "invalid_webhook"
    assert service.repository.credential_binding(destinations[1]["id"])["credential_ref"] is None


@pytest.mark.parametrize("record_type", ["destination", "subscription"])
def test_null_labels_are_safe_service_validation_errors(service, record_type):
    dest = service.create_destination({"alias": "x", "kind": "test"})
    if record_type == "destination":
        operation = service.update_destination
        record_id, field = dest["id"], "alias"
    else:
        sub = service.create_subscription({"name": "sub", "destination_id": dest["id"], "strategy_id": service.strategies()[0]["strategy_id"]})
        operation = service.update_subscription
        record_id, field = sub["id"], "name"
    with pytest.raises(ManagementError) as error:
        operation(record_id, {"version": 1, field: None})
    assert error.value.status_code == 422 and error.value.code == "invalid_request"
    updated = operation(record_id, {"version": 1, "enabled": True})
    assert updated["version"] == 2 and updated[field] == ("x" if field == "alias" else "sub")
