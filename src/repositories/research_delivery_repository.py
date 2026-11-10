"""Transactional storage with injected sessions and explicit public allowlists."""
from __future__ import annotations

import uuid
import json
import os
import re
from contextlib import contextmanager
from datetime import date, timedelta

from sqlalchemy import select, update, text, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.repositories.research_delivery_models import (DeliveryDestination, DeliverySubscription, DeliverySetupReceipt,
    ResearchPublication, ResearchLLMAttempt, DeliveryOutbox)
from src.storage import Base, utc_naive_now
from src.repositories.automation_models import (ResearchBatch, AutomationControl, AutomationJob,
    AutomationRunRequest, PublicationRelease, AutomationProgress)


class ManagementError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


def destination_public(row):
    return {key: getattr(row, key) for key in (
        "id", "alias", "kind", "enabled", "version", "signing_configured", "keyword", "created_at", "updated_at"
    )} | {"credential_configured": bool(row.credential_ref)}


def subscription_public(row):
    return {key: getattr(row, key) for key in (
        "id", "name", "destination_id", "strategy_id", "top_n", "llm_mode", "planned_time", "enabled", "version", "created_at", "updated_at"
    )}


class ResearchDeliveryRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        with session_factory() as session:
            Base.metadata.create_all(session.get_bind(), tables=[DeliveryDestination.__table__, DeliverySubscription.__table__, DeliverySetupReceipt.__table__])
            Base.metadata.create_all(session.get_bind(), tables=[ResearchPublication.__table__, ResearchLLMAttempt.__table__, DeliveryOutbox.__table__])
            Base.metadata.create_all(session.get_bind(), tables=[ResearchBatch.__table__, AutomationControl.__table__,
                AutomationJob.__table__, AutomationRunRequest.__table__, PublicationRelease.__table__, AutomationProgress.__table__])

    def _public(self, model, row):
        return destination_public(row) if model is DeliveryDestination else subscription_public(row)

    def list(self, model):
        with self.session_factory() as session:
            return [self._public(model, row) for row in session.scalars(select(model).order_by(model.created_at, model.id))]

    def get(self, model, record_id):
        with self.session_factory() as session:
            row = session.get(model, record_id)
            if row is None:
                raise ManagementError("not_found", "Record not found", 404)
            return self._public(model, row)

    def credential_binding(self, record_id):
        """Internal server-only binding. Never return this object from an endpoint."""
        with self.session_factory() as session:
            row = session.get(DeliveryDestination, record_id)
            if row is None:
                raise ManagementError("not_found", "Destination not found", 404)
            return {key: getattr(row, key) for key in (
                "id", "kind", "enabled", "version", "credential_ref", "keyword"
            )}

    def create(self, model, values):
        try:
            with self.session_factory() as session, session.begin():
                if model is DeliverySubscription and session.get(DeliveryDestination, values["destination_id"]) is None:
                    raise ManagementError("not_found", "Destination not found", 404)
                row = model(id=uuid.uuid4().hex, enabled=False, version=1, **values)
                session.add(row)
                session.flush()
                return self._public(model, row)
        except SQLAlchemyError:
            raise ManagementError("storage_unavailable", "Storage operation failed", 503) from None

    def update(self, model, record_id, version, values):
        try:
            with self.session_factory() as session, session.begin():
                if session.get(model, record_id) is None:
                    raise ManagementError("not_found", "Record not found", 404)
                if model is DeliverySubscription and "destination_id" in values:
                    if session.get(DeliveryDestination, values["destination_id"]) is None:
                        raise ManagementError("not_found", "Destination not found", 404)
                result = session.execute(update(model).where(model.id == record_id, model.version == version).values(**values, version=version + 1, updated_at=utc_naive_now()))
                if result.rowcount != 1:
                    raise ManagementError("version_conflict", "Record changed; refresh before editing", 409)
                # Any target/config revision invalidates pending approval, even re-enabling.
                condition = DeliveryOutbox.destination_id == record_id
                if model is DeliverySubscription:
                    condition = DeliveryOutbox.publication_id.in_(select(ResearchPublication.id).where(ResearchPublication.subscription_id == record_id))
                session.execute(update(DeliveryOutbox).where(condition, DeliveryOutbox.status == "pending").values(status="suppressed", error_code="configuration_changed", completed_at=utc_naive_now()))
                session.expire_all()
                return self._public(model, session.get(model, record_id))
        except IntegrityError:
            raise ManagementError("duplicate_destination", "This webhook is already assigned to another destination", 409) from None
        except SQLAlchemyError:
            raise ManagementError("storage_unavailable", "Storage operation failed", 503) from None

    @contextmanager
    def atomic(self):
        """Durable serial SQLite writes. Never keep this transaction across network I/O."""
        try:
            with self.session_factory() as session:
                if session.get_bind().dialect.name != "sqlite":
                    raise ManagementError("storage_unsupported", "Atomic research delivery requires SQLite in this slice", 503)
                session.execute(text("BEGIN IMMEDIATE"))
                try:
                    yield session
                    session.commit()
                except BaseException:
                    session.rollback()
                    raise
        except SQLAlchemyError:
            raise ManagementError("storage_unavailable", "Research storage operation failed", 503) from None

    def reserve_llm(self, input_key, business_day):
        with self.atomic() as session:
            row = session.scalar(select(ResearchLLMAttempt).where(ResearchLLMAttempt.input_key == input_key))
            if row is not None:
                return {"id": row.id, "created": False, "status": row.status, "result": json.loads(row.result_json) if row.result_json else None}
            used = session.scalar(select(func.count()).select_from(ResearchLLMAttempt).where(ResearchLLMAttempt.business_day == business_day))
            limit = 20
            override = os.environ.get('RESEARCH_DELIVERY_LLM_BUDGET_OVERRIDE', '')
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}:40', override):
                try:
                    if date.fromisoformat(override[:10]).isoformat() == business_day:
                        limit = 40
                except ValueError:
                    pass
            if used >= limit:
                return None
            row = ResearchLLMAttempt(id=uuid.uuid4().hex, input_key=input_key, business_day=business_day, status="requesting")
            session.add(row)
            session.flush()
            return {"id": row.id, "created": True, "status": "requesting", "result": None}

    def finish_llm(self, record_id, result):
        with self.atomic() as session:
            session.execute(update(ResearchLLMAttempt).where(ResearchLLMAttempt.id == record_id, ResearchLLMAttempt.status == "requesting").values(status=result["status"], result_json=json.dumps(result, ensure_ascii=False, allow_nan=False)))

    @staticmethod
    def delivery_public(row):
        return {key: getattr(row, key) for key in ("id", "request_id", "kind", "destination_id", "destination_version", "publication_id", "payload_hash", "status", "error_code", "attempt_count", "expires_at", "sending_at", "completed_at", "created_at")}

    def recover_outbox(self, session):
        now = utc_naive_now()
        session.execute(update(DeliveryOutbox).where(DeliveryOutbox.status == "sending", DeliveryOutbox.sending_at <= now - timedelta(seconds=90)).values(status="delivery_unknown", error_code="abandoned_sending", completed_at=now))
        session.execute(update(DeliveryOutbox).where(DeliveryOutbox.status == "pending", DeliveryOutbox.expires_at <= now).values(status="suppressed", error_code="approval_expired", completed_at=now))

    def deliveries(self, record_id=None):
        with self.atomic() as session:
            self.recover_outbox(session)
            if record_id:
                row = session.get(DeliveryOutbox, record_id)
                if row is None:
                    raise ManagementError("not_found", "Delivery not found", 404)
                return self.delivery_public(row)
            return [self.delivery_public(row) for row in session.scalars(select(DeliveryOutbox).order_by(DeliveryOutbox.created_at.desc()).limit(100))]
