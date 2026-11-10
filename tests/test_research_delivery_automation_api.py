"""Admin contract and real lifespan seam without live config/database access."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient as LifecycleClient
import pytest

from tests.test_research_delivery_api import APIClient, PREFIX
from tests.test_research_delivery_automation import runtime, formal, research
from api.v1.endpoints import research_delivery as endpoint
from src.auth import COOKIE_NAME


@pytest.fixture
def automation_client(runtime, monkeypatch):
    worker, publisher, sub, *_ = runtime
    app = FastAPI()
    app.include_router(endpoint.router, prefix=PREFIX)
    app.dependency_overrides[endpoint.get_service] = lambda: publisher.service
    app.state.research_automation_factory = lambda app: worker
    monkeypatch.setattr(endpoint.auth, 'is_auth_enabled', lambda: True)
    monkeypatch.setattr(endpoint.auth, 'verify_session', lambda value: value == 'admin-test')
    client = APIClient(app)
    client.cookies.set(COOKIE_NAME, 'admin-test')
    return client, worker, sub


def test_automation_api_status_activation_and_stable_receipt(automation_client):
    client, worker, sub = automation_client
    state = client.get(PREFIX + '/automation')
    assert state.status_code == 200 and not state.json()['enabled']
    assert worker._thread is None  # GET never starts resources.
    response = client.put(PREFIX + '/automation', json={'version': 1, 'enabled': True, 'subscription_ids': [sub['id']]})
    assert response.status_code == 200 and response.json()['version'] == 2
    assert client.put(PREFIX + '/automation', json={'version': 1, 'enabled': False, 'subscription_ids': []}).status_code == 409
    first = client.post(PREFIX + '/automation/run', json={'request_id': 'api-request-001'})
    second = client.post(PREFIX + '/automation/run', json={'request_id': 'api-request-001'})
    assert first.status_code == second.status_code == 202 and first.json() == second.json()
    assert client.get(PREFIX + '/capabilities').json()['formal_publication_available'] is True
    categories = client.get(PREFIX + '/categories').json()['items']
    assert all(r['producer_status'] == 'live_on_demand' for r in categories if r['category_id'] != 'system_status')
    assert next(r for r in categories if r['category_id'] == 'system_status')['producer_status'] == 'not_connected'


@pytest.mark.parametrize('body', [
    {'version': 1, 'enabled': True, 'subscription_ids': ['unknown']},
    {'version': 1, 'enabled': True, 'subscription_ids': [], 'stocks': ['600000']},
    {'version': True, 'enabled': True, 'subscription_ids': []},
])
def test_automation_invalid_control_is_safe(automation_client, body):
    client, worker, _ = automation_client
    assert client.put(PREFIX + '/automation', json=body).status_code == 422
    assert worker.status()['version'] == 1


def test_automation_run_rejects_injection_auth_and_origin(automation_client, monkeypatch):
    client, _, _ = automation_client
    assert client.post(PREFIX + '/automation/run', json={'request_id': 'api-request-001', 'date': '2026-10-14'}).status_code == 422
    for method, path, body in [('PUT', '/automation', {'version': 1, 'enabled': False, 'subscription_ids': []}),
        ('POST', '/automation/run', {'request_id': 'api-request-001'})]:
        assert client.request(method, PREFIX + path, json=body, headers={'origin': 'http://evil.test'}).status_code == 403
    client.cookies.clear()
    assert client.get(PREFIX + '/automation').status_code == 401
    monkeypatch.setattr(endpoint.auth, 'is_auth_enabled', lambda: False)
    assert client.get(PREFIX + '/automation').status_code == 403


@pytest.mark.parametrize('suppress_research', [False, True])
def test_lifespan_factory_shared_once_and_legacy_flags_do_not_disable(runtime, monkeypatch, suppress_research):
    import api.app as app_module
    import api.middlewares.auth as auth_middleware
    import src.services.name_to_code_resolver as resolver
    worker, publisher, _, _, _, calls, sends = runtime
    started, stopped, factories = [], [], []
    # This checks exact lifecycle ownership, not worker timing; native thread is
    # exercised separately against deterministic synchronization events.
    worker.start = lambda: started.append(True)
    worker.stop = lambda: stopped.append(True)
    def factory(app):
        factories.append(app)
        return worker
    class Legacy:
        def __init__(self, **kw): pass
        def reconcile_from_config(self, **kw): pass
        def stop(self): pass
    monkeypatch.setattr(app_module, 'RuntimeSchedulerService', Legacy)
    monkeypatch.setattr(app_module, 'SystemConfigService', lambda **kw: SimpleNamespace())
    monkeypatch.setattr(app_module, '_schedule_stock_index_background_refresh', lambda *a: None)
    monkeypatch.setattr(resolver, 'warmup_akshare_cache', lambda: None)
    monkeypatch.setenv(app_module.RUNTIME_SCHEDULER_SUPPRESS_START_ENV, 'true')
    monkeypatch.setenv('SCHEDULE_ENABLED', 'false')
    monkeypatch.setenv('DSA_RESEARCH_AUTOMATION_SUPPRESS_START', str(suppress_research).lower())
    monkeypatch.setattr(endpoint.auth, 'is_auth_enabled', lambda: True)
    monkeypatch.setattr(endpoint.auth, 'verify_session', lambda value: True)
    monkeypatch.setattr(auth_middleware, 'is_auth_enabled', lambda: False)
    app = app_module.create_app(research_automation_factory=factory)
    with LifecycleClient(app) as client:
        assert client.get(PREFIX + '/automation', headers={'cookie': COOKIE_NAME + '=test'}).status_code == 200
        assert app.state.research_delivery_service is publisher.service
        assert app.state.research_automation is worker
        assert len(factories) == 1
        assert len(started) == (0 if suppress_research else 1)
    assert len(stopped) == 1 and not calls and not sends


def test_bare_dependency_override_with_service_config_is_not_native(automation_client, monkeypatch):
    client, worker, sub = automation_client
    del client.app.state.research_automation_factory
    injected = object()
    monkeypatch.setattr(worker.service, 'config', injected, raising=False)
    assert client.get(PREFIX + '/automation').status_code == 200
    attached = client.app.state.research_automation
    assert attached.config_provider() is injected and attached._thread is None


def test_native_accessor_explicit_provider_observes_replacement_config(runtime, monkeypatch):
    import api.app as app_module
    import src.config
    worker = runtime[0]
    current = [object()]
    reads = []
    def get_config():
        reads.append(current[0])
        return current[0]
    monkeypatch.setattr(src.config, 'get_config', get_config)
    app = app_module.create_app()
    attached = endpoint.get_app_automation(app, worker.service)
    first = attached.config_provider()
    current[0] = object()
    second = attached.config_provider()
    assert first is not second and reads == [first, second]
    assert attached._thread is None
