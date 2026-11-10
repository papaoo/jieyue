"""Additive automation records. All persisted timestamps are UTC-naive."""
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint

from src.storage import Base, utc_naive_now


class ResearchBatch(Base):
    __tablename__ = 'research_automation_batches'
    id = Column(String(32), primary_key=True)
    business_date = Column(String(10), nullable=False)
    category_id = Column(String(100), nullable=False)
    strategy_version = Column(String(100), nullable=False)
    input_hash = Column(String(64), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_naive_now)
    expires_at = Column(DateTime, nullable=False)


class AutomationControl(Base):
    __tablename__ = 'research_automation_control'
    id = Column(String(10), primary_key=True, default='main')
    enabled = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    owner_id = Column(String(100), nullable=True)
    lease_token = Column(String(64), nullable=True)
    lease_until = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    last_error = Column(String(100), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utc_naive_now)


class AutomationJob(Base):
    __tablename__ = 'research_automation_jobs'
    __table_args__ = (UniqueConstraint('business_date', 'subscription_id', name='uq_research_automation_daily_job'),)
    id = Column(String(32), primary_key=True)
    business_date = Column(String(10), nullable=False)
    subscription_id = Column(String(32), nullable=False)
    subscription_version = Column(Integer, nullable=False)
    destination_version = Column(Integer, nullable=False)
    authorization_version = Column(Integer, nullable=False)
    category_id = Column(String(100), nullable=False)
    status = Column(String(32), nullable=False, default='pending')
    stage = Column(String(32), nullable=False, default='queued')
    attempts = Column(Integer, nullable=False, default=0)
    retry_at = Column(DateTime, nullable=True)
    force_requested = Column(Boolean, nullable=False, default=False)
    batch_id = Column(String(32), nullable=True)
    publication_id = Column(String(32), nullable=True)
    delivery_id = Column(String(32), nullable=True)
    error_code = Column(String(100), nullable=True)
    lease_token = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_naive_now)
    updated_at = Column(DateTime, nullable=False, default=utc_naive_now)


class AutomationRunRequest(Base):
    __tablename__ = 'research_automation_run_requests'
    request_id = Column(String(100), primary_key=True)
    business_date = Column(String(10), nullable=False)
    authorization_version = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_naive_now)


class AutomationProgress(Base):
    """Safe collection counters, never a publishable ResearchBatch."""
    __tablename__ = 'research_automation_progress'
    job_id = Column(String(32), primary_key=True)
    coverage_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=utc_naive_now)


class PublicationRelease(Base):
    __tablename__ = 'research_automation_releases'
    publication_id = Column(String(32), primary_key=True)
    batch_id = Column(String(32), nullable=False)
    authorization_version = Column(Integer, nullable=False)
    credential_ref = Column(String(32), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_naive_now)
