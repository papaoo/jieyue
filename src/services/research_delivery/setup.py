"""One atomic, idempotent setup command; no transport/model/configuration reads."""
import hashlib
import json
import uuid

from sqlalchemy import select

from api.v1.schemas.research_delivery import SetupCreate
from src.repositories.research_delivery_models import DeliveryDestination, DeliverySubscription, DeliverySetupReceipt
from src.repositories.research_delivery_repository import ManagementError, destination_public, subscription_public
from src.services.research_delivery.credentials import CredentialError, credential_fingerprint


def _public(session, receipt):
    target = session.get(DeliveryDestination, receipt.destination_id)
    subscription = session.get(DeliverySubscription, receipt.subscription_id)
    if target is None or subscription is None:
        raise ManagementError("setup_record_unavailable", "Saved setup is unavailable", 409)
    return {"request_id": receipt.request_id, "destination": destination_public(target),
            "subscription": subscription_public(subscription)}


def read_setup(service, request_id):
    with service.repository.session_factory() as session:
        receipt = session.get(DeliverySetupReceipt, request_id)
        if receipt is None:
            raise ManagementError("setup_not_found", "No committed setup found", 404)
        return _public(session, receipt)


def create_setup(service, body: SetupCreate):
    category = next((row for row in service.categories() if row["category_id"] == body.strategy_id), None)
    if category is None:
        raise ManagementError("invalid_strategy", "Unknown research category")
    # Digest includes the secret-bearing request but neither it nor plaintext is public/logged.
    digest = hashlib.sha256(json.dumps(body.model_dump(exclude={"request_id"}), sort_keys=True,
                                      ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    with service.repository.atomic() as session:
        receipt = session.get(DeliverySetupReceipt, body.request_id)
        if receipt is not None:
            if receipt.payload_hash != digest:
                raise ManagementError("setup_request_conflict", "Setup request content changed", 409)
            return _public(session, receipt)
        if body.new_destination is not None:
            connection = body.new_destination
            try:
                fingerprint = credential_fingerprint(connection.webhook_url)
            except CredentialError:
                raise ManagementError("invalid_webhook", "Invalid official Feishu webhook") from None
            if session.scalar(select(DeliveryDestination.id).where(DeliveryDestination.credential_fingerprint == fingerprint)):
                raise ManagementError("duplicate_destination", "Webhook already assigned; select existing group", 409)
            try:
                reference = service.vault.store(connection.webhook_url, connection.signing_secret)
            except CredentialError:
                raise ManagementError("credential_storage_unavailable", "Protected storage unavailable", 503) from None
            target = DeliveryDestination(id=uuid.uuid4().hex, alias=connection.alias,
                kind=category["destination_kind"], enabled=False, version=1,
                credential_ref=reference, credential_fingerprint=fingerprint,
                signing_configured=bool(connection.signing_secret), keyword=connection.keyword)
            session.add(target)
            session.flush()
        else:
            target = session.get(DeliveryDestination, body.destination_id)
            if target is None:
                raise ManagementError("not_found", "Destination not found", 404)
            if target.version != body.destination_version:
                raise ManagementError("version_conflict", "Destination changed; refresh first", 409)
            if not target.credential_ref:
                raise ManagementError("destination_unconfigured", "Save group connection first", 409)
            if session.scalar(select(DeliverySubscription.id).where(
                    DeliverySubscription.destination_id == target.id, DeliverySubscription.strategy_id == body.strategy_id)):
                raise ManagementError("duplicate_subscription", "This group already has this category", 409)
        subscription = DeliverySubscription(id=uuid.uuid4().hex, destination_id=target.id,
            name=body.name, strategy_id=body.strategy_id, top_n=body.top_n, llm_mode=body.llm_mode,
            planned_time=body.planned_time, enabled=False, version=1)
        session.add(subscription)
        session.flush()
        receipt = DeliverySetupReceipt(request_id=body.request_id, payload_hash=digest,
            destination_id=target.id, subscription_id=subscription.id)
        session.add(receipt)
        session.flush()
        return _public(session, receipt)
