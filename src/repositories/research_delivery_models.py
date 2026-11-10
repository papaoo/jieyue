"""Additive research delivery tables; no engine, configuration or startup I/O."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from src.storage import Base, utc_naive_now


class DeliveryDestination(Base):
    __tablename__ = "research_delivery_destinations"
    id = Column(String(32), primary_key=True)
    alias = Column(String(100), nullable=False)
    kind = Column(String(20), nullable=False)
    enabled = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    credential_ref = Column(String(32), nullable=True)
    credential_fingerprint = Column(String(64), nullable=True, unique=True)
    signing_configured = Column(Boolean, nullable=False, default=False)
    keyword = Column(String(100), nullable=False, default="阶跃")
    created_at = Column(DateTime, nullable=False, default=utc_naive_now)
    updated_at = Column(DateTime, nullable=False, default=utc_naive_now)


class DeliverySubscription(Base):
    __tablename__ = "research_delivery_subscriptions"
    id = Column(String(32), primary_key=True)
    name = Column(String(100), nullable=False)
    destination_id = Column(String(32), ForeignKey("research_delivery_destinations.id"), nullable=False)
    strategy_id = Column(String(100), nullable=False)
    top_n = Column(Integer, nullable=False)
    llm_mode = Column(String(20), nullable=False)
    planned_time = Column(String(5), nullable=False)
    enabled = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=utc_naive_now)
    updated_at = Column(DateTime, nullable=False, default=utc_naive_now)


class DeliverySetupReceipt(Base):
    __tablename__ = "research_delivery_setup_receipts"
    request_id = Column(String(100), primary_key=True)
    payload_hash = Column(String(64), nullable=False)
    destination_id = Column(String(32), ForeignKey("research_delivery_destinations.id"), nullable=False)
    subscription_id = Column(String(32), ForeignKey("research_delivery_subscriptions.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_naive_now)


class ResearchPublication(Base):
    __tablename__ = "research_delivery_publications"
    id = Column(String(32), primary_key=True)
    subscription_id = Column(String(32), nullable=False)
    destination_id = Column(String(32), nullable=False)
    subscription_version = Column(Integer, nullable=False)
    destination_version = Column(Integer, nullable=False)
    source_run_id = Column(String(64), nullable=False)
    content_hash = Column(String(64), nullable=False)
    input_hash = Column(String(64), nullable=False)
    source_snapshot_json = Column(Text, nullable=False)
    document_json = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="blocked")
    created_at = Column(DateTime, nullable=False, default=utc_naive_now)


class ResearchLLMAttempt(Base):
    __tablename__ = "research_delivery_llm_attempts"
    id = Column(String(32), primary_key=True)
    input_key = Column(String(64), nullable=False, unique=True)
    business_day = Column(String(10), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="requesting")
    result_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_naive_now)


class DeliveryOutbox(Base):
    __tablename__ = "research_delivery_outbox"
    id = Column(String(32), primary_key=True)
    request_id = Column(String(100), nullable=False, unique=True)
    kind = Column(String(20), nullable=False)
    destination_id = Column(String(32), nullable=False)
    destination_version = Column(Integer, nullable=False)
    credential_ref = Column(String(32), nullable=False)
    publication_id = Column(String(32), nullable=True)
    payload_json = Column(Text, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    error_code = Column(String(64), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime, nullable=False)
    sending_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_naive_now)
