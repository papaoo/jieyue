"""Admin-only management endpoints, deliberately separate from legacy sending."""
from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
import threading
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError

from api.v1.schemas.research_delivery import (DestinationCreate, DestinationUpdate, CredentialWrite, SubscriptionCreate,
    SubscriptionUpdate, PreviewRequest, PublicationAction, TestDeliveryRequest, SetupCreate, AutomationUpdate, AutomationRun)
from src import auth
from src.repositories.research_delivery_models import DeliveryDestination, DeliverySubscription
from src.repositories.research_delivery_repository import ResearchDeliveryRepository, ManagementError
from src.services.research_delivery.credentials import CredentialVault
from src.services.research_delivery.subscriptions import ResearchDeliveryService
from src.services.research_delivery.publisher import ResearchPublisher

logger = logging.getLogger(__name__)


class SafeManagementRoute(APIRoute):
    """Pydantic/FastAPI errors must never serialize secret input or error contexts."""
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe(request):
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"error": "invalid_request", "message": "Invalid management fields"})
            except ManagementError as error:
                return JSONResponse(status_code=error.status_code, content={"error": error.code, "message": error.message})
            except SQLAlchemyError:
                return JSONResponse(status_code=503, content={"error": "storage_unavailable", "message": "Research storage unavailable"})
        return safe


