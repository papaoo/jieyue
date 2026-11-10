"""Fixed-day, entirely offline contracts for the trusted collector."""
from dataclasses import asdict
from datetime import date, datetime
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.services.research_delivery.live_collector import LiveCollector as ProductionCollector, CollectionError
from tests.screening_reference_fixtures import reference_bundle
from src.services.screening.config import Config
from src.services.screening.strategy import list_strategies
from tests.test_screening_compute_only import forbid_remote


T = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


def LiveCollector(*args, **kwargs):
    kwargs.setdefault('reference_bundle', reference_bundle())
    return ProductionCollector(*args, **kwargs)


@pytest.mark.parametrize('damage,expected', [('', 1), ('holiday', 0)])
def test_calendar_only_does_not_fetch_stocks(tmp_path, damage, expected):
    client = Client()
    client.damage = damage
    collector = LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW, request_interval=0)
    result = collector.trading_day(T)
    assert result['is_open'] == expected and result['business_date'] == T.isoformat()
    assert result['evidence']['endpoint'] == 'trade_cal'
    assert [c[0] for c in client.calls] == ['trade_cal']


@pytest.mark.parametrize('failure,code', [('invalid', 'calendar_evidence_invalid'),
    ('denied', 'source_permission_denied')])
def test_calendar_only_unknown_is_not_closed(tmp_path, failure, code):
    class BadClient:
        def query(self, *a, **kw):
            if failure == 'denied': raise RuntimeError('permission denied PRIVATE_SENTINEL')
            return pd.DataFrame([dict(exchange='SSE', cal_date='20261014', is_open=1)])
    collector = LiveCollector(SimpleNamespace(), tmp_path, client=BadClient(), now=NOW, request_interval=0)
    with pytest.raises(CollectionError) as error: collector.trading_day(T)
    assert str(error.value) == code


def test_calendar_only_window_and_date_guard(tmp_path):
    client = Client()
    collector = LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW.replace(hour=14), request_interval=0)
    with pytest.raises(CollectionError): collector.trading_day(T)
    collector.now = NOW
    with pytest.raises(CollectionError): collector.trading_day(date(2026, 10, 14))
    assert not client.calls


class Client:
    def __init__(self, count=2):
        self.codes = [f"{600001 + i:06d}.SH" for i in range(count)]
        self.calls = []
        self.damage = ""

    def query(self, endpoint, fields="", request_timeout=None, **params):
        self.calls.append((endpoint, params, request_timeout))
        if self.damage == "denied":
            raise RuntimeError("token=do-not-record-this")
        if endpoint == "trade_cal":
            if params.get('start_date') != params.get('end_date'):
                return pd.DataFrame([dict(exchange=params['exchange'], cal_date=d.strftime('%Y%m%d'),
                    is_open=int(d.weekday() < 5)) for d in pd.date_range(params['start_date'], params['end_date'])])
            return pd.DataFrame([dict(exchange="SSE", cal_date="20261015", is_open=0 if self.damage == "holiday" else 1)])
        if endpoint == "stock_basic":
            return pd.DataFrame([dict(ts_code=c, symbol=c[:6], name="示例", list_date="20000101") for c in self.codes])
        if endpoint == 'suspend_d':
            return pd.DataFrame(columns=['ts_code', 'trade_date', 'suspend_timing', 'suspend_type'])
        if endpoint == "daily_basic":
            rows = [dict(ts_code=c, trade_date="20261015", turnover_rate=2., volume_ratio=1.2,
                         pe_ttm=None if self.damage == "missing_pe" and c == self.codes[0] else 8.,
                         pb=1., total_mv=1e6, circ_mv=8e5) for c in self.codes]
            if self.damage == "partial":
                rows = rows[:1]
            if self.damage == "stale":
                rows[0]["trade_date"] = "20261014"
            return pd.DataFrame(rows)
        if endpoint == "daily" and "ts_code" not in params:
            rows = [dict(ts_code=c, trade_date="20261015", close=10., pct_chg=1., amount=1e6) for c in self.codes]
            if self.damage == "duplicate":
                rows.append(rows[0])
            return pd.DataFrame(rows)
        dates = (pd.bdate_range(end=T, periods=60) if self.damage == 'short' else
                 pd.bdate_range(params['start_date'], params['end_date']))
        if endpoint == "adj_factor":
            rows = [dict(ts_code=params["ts_code"], trade_date=d.strftime("%Y%m%d"), adj_factor=1. if i < 30 else 2.) for i, d in enumerate(dates)]
            if self.damage == "factor_conflict":
                rows.append(dict(rows[0], adj_factor=3.))
            return pd.DataFrame(rows)
        close = [18. if i < 30 else 10. for i in range(len(dates))]
        return pd.DataFrame([dict(ts_code=params["ts_code"], trade_date=d.strftime("%Y%m%d"),
                                  open=c, high=c + .1, low=c - .1, close=c, vol=10000.)
                             for d, c in zip(dates, close)])


