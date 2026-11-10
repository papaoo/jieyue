"""Immutable drafts and explicitly confirmed, single-target connection tests."""
from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta

from sqlalchemy import select

from src.storage import utc_naive_now
from src.notification_sender.feishu_sender import send_once, DeliveryOutcome
from src.repositories.research_delivery_models import DeliverySubscription, DeliveryDestination, ResearchPublication, DeliveryOutbox
from src.repositories.research_delivery_repository import ManagementError
from src.services.research_delivery.aggregation import ResearchAggregator, canonical, content_hash
from src.services.research_delivery.explanation import ExplanationService, MODEL_TIMEOUT_SECONDS
from src.services.research_delivery.rendering import render_draft
from src.services.research_delivery.credentials import CredentialError

TEST_MESSAGE = "阶跃 · 群连接测试\n这是一条人工确认的非金融测试消息，用于验证当前群的单次投递。\n不包含股票研究结论，非投资建议。系统不会自动重发。"


class ResearchPublisher:
    def __init__(self, service, *, config, sender=None):
        self.service = service
        self.repository = service.repository
        self.aggregator = ResearchAggregator(self.repository)
        self.explanations = ExplanationService(self.repository, config)
        self.sender = sender or send_once

    def capabilities(self):
        llm_status = self.explanations.capability()
        runtime = getattr(self.service, 'automation', None)
        state = runtime.status() if runtime is not None else None
        return {"management_enabled": True, "auth_required": True, "credential_transport": "https_or_loopback",
                "llm_modes": ["off", "explain_only"], "llm_available": llm_status == "supported", "llm_status": llm_status,
                "llm_daily_attempt_limit": 20, "llm_timeout_seconds": MODEL_TIMEOUT_SECONDS,
                "scheduler_active": bool(state and state['worker_alive']),
                "sending_enabled": bool(state and state['enabled']), "manual_test_available": True,
                "formal_publication_available": runtime is not None,
                "global_sending_state": ('authorized' if state['enabled'] else 'paused') if state else 'manual_test_only',
                "test_message": TEST_MESSAGE,
                "subscription_scopes": ["a_share_strategy"], "timezone": "Asia/Shanghai"}

    def sources(self, strategy_id):
        self.service._strategy(strategy_id)
        return self.aggregator.sources(strategy_id)

    def preview(self, subscription_id, source_run_id, use_llm=False):
        subscription = self.repository.get(DeliverySubscription, subscription_id)
        if subscription["strategy_id"] not in {row["strategy_id"] for row in self.service.strategies()}:
            raise ManagementError("category_preview_unavailable", "This category is configured but its content producer is not connected", 409)
        destination = self.repository.get(DeliveryDestination, subscription["destination_id"])
        envelope, raw = self.aggregator.freeze(subscription, source_run_id)
        strategy = next(row for row in self.service.strategies() if row["strategy_id"] == subscription["strategy_id"])
        envelope["registered_strategy_version"] = strategy["version"]
        if envelope["strategy_version"] != strategy["version"]:
            envelope["blockers"].append("strategy_version_unverified")
        input_hash = content_hash(envelope)
        llm = self.explanations.explain(envelope, input_hash, use_llm, subscription["llm_mode"])
        rendered = render_draft(envelope, llm)
        document = {"envelope": envelope, "blockers": envelope["blockers"], "llm": llm, "rendered_content": rendered}
        immutable_hash = content_hash(document)
        with self.repository.atomic() as session:
            # Saving never sends; source rows/config edits after this point cannot mutate content.
            row = ResearchPublication(id=uuid.uuid4().hex, subscription_id=subscription_id, destination_id=destination["id"],
                subscription_version=subscription["version"], destination_version=destination["version"], source_run_id=source_run_id,
                content_hash=immutable_hash, input_hash=input_hash, source_snapshot_json=raw, document_json=canonical(document), status="blocked")
            session.add(row)
            session.flush()
            return self._publication_public(row)

    @staticmethod
    def _publication_public(row):
        document = json.loads(row.document_json)
        document.pop('revision_binding', None)  # Internal original source/scope; never expose via API.
        return {key: getattr(row, key) for key in ("id", "subscription_id", "destination_id", "subscription_version", "destination_version", "source_run_id", "content_hash", "input_hash", "status", "created_at")} | document

    def publications(self):
        with self.repository.session_factory() as session:
            return [self._publication_public(row) for row in session.scalars(select(ResearchPublication).order_by(ResearchPublication.created_at.desc()).limit(100))]

    def publication(self, record_id):
        with self.repository.session_factory() as session:
            row = session.get(ResearchPublication, record_id)
            if row is None:
                raise ManagementError("not_found", "Publication not found", 404)
            return self._publication_public(row)

    def approve(self, record_id, expected_hash):
        publication = self.publication(record_id)
        if publication["content_hash"] != expected_hash:
            raise ManagementError("content_conflict", "Publication hash does not match", 409)
        if publication["blockers"]:
            raise ManagementError("publication_blocked", "Historical draft cannot be approved: " + ", ".join(publication["blockers"]), 409)
        # No supported W1 source producer exists here. Never trust stored/browser flags.
        raise ManagementError("formal_publication_unavailable", "Server-verified W1 publication is not implemented", 409)

    def send(self, record_id, expected_hash):
        publication = self.publication(record_id)
        if publication["content_hash"] != expected_hash:
            raise ManagementError("content_conflict", "Publication hash does not match", 409)
        raise ManagementError("publication_blocked", "Formal report delivery remains blocked pending server W1 verification and approval", 409)

    def reserve_test(self, destination_id, version, confirm, request_id):
        if confirm is not True or type(version) is not int or version < 1 or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
            raise ManagementError("invalid_request", "Explicit confirmation, target version and request_id required")
        with self.repository.atomic() as session:
            self.repository.recover_outbox(session)
            existing = session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.request_id == request_id))
            if existing is not None:
                if existing.destination_id != destination_id or existing.destination_version != version or existing.kind != "test":
                    raise ManagementError("idempotency_conflict", "request_id belongs to a different immutable delivery", 409)
                return self.repository.delivery_public(existing)
            target = session.get(DeliveryDestination, destination_id)
            if target is None:
                raise ManagementError("not_found", "Destination not found", 404)
            if target.version != version:
                raise ManagementError("version_conflict", "Destination changed; refresh before testing", 409)
            if not target.enabled or not target.credential_ref:
                raise ManagementError("test_target_unavailable", "Testing requires an enabled, credential-configured target", 409)
            # Fixed text is identical to capabilities; custom financial/keyword content forbidden.
            if target.keyword and target.keyword not in TEST_MESSAGE:
                raise ManagementError("test_keyword_mismatch", "Fixed test message does not contain this target keyword", 409)
            payload = {"msg_type": "text", "content": {"text": TEST_MESSAGE}}
            row = DeliveryOutbox(id=uuid.uuid4().hex, request_id=request_id, kind="test", destination_id=destination_id,
                destination_version=version, credential_ref=target.credential_ref, payload_json=canonical(payload), payload_hash=content_hash(payload),
                status="pending", expires_at=utc_naive_now() + timedelta(minutes=10), attempt_count=0)
            session.add(row)
            session.flush()
            return self.repository.delivery_public(row)

    def test(self, destination_id, version, confirm, request_id):
        reserved = self.reserve_test(destination_id, version, confirm, request_id)
        with self.repository.atomic() as session:
            self.repository.recover_outbox(session)
            row = session.get(DeliveryOutbox, reserved["id"])
            if row.status != "pending":
                return self.repository.delivery_public(row)
            target = session.get(DeliveryDestination, row.destination_id)
            if target is None or not target.enabled or target.version != row.destination_version or target.credential_ref != row.credential_ref:
                row.status, row.error_code, row.completed_at = "suppressed", "configuration_changed", utc_naive_now()
                return self.repository.delivery_public(row)
            payload = json.loads(row.payload_json)
            if content_hash(payload) != row.payload_hash:
                row.status, row.error_code, row.completed_at = "suppressed", "payload_changed", utc_naive_now()
                return self.repository.delivery_public(row)
            try:
                credentials = self.service.vault.resolve(row.credential_ref)
            except CredentialError:
                row.status, row.error_code, row.completed_at = "suppressed", "credential_unavailable", utc_naive_now()
                return self.repository.delivery_public(row)
            row.status, row.sending_at, row.attempt_count = "sending", utc_naive_now(), 1
            # Durable commit by atomic context exit must succeed before invoking sender.
        try:
            outcome = self.sender(credentials, payload)
            if not isinstance(outcome, DeliveryOutcome) or outcome.status not in ("sent", "rejected", "delivery_unknown"):
                outcome = DeliveryOutcome("delivery_unknown", "invalid_transport_outcome")
        except Exception:
            outcome = DeliveryOutcome("delivery_unknown", "transport_exception")
        with self.repository.atomic() as session:
            row = session.get(DeliveryOutbox, reserved["id"])
            # Lease recovery wins; a late worker cannot turn recovered unknown into sent.
            if row.status == "sending":
                row.status, row.error_code, row.completed_at = outcome.status, outcome.error_code, utc_naive_now()
            return self.repository.delivery_public(row)