def _origin(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def require_admin(request: Request):
    if not auth.is_auth_enabled():
        logger.warning("Research delivery request rejected: auth_required")
        raise ManagementError("auth_required", "Enable administrator authentication before using this workspace", 403)
    cookie = request.cookies.get(auth.COOKIE_NAME)
    if not cookie or not auth.verify_session(cookie):
        raise ManagementError("unauthorized", "Administrator session required", 401)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = _origin(request.headers.get("origin", ""))
        expected = _origin(str(request.base_url))
        if origin is None or origin != expected or request.headers.get("sec-fetch-site") == "cross-site":
            logger.warning(
                "Research delivery cross-origin rejection: origin_present=%s origin_valid=%s "
                "expected_valid=%s scheme_match=%s host_match=%s port_match=%s cross_site=%s",
                "origin" in request.headers, origin is not None, expected is not None,
                bool(origin and expected and origin[0] == expected[0]),
                bool(origin and expected and origin[1] == expected[1]),
                bool(origin and expected and origin[2] == expected[2]),
                request.headers.get("sec-fetch-site") == "cross-site",
            )
            raise ManagementError("cross_origin", "Same-origin request required", 403)


def require_secret_transport(request: Request):
    # Never trust client-supplied X-Forwarded-Proto / X-Forwarded-For here.
    if request.url.scheme == "https":
        return
    peer_is_loopback = False
    host_is_loopback = False
    try:
        peer_is_loopback = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        host = request.url.hostname
        host_is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        host_is_loopback = False
    if not peer_is_loopback or not host_is_loopback:
        logger.warning(
            "Research delivery request rejected: secure_transport_required "
            "peer_loopback=%s host_loopback=%s", peer_is_loopback, host_is_loopback,
        )
        raise ManagementError("secure_transport_required", "Credential writes require HTTPS or an actual loopback connection", 403)


_initialization_lock = threading.Lock()


def get_service(request: Request):
    """Lazy, app-scoped setup; tests override this dependency with isolated storage."""
    service = getattr(request.app.state, "research_delivery_service", None)
    if service is not None:
        return service
    with _initialization_lock:
        service = getattr(request.app.state, "research_delivery_service", None)
        if service is None:
            from api.deps import get_database_manager
            try:
                manager = get_database_manager()
                with manager.get_session() as session:
                    db_file = session.get_bind().url.database
                if not db_file or db_file == ":memory:":
                    raise ManagementError("storage_unavailable", "A persistent data directory is required", 503)
                repository = ResearchDeliveryRepository(manager.get_session)
                service = ResearchDeliveryService(repository, CredentialVault(Path(db_file).absolute().parent / ".research-delivery-vault"))
                from api.deps import get_config_dep
                service.config = get_config_dep()
            except ManagementError:
                raise
            except Exception:
                raise ManagementError("storage_unavailable", "Management storage unavailable", 503) from None
            request.app.state.research_delivery_service = service
    return service


def get_publisher(service=Depends(get_service)):
    # Config() is a no-I/O fallback for explicitly injected management-only services.
    from src.config import Config
    return ResearchPublisher(service, config=getattr(service, "config", None) or Config())


def get_app_automation(app, service=None):
    """Shared lifecycle/request accessor; construction alone never starts work.

    An explicit factory receives the app and returns a runtime owning its injected
    service. Unlike direct dependency calls, this is safe in lifespan tests too.
    """
    with _initialization_lock:
        runtime = getattr(app.state, 'research_automation', None)
        if runtime is None:
            factory = getattr(app.state, 'research_automation_factory', None)
            if factory is not None:
                runtime = factory(app)
            else:
                from src.services.research_delivery.automation import ResearchAutomation
                # get_service uses this same lock; resolve outside it below.
                if service is None:
                    raise ManagementError('service_required', 'Research service required', 503)
                runtime = ResearchAutomation(service,
                    config_provider=getattr(app.state, 'research_automation_config_provider', None))
            if service is not None and runtime.service is not service:
                raise ManagementError('service_mismatch', 'Research runtime/service mismatch', 503)
            app.state.research_delivery_service = runtime.service
            app.state.research_automation = runtime
            runtime.service.automation = runtime
        elif service is not None and runtime.service is not service:
            raise ManagementError('service_mismatch', 'Research runtime/service mismatch', 503)
        return runtime


def get_automation(request: Request, service=Depends(get_service)):
    return get_app_automation(request.app, service)


router = APIRouter(route_class=SafeManagementRoute, dependencies=[Depends(require_admin)])


@router.get('/automation')
def automation_status(runtime=Depends(get_automation)):
    return runtime.status()


@router.put('/automation')
def automation_update(body: AutomationUpdate, runtime=Depends(get_automation)):
    return runtime.update(body.version, body.enabled, body.subscription_ids)


@router.post('/automation/run', status_code=202)
def automation_run(body: AutomationRun, runtime=Depends(get_automation)):
    return runtime.request_run(body.request_id)


@router.post("/setups", status_code=201, dependencies=[Depends(require_secret_transport)])
def setup(body: SetupCreate, service=Depends(get_service)):
    from src.services.research_delivery.setup import create_setup
    return create_setup(service, body)


@router.get("/setups/{request_id}")
def setup_status(request_id: str, service=Depends(get_service)):
    from src.services.research_delivery.setup import read_setup
    return read_setup(service, request_id)


@router.get("/capabilities")
def capabilities(publisher=Depends(get_publisher)):
    return publisher.capabilities()


@router.get("/strategies")
def strategies(service=Depends(get_service)):
    return {"items": service.strategies()}


@router.get("/destinations")
def destinations(service=Depends(get_service)):
    return {"items": service.repository.list(DeliveryDestination)}


@router.get("/categories")
def categories(service=Depends(get_service)):
    return {"items": service.categories()}


@router.post("/destinations", status_code=201)
def create_destination(body: DestinationCreate, service=Depends(get_service)):
    return service.create_destination(body.model_dump())


@router.patch("/destinations/{record_id}")
def update_destination(record_id: str, body: DestinationUpdate, service=Depends(get_service)):
    return service.update_destination(record_id, body.model_dump(exclude_unset=True))


@router.put("/destinations/{record_id}/credentials", dependencies=[Depends(require_secret_transport)])
def write_credentials(record_id: str, body: CredentialWrite, service=Depends(get_service)):
    return service.set_credentials(record_id, body.model_dump(exclude_unset=True))


@router.get("/subscriptions")
def subscriptions(service=Depends(get_service)):
    return {"items": service.repository.list(DeliverySubscription)}


@router.post("/subscriptions", status_code=201)
def create_subscription(body: SubscriptionCreate, service=Depends(get_service)):
    return service.create_subscription(body.model_dump())


@router.patch("/subscriptions/{record_id}")
def update_subscription(record_id: str, body: SubscriptionUpdate, service=Depends(get_service)):
    return service.update_subscription(record_id, body.model_dump(exclude_unset=True))


@router.get("/sources")
def sources(strategy_id: str, publisher=Depends(get_publisher)):
    return {"items": publisher.sources(strategy_id)}


@router.post("/subscriptions/{record_id}/preview", status_code=201)
def preview(record_id: str, body: PreviewRequest, publisher=Depends(get_publisher)):
    return publisher.preview(record_id, body.source_run_id, body.use_llm)


@router.get("/publications")
def publications(publisher=Depends(get_publisher)):
    return {"items": publisher.publications()}


@router.get("/publications/{record_id}")
def publication(record_id: str, publisher=Depends(get_publisher)):
    return publisher.publication(record_id)


@router.post("/publications/{record_id}/approve")
def approve(record_id: str, body: PublicationAction, publisher=Depends(get_publisher)):
    return publisher.approve(record_id, body.content_hash)


@router.post("/publications/{record_id}/send")
def send(record_id: str, body: PublicationAction, publisher=Depends(get_publisher)):
    return publisher.send(record_id, body.content_hash)


@router.post("/destinations/{record_id}/test")
def test_destination(record_id: str, body: TestDeliveryRequest, publisher=Depends(get_publisher)):
    return publisher.test(record_id, body.version, body.confirm, body.request_id)


@router.get("/deliveries")
def deliveries(publisher=Depends(get_publisher)):
    return {"items": publisher.repository.deliveries()}


@router.get("/deliveries/{record_id}")
def delivery(record_id: str, publisher=Depends(get_publisher)):
    return publisher.repository.deliveries(record_id)