def collect(tmp_path, client=None, strategies=("dual_low",), **kwargs):
    return LiveCollector(SimpleNamespace(tushare_token="never-persist-token"), tmp_path,
                         client=client or Client(), now=kwargs.pop("now", NOW), request_interval=0).collect(strategies, T, **kwargs)


class HistoricalGapClient(Client):
    def __init__(self, timing=None, explained=True):
        super().__init__()
        self.timing, self.explained = timing, explained

    def query(self, endpoint, **kwargs):
        table = super().query(endpoint, **kwargs)
        if endpoint == 'daily' and 'ts_code' in kwargs:
            return table.loc[table.trade_date != '20260901'].copy()
        if endpoint == 'suspend_d' and 'ts_code' in kwargs and self.explained:
            return pd.DataFrame([dict(ts_code=kwargs['ts_code'], trade_date='20260901',
                                      suspend_type='S', suspend_timing=self.timing)])
        return table


def test_explained_history_gap_resumes_and_passes_actual_publisher(tmp_path):
    from copy import deepcopy
    from src.services.research_delivery.formal_publisher import FormalPublisher
    client = HistoricalGapClient()
    first = LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW,
                          request_interval=0, batch_size=1).collect(['dual_low'], T)['dual_low']
    assert first.coverage['pending_count'] == 1
    resumed = LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW,
                            request_interval=0, batch_size=1).collect(['dual_low'], T)['dual_low']
    assert resumed.state == 'degraded' and not resumed.blockers
    source = asdict(resumed)
    FormalPublisher._coverage(source)
    FormalPublisher._evidence(source, NOW)
    assert len([c for c in client.calls if c[0] == 'daily' and 'ts_code' in c[1]]) == 2
    # Read actual immutable checkpoint records; frame attrs must retain every parent.
    records = [json.loads(p.read_text(encoding='utf-8')) for p in tmp_path.rglob('*.json')]
    checkpoints = [r['payload'] for r in records if r.get('dataset') == 'checkpoint']
    complete = next(r for r in checkpoints if len(r['histories']) == 2)
    refs = {r['sha256']: r for r in resumed.evidence}
    for code, history in complete['histories'].items():
        parents = history['attrs']['history_evidence']
        assert set(parents) == {'daily', 'adj_factor', 'suspend_d'}
        assert history['attrs']['evidence_id'] == parents['adj_factor'] == resumed.coverage['daily_evidence'][code]
        for endpoint, identifier in parents.items():
            assert refs[identifier]['endpoint'] == endpoint
            assert refs[identifier]['params']['ts_code'][:6] == code
    for wrong in (complete['histories']['600001']['attrs']['history_evidence']['suspend_d'],
                  resumed.coverage['daily_evidence']['600002'], '0' * 64):
        damaged = deepcopy(source)
        damaged['coverage']['daily_evidence']['600001'] = wrong
        with pytest.raises(Exception, match='coverage_invalid'):
            FormalPublisher._coverage(damaged)


