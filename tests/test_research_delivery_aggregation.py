import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import Config
from src.storage import Base, ScreeningRun, IntelligenceItem, IntelligenceSource
from src.repositories.research_delivery_repository import ResearchDeliveryRepository, ManagementError
from src.services.research_delivery.credentials import CredentialVault
from src.services.research_delivery.subscriptions import ResearchDeliveryService
from src.services.research_delivery.publisher import ResearchPublisher


@pytest.fixture
def research(tmp_path, monkeypatch):
    import src.config
    import requests
    monkeypatch.setattr(src.config, "setup_env", lambda *a, **k: pytest.fail("root env forbidden"))
    monkeypatch.setattr(requests.Session, "request", lambda *a, **k: pytest.fail("network forbidden"))
    engine = create_engine("sqlite:///" + (tmp_path / "research.db").as_posix(), connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine, tables=[ScreeningRun.__table__, IntelligenceSource.__table__, IntelligenceItem.__table__])
    service = ResearchDeliveryService(ResearchDeliveryRepository(factory), CredentialVault(tmp_path / "vault"))
    dest = service.create_destination({"alias": "private-target", "kind": "test"})
    sub = service.create_subscription({"name": "research", "destination_id": dest["id"], "strategy_id": service.strategies()[0]["strategy_id"], "llm_mode": "off"})
    publisher = ResearchPublisher(service, config=Config())
    yield publisher, sub, dest
    engine.dispose()


def saved_run(publisher, sub, **overrides):
    result = {"strategy": sub["strategy_id"], "strategy_version": "1", "market": "cn", "ranking_mode": "llm", "llm_ranked": True,
              "snapshot_count": 50, "after_filter_count": 5, "warnings": ["source warning"], "source_errors": {"bad": "failed"},
              "candidates": [{"code": "600001", "name": "one", "rank": 2, "score": 0, "price": 10, "why_selected": [{"code": "rule", "text": "saved reason", "source": "rule", "quality": "observed"}],
                              "raw": {"portfolio_flags": ["concentrated"], "deep_analysis_risk_flags": ["deep risk"], "daily_quality_flags": ["stale"], "excluded_by_risk": False, "private_holdings": "never prompt"}},
                             {"code": "600002", "name": "two", "rank": 1, "score": 5, "price": 20}]}
    result.update(overrides)
    with publisher.repository.session_factory() as session, session.begin():
        session.add(ScreeningRun(run_id="run-one", strategy=sub["strategy_id"], market="cn", result_json=json.dumps(result)))
    return result


def test_freezes_real_order_risk_dates_hash_and_blockers(research):
    publisher, sub, dest = research
    saved_run(publisher, sub)
    first = publisher.preview(sub["id"], "run-one")
    assert first["status"] == "blocked"
    assert first["envelope"]["business_date"] is None
    assert first["envelope"]["ranking_mode"] == "llm"
    picks = first["envelope"]["candidates"]
    assert [p["code"] for p in picks] == ["600001", "600002"]
    assert picks[0]["metrics"]["score"] == 0 and picks[0]["rank"] == 2
    assert picks[0]["why_selected"][0]["text"] == "saved reason"
    assert all(term in first["rendered_content"] for term in ("concentrated", "deep risk", "stale", "source warning"))
    assert "w1_unverified" in first["blockers"]
    second = publisher.preview(sub["id"], "run-one")
    assert first["content_hash"] == second["content_hash"]
    with publisher.repository.session_factory() as session, session.begin():
        session.query(ScreeningRun).filter_by(run_id="run-one").one().result_json = "{}"
    assert publisher.publication(first["id"])["content_hash"] == first["content_hash"]
    with pytest.raises(ManagementError) as error:
        publisher.approve(first["id"], first["content_hash"])
    assert error.value.code == "publication_blocked"


def test_stored_evidence_exact_issuer_dates_dedup_and_bounded(research):
    publisher, sub, _ = research
    saved_run(publisher, sub)
    now = datetime.utcnow()
    with publisher.repository.session_factory() as session, session.begin():
        for i, (code, date, summary) in enumerate([("600001", now, "same"), ("600001", now, "same"), ("600001", now, "conflict"), ("600001", None, "undated"), ("600001", now-timedelta(days=100), "old"), ("600999", now, "unrelated")]):
            session.add(IntelligenceItem(source_name="public", title="title", summary=summary, url="https://example.com/evidence", source_type="rss", scope_type="symbol", scope_value=code, market="cn", published_at=date, fetched_at=now))
    evidence = publisher.preview(sub["id"], "run-one")["envelope"]["evidence"]
    assert len(evidence) == 3
    assert {e["summary"] for e in evidence} == {"same", "conflict", "undated"}
    assert all(e["issuer_code"] == "600001" for e in evidence)
    assert next(e for e in evidence if e["summary"] == "undated")["date_status"] == "unknown"


def test_mismatch_unknown_empty_and_bad_metrics(research):
    publisher, sub, _ = research
    saved_run(publisher, sub, candidates=[])
    report = publisher.preview(sub["id"], "run-one")
    assert "empty_coverage_unverified" in report["blockers"]
    with publisher.repository.session_factory() as session, session.begin():
        session.query(ScreeningRun).one().strategy = "different"
    with pytest.raises(ManagementError) as error:
        publisher.preview(sub["id"], "run-one")
    assert error.value.code == "source_mismatch"


def test_missing_legacy_metadata_readable_and_malformed_values_are_gaps(research):
    publisher, sub, _ = research
    saved_run(publisher, sub, strategy=None, market=None, strategy_version=None,
              candidates=[{"code": "600001", "price": True, "score": float("nan"), "raw": {"price": 10, "portfolio_flags": ["risk"]}, "dsa_context": {"warnings": {"error": "bad"}, "news": {"results": [{"title": "saved context", "published_date": None, "url": "https://example.com/news"}]}}}])
    report = publisher.preview(sub["id"], "run-one")
    assert report["status"] == "blocked" and "source_metadata_missing" in report["blockers"]
    candidate = report["envelope"]["candidates"][0]
    assert candidate["metrics"]["price"] is None and candidate["metrics"]["score"] is None
    assert "conflicting_metric:price" in candidate["missing"]
    assert report["envelope"]["evidence"][0]["issuer_status"] == "unverified_candidate_context"