@pytest.mark.parametrize('explained,timing', [(False, None), (True, '09:30-10:30')])
def test_unexplained_or_partial_history_gap_cannot_publish(tmp_path, explained, timing):
    from src.services.research_delivery.formal_publisher import FormalPublisher
    result = collect(tmp_path, HistoricalGapClient(timing, explained))['dual_low']
    assert result.state == 'blocked'
    assert {f['reason'] for f in result.coverage['history_failures']} == {'unexplained_historical_bar_gap'}
    with pytest.raises(Exception, match='coverage_invalid'):
        FormalPublisher._coverage(asdict(result))


@pytest.mark.parametrize('damage', ['', 'wrong_date', 'wrong_identity', 'full_day_conflict'])
def test_formal_collector_mixed_resumption_intraday_events(tmp_path, damage):
    from src.services.research_delivery.formal_publisher import FormalPublisher
    class MixedEvents(Client):
        def query(self, endpoint, **params):
            table = super().query(endpoint, **params)
            if endpoint == 'suspend_d':
                identity = dict(ts_code='invalid' if damage == 'wrong_identity' else self.codes[0],
                                trade_date='20261014' if damage == 'wrong_date' else '20261015')
                return pd.DataFrame([dict(identity, suspend_type='R', suspend_timing=None),
                    dict(identity, suspend_type='S', suspend_timing=None if damage == 'full_day_conflict'
                         else '09:30-09:41,09:44-09:54')])
            return table
    result = collect(tmp_path, MixedEvents())['dual_low']
    if damage:
        assert result.state == 'blocked' and not result.result
    else:
        assert result.state == 'degraded' and not result.blockers
        FormalPublisher._coverage(asdict(result))
        FormalPublisher._evidence(asdict(result), NOW)


def test_all_ten_real_strategies_shared_histories_and_safe_evidence(tmp_path, monkeypatch):
    forbid_remote(monkeypatch)
    ids = [s.name for s in list_strategies(Config().strategies_dir)]
    client = Client()
    progress = []
    results = collect(tmp_path, client, ids, progress=progress.append)
    assert len(results) == 10
    assert all(r.state == "degraded" and not r.blockers and r.result["strategy"] == k for k, r in results.items())
    assert all(r.strategy_version and r.coverage["independent_universe_count"] == 2 for r in results.values())
    assert len([c for c in client.calls if c[0] == "daily" and "ts_code" in c[1]]) == 2
    assert len([c for c in client.calls if c[0] == "adj_factor"]) == 2
    assert all(c[2] == 15 for c in client.calls)
    for endpoint, params, _ in client.calls:
        if "ts_code" in params:
            assert params["end_date"] == "20261015" and params["start_date"] == "20251219"
    assert progress
    for result in results.values():
        json.dumps(asdict(result), allow_nan=False)
        assert result.coverage["history_basis"] == "bounded_observed_bars_not_exchange_contiguous"
        assert any(e["endpoint"] == "stock_basic" and e["fetched_at"].startswith("2026-10-15") for e in result.evidence)
        for entry in result.evidence:
            assert sha256(Path(entry["path"]).read_text(encoding="utf-8").encode("utf-8")).hexdigest() == entry["sha256"]
    raw = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json"))
    assert "never-persist-token" not in raw


@pytest.mark.parametrize("damage,code", [("denied", "source_request_failed"), ("partial", "market_response_coverage_below_95pct"),
                                        ("stale", "market_trade_date_invalid"), ("duplicate", "market_identity_invalid")])
def test_bad_market_input_blocks_without_secret(tmp_path, damage, code):
    client = Client()
    client.damage = damage
    result = collect(tmp_path, client)["dual_low"]
    assert result.state == "blocked" and code in result.blockers
    assert not result.result and "do-not-record-this" not in repr(result)


def test_missing_metric_is_audited_not_zero_or_whole_market(tmp_path):
    client = Client()
    client.damage = "missing_pe"
    result = collect(tmp_path, client)["dual_low"]
    assert result.state == "degraded"
    assert result.coverage["eligible_count"] == 1
    assert result.coverage["excluded"] == [{"code": "600001", "reasons": ["ineligible_pe_not_reported"]}]
    assert result.coverage["scope"] == "restricted_complete_core_metric_eligible_universe"
    assert all(p["code"] != "600001" for p in result.result["picks"])


def test_shared_context_precedes_filter_and_does_not_change_picks(tmp_path, monkeypatch):
    from src.services.research_delivery import live_collector as module
    original = module.preview_strategy
    computed = []
    def preview(*args, **kwargs):
        result = original(*args, **kwargs)
        if result.result:
            computed.append(asdict(result.result))
        return result
    monkeypatch.setattr(module, "preview_strategy", preview)
    class NamedClient(Client):
        def query(self, endpoint, fields="", **params):
            table = super().query(endpoint, fields=fields, **params)
            if endpoint == "stock_basic":
                assert "industry" in fields.split(",")
                table["industry"] = "银行"
            return table
    client = NamedClient()
    client.damage = "missing_pe"
    result = collect(tmp_path, client)["dual_low"].result
    assert result["picks"] == computed[-1]["picks"]
    context = result["research_context"]
    assert context["version"] == "research-context-v1"
    assert context["industries"] == {}
    assert context['market_breadth']['observed_count'] == 2
    assert 'canonical_industry_unassessed' in context['missing']
    assert set(context["instruments"]) == {p["code"] for p in result["picks"]}


@pytest.mark.parametrize("missing", [None, pd.NA])
def test_object_amount_missing_keeps_other_candidate_eligible(tmp_path, monkeypatch, missing):
    from src.services.research_delivery import live_collector as module
    original = module.preview_strategy
    expected_picks = []
    def preview(*args, **kwargs):
        result = original(*args, **kwargs)
        if result.result:
            expected_picks.append(asdict(result.result)["picks"])
        return result
    monkeypatch.setattr(module, "preview_strategy", preview)
    class MissingAmountClient(Client):
        def query(self, endpoint, fields="", **params):
            frame = super().query(endpoint, fields=fields, **params)
            if endpoint == "stock_basic":
                frame["industry"] = "银行"
            if endpoint == "daily" and "ts_code" not in params:
                frame["amount"] = frame["amount"].astype(object)
                frame.loc[0, "amount"] = missing
                assert frame.amount.dtype == object
            return frame
    result = collect(tmp_path, MissingAmountClient())["dual_low"]
    assert result.blockers == ['required_market_or_reference_evidence_invalid']
    assert not result.result and not expected_picks


@pytest.mark.parametrize("damage", ["factor_conflict", "short"])
def test_bad_candidate_history_blocks_all_strict_strategies(tmp_path, damage):
    client = Client()
    client.damage = damage
    results = collect(tmp_path, client, ("dual_low", "shrink_pullback"))
    assert results["dual_low"].state == "blocked"
    assert results["shrink_pullback"].state == "blocked"
    assert results["shrink_pullback"].coverage["history_failures"]


def test_reference_names_do_not_fabricate_trade_date(tmp_path, monkeypatch):
    from src.services.research_delivery import live_collector as module
    real = module.preview_strategy
    seen = []
    def inspect(*args, **kwargs):
        snapshot = args[1]
        assert "name" not in snapshot.attrs["field_trade_date_columns"]
        assert snapshot.attrs["reference_field_metadata"]["name"]["effective_as_of"] == T.isoformat()
        seen.append(True)
        return real(*args, **kwargs)
    monkeypatch.setattr(module, "preview_strategy", inspect)
    assert collect(tmp_path)["dual_low"].state == "degraded"
    assert seen


@pytest.mark.parametrize("now", [datetime(2026, 10, 14, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
                                  datetime(2026, 10, 15, 14, tzinfo=ZoneInfo("Asia/Shanghai"))])
def test_noncurrent_or_preclose_day_never_fetches(tmp_path, now):
    client = Client()
    assert collect(tmp_path, client, now=now)["dual_low"].state == "blocked"
    assert not client.calls


def test_holiday_skips_market_calls(tmp_path):
    client = Client()
    client.damage = "holiday"
    assert collect(tmp_path, client)["dual_low"].state == "skipped"
    assert len(client.calls) == 1


def test_adjustment_and_volume_units_reach_real_preview(tmp_path, monkeypatch):
    from src.services.research_delivery import live_collector as module
    real = module.preview_strategy
    seen = []
    def inspect(*args, **kwargs):
        for code, history in kwargs.get("daily_histories", {}).items():
            assert history.iloc[0]["close"] == 9.  # 18 * 1 / factor(T=2)
            assert history.iloc[-1]["close"] == 10.
            assert history.iloc[0]["volume"] == 1e6  # 10,000 hands -> shares
            assert history.attrs["volume_unit"] == "shares"
            seen.append(code)
        return real(*args, **kwargs)
    monkeypatch.setattr(module, "preview_strategy", inspect)
    assert collect(tmp_path, strategies=("shrink_pullback",))["shrink_pullback"].state == "degraded"
    assert set(seen) == {"600001", "600002"}


def test_exact_95_percent_is_allowed_but_preserves_missing_denominator(tmp_path):
    class OneMissing(Client):
        def query(self, endpoint, **kwargs):
            table = super().query(endpoint, **kwargs)
            return table.iloc[1:].copy() if endpoint == "daily_basic" else table
    result = collect(tmp_path, OneMissing(20))["dual_low"]
    assert result.state == "blocked"
    assert result.coverage["independent_universe_count"] == 20
    assert result.coverage["daily_basic_response_coverage"] == .95
    assert result.blockers == ['required_market_or_reference_evidence_invalid']


def test_technical_budget_blocks_incomplete_full_scope(tmp_path, monkeypatch):
    forbid_remote(monkeypatch)
    results = collect(tmp_path, Client(101), ("dual_low", "shrink_pullback"))
    assert results["dual_low"].result == {}
    technical = results["shrink_pullback"]
    assert technical.state == "blocked"
    assert technical.coverage["technical_planned_count"] == 101
    assert technical.coverage["pending_count"] == 1
    assert technical.coverage["technical_not_evaluated_count"] == 0


@pytest.mark.parametrize("damage", ["name", "price", "factor_missing", "history_future", "history_stale"])
def test_invalid_cells_are_not_silently_fabricated(tmp_path, damage):
    class Damaged(Client):
        def query(self, endpoint, **kwargs):
            table = super().query(endpoint, **kwargs)
            params = kwargs
            if damage == "name" and endpoint == "stock_basic":
                table["name"] = ""
            if damage == "price" and endpoint == "daily" and "ts_code" not in params:
                table["close"] = True
            if damage == "factor_missing" and endpoint == "adj_factor":
                table = table.iloc[1:].copy()
            if endpoint == "daily" and "ts_code" in params:
                if damage == "history_future":
                    table.loc[0, "trade_date"] = "20261016"
                if damage == "history_stale":
                    table = table.iloc[:-1].copy()
            return table
    result = collect(tmp_path, Damaged(), ("shrink_pullback",))["shrink_pullback"]
    assert result.state == "blocked" and not result.result


def test_old_tushare_diagnostic_still_does_not_accept_undated_names():
    from src.services.research_delivery.tushare_snapshot import assess_tushare_snapshot
    client = Client()
    result = assess_tushare_snapshot(client.query("daily"), client.query("daily_basic"), client.query("stock_basic"),
                                    trade_date=T, universe=("600001", "600002"),
                                    calendar_verified=True, universe_verified=True)
    assert result.state == "blocked"
    assert any("name:wrong_or_unknown_business_date" in code for code in result.blockers)


@pytest.mark.parametrize("value", [True, 0, 21, 2.5])
def test_preview_output_override_rejects_invalid_values(value):
    from tests.test_research_delivery_preview import verified_snapshot
    from src.services.research_delivery.screening_preview import preview_strategy
    with pytest.raises(ValueError, match="max_output"):
        preview_strategy("dual_low", verified_snapshot(), trade_date=T, universe=("600001", "600002"),
                         calendar_verified=True, universe_verified=True, max_output=value)


@pytest.mark.parametrize("error,code", [(TimeoutError("secret"), "source_timeout"),
    (RuntimeError("抱歉，您没有接口权限 token=secret"), "source_permission_denied"),
    (RuntimeError("daily quota exhausted secret"), "source_permission_denied"),
    (RuntimeError("每分钟最多访问500次 secret"), "source_rate_limited")])
def test_provider_error_classification_is_safe(tmp_path, error, code):
    class Failed(Client):
        def query(self, *args, **kwargs):
            raise error
    result = collect(tmp_path, Failed())["dual_low"]
    assert result.blockers == [code]
    assert "secret" not in repr(result)


def test_progress_callback_lease_failure_propagates_without_retry(tmp_path):
    class LeaseLost(Exception):
        pass
    def progress(event):
        if event["stage"] == "collected":
            raise LeaseLost("fenced")
    client = Client()
    with pytest.raises(LeaseLost):
        collect(tmp_path, client, progress=progress)
    assert len(client.calls) == 1


def test_config_token_client_has_bounded_timeout_and_no_environment_mutation(tmp_path, monkeypatch):
    import os
    from data_provider import tushare_fetcher
    client = Client()
    before = dict(os.environ)
    seen = []
    def factory(token, *, timeout):
        assert token == "private-fixture"
        seen.append(timeout)
        return client
    monkeypatch.setattr(tushare_fetcher, "create_tushare_http_client", factory)
    result = LiveCollector(SimpleNamespace(tushare_token="private-fixture"), tmp_path,
                           now=NOW, request_interval=0).collect(["dual_low"], T)["dual_low"]
    assert result.state == "degraded" and seen == [15]
    assert dict(os.environ) == before
    assert "private-fixture" not in repr(result)


def test_endpoint_spacing_is_enforced_without_real_wait(tmp_path, monkeypatch):
    from src.services.research_delivery import live_collector as module
    sleeps = []
    monkeypatch.setattr(module.time, "monotonic", lambda: 10.)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    client = Client()
    result = LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW).collect(["dual_low"], T)
    assert result["dual_low"].state == "degraded"
    assert sleeps == [1.] * (len(client.calls) - 1)


@pytest.mark.parametrize("error,reason,limit", [
    (RuntimeError("permission denied private"), "source_permission_denied", 1),
    (RuntimeError("rate limit private"), "source_rate_limited", 1),
    (TimeoutError("private"), "source_timeout", 5),
    (RuntimeError("private"), "source_request_failed", 5),
])
def test_history_provider_circuit_breaker_preserves_all_planned_failures(tmp_path, error, reason, limit):
    class FailedHistory(Client):
        def query(self, endpoint, **kwargs):
            if "ts_code" in kwargs:
                self.calls.append((endpoint, kwargs, kwargs.get("request_timeout")))
                raise error
            return super().query(endpoint, **kwargs)
    client = FailedHistory(8)
    result = collect(tmp_path, client, ("shrink_pullback",))["shrink_pullback"]
    assert result.state == "blocked"
    assert reason in result.blockers
    assert len([c for c in client.calls if "ts_code" in c[1]]) == limit
    assert len(result.coverage["history_failures"]) == 8
    assert {failure["reason"] for failure in result.coverage["history_failures"]} == {reason}
    assert all(any(f"daily:{code[:6]}:" in blocker for blocker in result.blockers) for code in client.codes)
