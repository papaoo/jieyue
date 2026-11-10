"""Server-side, current-day Tushare inputs for the unchanged frozen strategies."""
from dataclasses import asdict
from contextlib import nullcontext
from datetime import datetime, timedelta
import re
import time
from zoneinfo import ZoneInfo
from pathlib import Path
import json
from hashlib import sha256

import pandas as pd
from requests.exceptions import Timeout

from src.services.research_delivery.evidence import CORE_FIELDS, business_date as parse_date, finite_number, validate_history, validate_snapshot
from src.services.research_delivery.evidence_store import EvidenceStore
from src.services.research_delivery.collection_checkpoint import (
    CollectionCheckpoint, DEPENDENCY_VERSION, digest, pack_frame, unpack_frame, strategy_catalog_hash,
)
from src.services.research_delivery.timing import (
    PUBLICATION_WINDOW_END_TIME,
    PUBLICATION_WINDOW_START_TIME,
)
from src.services.research_delivery.live_contracts import ProducerResult, json_safe
from src.services.research_delivery.market_context import build_market_context
from src.services.research_delivery.screening_preview import preview_strategy
from src.services.screening.config import Config
from src.services.screening.daily import strict_history_start, prepare_dated_history
from src.services.screening.eligibility import (scoring_eligibility, suspension_state, validate_history_sessions,
    ELIGIBILITY_VERSION, suspension_proof_candidate, verified_suspension_proof)
from src.services.screening.snapshot import _prepare_tushare_snapshot
from src.services.screening.strategy import list_strategies
from data_provider.historical_reference import ReferenceBundle, name_state
from data_provider.historical_fundamentals import missing, supplement_pb, fetch_pb_range, fetch_suspension_status


SHANGHAI = ZoneInfo("Asia/Shanghai")
FIELDS = {
    "trade_cal": "exchange,cal_date,is_open",
    "stock_basic": "ts_code,symbol,name,industry,list_date",
    "daily": "ts_code,trade_date,close,pct_chg,amount",
    "daily_basic": "ts_code,trade_date,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv,dv_ttm",
    "history": "ts_code,trade_date,open,high,low,close,vol",
    "adj_factor": "ts_code,trade_date,adj_factor",
    "suspend_d": "ts_code,trade_date,suspend_timing,suspend_type",
}


class CollectionError(Exception):
    """Only fixed safe error codes can cross the producer boundary."""


class _ProgressInterrupted(Exception):
    def __init__(self, error):
        self.error = error


def _notify(progress, payload):
    if progress:
        try:
            progress(payload)
        except Exception as exc:
            raise _ProgressInterrupted(exc) from None


def _source_error(error):
    if isinstance(error, (TimeoutError, Timeout)):
        return "source_timeout"
    # Inspect in memory only; original provider text is never persisted or returned.
    message = str(error).lower()
    if re.search(r"每分钟|频率|rate.?limit|too many|\b429\b", message):
        return "source_rate_limited"
    if re.search(r"权限|无权|积分|quota|permission|denied|unauthori|forbidden|\b40[13]\b", message):
        return "source_permission_denied"
    return "source_request_failed"


class LiveCollector:
    def __init__(self, config, evidence_dir, *, client=None, now=None, request_interval=1.0, batch_size=100, reference_bundle=None,
                 pb_fetcher=fetch_pb_range, suspension_fetcher=fetch_suspension_status):
        self.config, self.client, self.now = config, client, now
        self.store = EvidenceStore(evidence_dir)
        self.request_interval = max(0., finite_number(request_interval))
        self._last_request = None
        self._evidence = []
        self._exchange_codes = {}
        self._research_context = {}
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError('batch_size_invalid')
        self.batch_size = batch_size
        self.checkpoint_guard = nullcontext
        self.reference_bundle = reference_bundle
        self.pb_fetcher = pb_fetcher
        self.suspension_fetcher = suspension_fetcher
        self._suspension_requests = 0

    def _suspension(self, code, requested, target, events, quotes, parents, progress):
        try:
            return suspension_state(events)
        except ValueError:
            if not suspension_proof_candidate(events, quotes, code=code, stamp=requested):
                raise
        identity = dict(ts_code=code, requested_date=requested.isoformat(), primary=parents['suspend_d'],
                        daily=parents['daily'])
        captured = None
        for entry in self._evidence:
            if entry.get('source') != 'baostock' or entry.get('dataset') != 'suspension_status':
                continue
            raw = Path(entry['path']).read_bytes()
            if sha256(raw).hexdigest() != entry['sha256']:
                raise CollectionError('suspension_proof_hash_invalid')
            payload = json.loads(raw)['payload']
            if all(payload.get(k) == v for k, v in identity.items()):
                records, captured = payload['records'], entry
                break
        if captured is None:
            if self._suspension_requests >= self.batch_size:
                raise CollectionError('suspension_proof_request_budget_exhausted')
            if self._clock().date() != target:
                raise CollectionError('collector_business_date_changed')
            self._suspension_requests += 1
            _notify(progress, dict(stage='collecting', endpoint='baostock_suspension', code=code[:6]))
            if self._last_request is not None:
                time.sleep(max(0., self.request_interval - (time.monotonic() - self._last_request)))
            try:
                records = self.suspension_fetcher(code, requested)
            except Exception as exc:
                raise CollectionError(_source_error(exc)) from None
            finally:
                self._last_request = time.monotonic()
            if self._clock().date() != target:
                raise CollectionError('collector_business_date_changed')
            # Capture even rejected records; only verified captures join a checkpoint.
            captured = self.store.capture_payload('baostock', 'suspension_status', dict(identity, records=records),
                fetched_at=self._clock(), business_date=target, provenance='provider_normalized')
        state = verified_suspension_proof(events, quotes, records, code=code, stamp=requested)
        if captured not in self._evidence:
            self._evidence.append(captured)
        state['independent_proof'] = dict(identity, evidence_id=captured['sha256'])
        return state

    def _clock(self):
        value = self.now() if callable(self.now) else self.now
        value = value if value is not None else datetime.now(SHANGHAI)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise CollectionError("collector_clock_invalid")
        return value.astimezone(SHANGHAI)

    def _client(self):
        if self.client is None:
            token = getattr(self.config, "tushare_token", "")
            if not isinstance(token, str) or not token.strip():
                raise CollectionError("tushare_credentials_missing")
            from data_provider.tushare_fetcher import create_tushare_http_client
            self.client = create_tushare_http_client(token, timeout=15)

    def trading_day(self, business_date, progress=None):
        """Current-window calendar-only evidence, including auxiliary-only runs.

        Unknown/denied/invalid calendars raise fixed CollectionError codes; only
        actual exchange is_open=0 is a holiday. No stock endpoints are queried.
        """
        target = parse_date(business_date)
        now = self._clock()
        if target is None or target != now.date():
            raise CollectionError("collector_current_business_day_only")
        if not PUBLICATION_WINDOW_START_TIME <= now.time() < PUBLICATION_WINDOW_END_TIME:
            raise CollectionError("collector_outside_collection_window")
        self._client()
        try:
            calendar = self._calendar(target, progress)
        except _ProgressInterrupted as exc:
            raise exc.error
        return dict(calendar, evidence=dict(self._evidence[-1]))

    def _request(self, endpoint, target, progress, *, history=False, **params):
        if self._clock().date() != target:
            raise CollectionError("collector_business_date_changed")
        _notify(progress, {"stage": "collecting", "endpoint": endpoint, "code": params.get("ts_code", "")[:6]})
        if self._last_request is not None:
            delay = self.request_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
        try:
            table = self.client.query(endpoint, fields=FIELDS["history" if history else endpoint],
                                      request_timeout=15, **params)
        except Exception as exc:
            raise CollectionError(_source_error(exc)) from None
        finally:
            self._last_request = time.monotonic()
        if not isinstance(table, pd.DataFrame) or not table.columns.is_unique:
            raise CollectionError("source_schema_invalid")
        allowed = set(FIELDS["history" if history else endpoint].split(","))
        if not set(table.columns) <= allowed:
            raise CollectionError("source_schema_invalid")
        fetched = self._clock()
        if fetched.date() != target:
            raise CollectionError("collector_business_date_changed")
        try:
            self._evidence.append(self.store.capture(endpoint, params, table, fetched_at=fetched, business_date=target))
        except Exception:
            raise CollectionError("evidence_capture_failed") from None
        _notify(progress, {"stage": "collected", "endpoint": endpoint, "code": params.get("ts_code", "")[:6]})
        return table.copy(deep=True)

    @staticmethod
    def _identities(table, *, dates=None, single_code=None, allow_repeated_codes=False):
        if "ts_code" not in table:
            raise CollectionError("market_identity_invalid")
        values = table["ts_code"].tolist()
        if any(not isinstance(c, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", c) for c in values):
            raise CollectionError("market_identity_invalid")
        if single_code is not None:
            if any(c != single_code for c in values):
                raise CollectionError("history_identity_invalid")
        elif not allow_repeated_codes and len({c[:6] for c in values}) != len(values):
            raise CollectionError("market_identity_invalid")
        if dates is not None:
            if "trade_date" not in table or any(parse_date(v) != dates for v in table["trade_date"]):
                raise CollectionError("market_trade_date_invalid")
        return set(values)

    def _calendar(self, target, progress):
        stamp = target.strftime("%Y%m%d")
        calendar = self._request("trade_cal", target, progress, exchange="SSE", start_date=stamp, end_date=stamp)
        if (len(calendar) != 1 or not {"cal_date", "is_open", "exchange"} <= set(calendar)
                or parse_date(calendar.iloc[0]["cal_date"]) != target or calendar.iloc[0]["exchange"] != "SSE"
                or calendar.iloc[0]["is_open"] not in (0, 1, "0", "1")):
            raise CollectionError("calendar_evidence_invalid")
        return {"exchange": "SSE", "business_date": target.isoformat(), "is_open": int(calendar.iloc[0]["is_open"])}

    def _market(self, target, progress, coverage):
        stamp = target.strftime("%Y%m%d")
        coverage["calendar"] = self._calendar(target, progress)
        if not coverage["calendar"]["is_open"]:
            return None
        reference = self._request("stock_basic", target, progress, exchange="", list_status="L")
        universe = self._identities(reference)
        if not universe or len(reference) >= 6000:
            raise CollectionError("universe_empty_or_truncated")
        if "list_date" not in reference or any(parse_date(v) is None or parse_date(v) > target for v in reference["list_date"]):
            raise CollectionError("universe_listing_date_invalid")
        coverage.update(independent_universe_count=len(universe), independent_universe_codes=sorted(c[:6] for c in universe),
                        universe_source="tushare.stock_basic:list_status=L:observed_current_day")
        self._exchange_codes = {code[:6]: code for code in universe}
        ref_evidence = self._evidence[-1]
        eligibility = {}
        references = self.reference_bundle
        derived = self.store.capture_payload('CAPCO', 'reference_derivation',
            dict(bundle_hash=references.hash, derived_as_of=target.isoformat(),
                 raw_source_vintages=getattr(references, 'source_vintages', {})),
            fetched_at=self._clock(), business_date=target, provenance='derived')
        self._evidence.append(derived)
        coverage['reference_bundle_hash'] = references.hash
        for suffix, exchange in (('SH', 'SSE'), ('SZ', 'SZSE'), ('BJ', 'BSE')):
            members = reference.loc[reference.ts_code.str.endswith('.' + suffix)]
            if members.empty:
                continue
            start = target - timedelta(days=150)
            verified_calendar, calendar_evidence = references.calendar(exchange, start, target)
            if exchange == 'BSE':
                calendar = pd.DataFrame([dict(cal_date=day.isoformat(), is_open=value, exchange=exchange)
                                         for day, value in verified_calendar.items()])
            else:
                calendar = self._request('trade_cal', target, progress, exchange=exchange,
                    start_date=start.strftime('%Y%m%d'), end_date=stamp)
            if not {'cal_date', 'is_open', 'exchange'} <= set(calendar):
                raise CollectionError('listing_calendar_invalid')
            dates = [parse_date(value) for value in calendar.cal_date]
            if (any(day is None or not start <= day <= target for day in dates) or len(set(dates)) != len(dates)
                    or len(dates) != (target - start).days + 1 or any(value != exchange for value in calendar.exchange)
                    or any(value not in (0, 1, '0', '1') for value in calendar.is_open)):
                raise CollectionError('listing_calendar_invalid')
            observed_calendar = dict(zip(dates, [int(value) for value in calendar.is_open]))
            if observed_calendar != verified_calendar:
                raise CollectionError('calendar_reference_conflict')
            for row in members.itertuples():
                try:
                    eligibility[row.ts_code[:6]] = references.eligibility(row.ts_code[:6], parse_date(row.list_date), target,
                        exchange, ref_evidence['sha256'], name_status=name_state([
                            dict(name=row.name, start_date=target.isoformat(), ann_date=target.isoformat())], target)['status'])
                except ValueError:
                    raise CollectionError('listing_eligibility_invalid') from None
        coverage['listing_eligibility'] = eligibility
        daily = self._request("daily", target, progress, trade_date=stamp)
        quote_ref = self._evidence[-1]
        basic = self._request("daily_basic", target, progress, trade_date=stamp)
        for label, table in (("daily", daily), ("daily_basic", basic)):
            codes = self._identities(table, dates=target)
            if not codes <= universe or len(table) >= 6000:
                raise CollectionError("market_universe_conflict_or_truncated")
            ratio = len(codes) / len(universe)
            coverage.update({f"{label}_response_count": len(codes), f"{label}_response_coverage": ratio,
                             f"{label}_missing_codes": sorted(c[:6] for c in universe - codes)})
        if min(coverage["daily_response_coverage"], coverage["daily_basic_response_coverage"]) < .95:
            raise CollectionError("market_response_coverage_below_95pct")
        # Derive facts from the observed market before strategy eligibility filters.
        self._research_context = build_market_context(reference, daily, business_date=target,
            fetched_at=quote_ref["fetched_at"], source="tushare", canonical_only=True)
        self._research_context["evidence"] = [dict(ref_evidence), dict(quote_ref)]
        for value in self._research_context["instruments"].values():
            value["evidence_id"] = ref_evidence["sha256"]
            value["observed_at"] = value["fetched_at"] = ref_evidence["fetched_at"]
        if self._research_context["industries"]:
            self._research_context["industries"]["evidence_ids"] = [ref_evidence["sha256"], quote_ref["sha256"]]
            self._research_context["industries"]["membership_observed_at"] = ref_evidence["fetched_at"]
        suspensions = self._request('suspend_d', target, progress, trade_date=stamp)
        # suspend_d contains events, so one security can have R and intraday S
        # rows on the same date. Keep identity/date validation for every row.
        self._identities(suspensions, dates=target, allow_repeated_codes=True)
        if not {'suspend_type', 'suspend_timing'} <= set(suspensions):
            raise CollectionError('suspension_schema_unknown')
        suspension_ref = self._evidence[-1]
        suspension_rows = suspensions.astype(object).where(suspensions.notna(), None).to_dict('records')
        field_sources = {}
        quote_records = daily.to_dict('records')
        quote_map = {r['ts_code']: r for r in quote_records}
        states = {}
        for provider in sorted(universe):
            try:
                states[provider] = self._suspension(provider, target, target,
                    [r for r in suspension_rows if r['ts_code'] == provider], quote_records,
                    dict(suspend_d=suspension_ref['sha256'], daily=quote_ref['sha256']), progress)
            except ValueError:
                raise CollectionError('required_market_or_reference_evidence_invalid') from None
        for index, row in basic.iterrows():
            code = row.ts_code[:6]
            if eligibility[code]['eligible'] and 'pb' in row and missing(row.pb) and not missing(row.get('pe_ttm')):
                if states[row.ts_code]['status'] == 'full_day':
                    continue
                if row.ts_code[-2:] not in {'SH', 'SZ'}:
                    continue
                try:
                    records = self.pb_fetcher(row.ts_code, target, target)
                    if len(records) != 1 or self._clock().date() != target:
                        raise ValueError('supplement_missing_or_date_changed')
                    captured = self.store.capture_payload('baostock', 'pb_supplement', records,
                        fetched_at=self._clock(), business_date=target, provenance='provider_normalized')
                    updated, metadata = supplement_pb(row.to_dict(), quote_map.get(row.ts_code), records[0], evidence_id=captured['sha256'])
                except Exception:
                    raise CollectionError('pb_supplement_unavailable_or_conflicting') from None
                self._evidence.append(captured)
                basic.at[index, 'pb'] = updated['pb']
                if metadata:
                    field_sources[code] = dict(pb_ratio=metadata)
        frame = _prepare_tushare_snapshot(daily, basic, reference, preserve_invalid_rows=True)
        frame = frame.set_index("code").reindex(sorted(c[:6] for c in universe)).reset_index()
        for field in ('industry', 'industry_code', 'industry_taxonomy', 'industry_as_of', 'industry_source', 'industry_evidence_id'):
            frame[field] = frame.code.map(lambda code: eligibility[code].get(field, ''))
        frame.attrs.update(snapshot_source="tushare", evidence_id=quote_ref["sha256"], field_source_overrides=field_sources,
                           reference_field_metadata={"name": {"source": "tushare.stock_basic", "evidence_id": ref_evidence["sha256"],
                           "observed_at": ref_evidence["fetched_at"], "effective_as_of": target.isoformat()}})
        quote_map = {r['ts_code']: r for r in quote_records}
        basic_map = {r['ts_code']: r for r in basic.to_dict('records')}
        scoring = {}
        for row in reference.to_dict('records'):
            code = row['ts_code'][:6]
            if not eligibility[code]['eligible']:
                scoring[code] = dict(eligibility[code], version=ELIGIBILITY_VERSION)
                continue
            try:
                state = name_state([dict(name=row['name'], start_date=target.isoformat(), ann_date=target.isoformat())], target)
                suspension = states[row['ts_code']]
                scoring[code] = scoring_eligibility(reference=eligibility[code], name_status=state['status'],
                    daily=quote_map.get(row['ts_code']), basic=basic_map.get(row['ts_code']),
                    suspension=suspension['status'], evidence_id=suspension_ref['sha256'])
                if 'independent_proof' in suspension:
                    scoring[code]['suspension_proof'] = suspension['independent_proof']
                    scoring[code]['evidence_id'] = digest(suspension['independent_proof'])
            except (ValueError, TypeError):
                raise CollectionError('required_market_or_reference_evidence_invalid') from None
        accepted, receipts, blockers = validate_snapshot(frame, CORE_FIELDS, tuple(frame["code"]), target)
        if blockers or accepted is None:
            raise CollectionError("snapshot_normalization_invalid")
        exclusions = {}
        for code, value in scoring.items():
            if not value['eligible']:
                exclusions[code] = [value['reason']]
        coverage['ipo_ineligible_count'] = sum(v['reason'] == 'ipo_fewer_than_61_sessions' for v in eligibility.values())
        coverage['scoring_eligibility'] = scoring
        coverage['eligibility_version'] = ELIGIBILITY_VERSION
        for receipt in receipts:
            if receipt.status != "valid" or receipt.as_of != target:
                if receipt.key.code not in exclusions:
                    raise CollectionError('required_market_or_reference_evidence_invalid')
        coverage.update(scope="restricted_complete_core_metric_eligible_universe",
                        excluded=[{"code": code, "reasons": sorted(set(reasons))} for code, reasons in sorted(exclusions.items())],
                        eligible_count=len(frame) - len(exclusions), unknown_missing_count=len((universe - set(daily.ts_code)) | (universe - set(basic.ts_code))))
        if not coverage["eligible_count"]:
            raise CollectionError("no_complete_metric_eligible_codes")
        return frame.loc[~frame.code.isin(exclusions)].copy()

    def _history(self, ts_code, target, price, progress):
        params = dict(ts_code=ts_code, start_date=strict_history_start(target).strftime("%Y%m%d"), end_date=target.strftime("%Y%m%d"))
        daily = self._request("daily", target, progress, history=True, **params)
        parents = {'daily': self._evidence[-1]['sha256']}
        factors = self._request("adj_factor", target, progress, **params)
        parents['adj_factor'] = self._evidence[-1]['sha256']
        for table in (daily, factors):
            self._identities(table, single_code=ts_code)
            if "trade_date" not in table:
                raise CollectionError("history_dates_invalid")
            dates = [parse_date(v) for v in table.trade_date]
            if any(v is None or v > target or v < strict_history_start(target) for v in dates) or len(set(dates)) != len(dates):
                raise CollectionError("history_dates_or_factor_conflict")
        if not {"open", "high", "low", "close", "vol"} <= set(daily) or "adj_factor" not in factors:
            raise CollectionError("history_schema_invalid")
        start = max(strict_history_start(target), self._listing_dates[ts_code[:6]])
        exchange = {'SH': 'SSE', 'SZ': 'SZSE', 'BJ': 'BSE'}[ts_code[-2:]]
        calendar, _ = self.reference_bundle.calendar(exchange, start, target)
        expected = {stamp for stamp, opened in calendar.items() if opened}
        observed = {parse_date(stamp) for stamp in daily.trade_date}
        suspended = []
        if expected - observed:
            table = self._request('suspend_d', target, progress, **params)
            parents['suspend_d'] = self._evidence[-1]['sha256']
            if not {'ts_code', 'trade_date', 'suspend_type', 'suspend_timing'} <= set(table) or len(table) >= 5000:
                raise CollectionError('history_suspension_schema_invalid')
            suspended = table.astype(object).where(table.notna(), None).to_dict('records')
            if any(r['ts_code'] != ts_code or parse_date(r['trade_date']) is None
                   or not start <= parse_date(r['trade_date']) <= target for r in suspended):
                raise CollectionError('history_suspension_identity_invalid')
        try:
            proofs = {}
            def resolve(stamp):
                state = self._suspension(ts_code, stamp, target,
                    [r for r in suspended if parse_date(r['trade_date']) == stamp], daily.to_dict('records'), parents, progress)
                if 'independent_proof' in state:
                    proofs[stamp.isoformat()] = state['independent_proof']
                return state
            validate_history_sessions(expected, observed,
                lambda stamp: [r for r in suspended if parse_date(r['trade_date']) == stamp], resolve=resolve)
        except ValueError:
            raise CollectionError('unexplained_historical_bar_gap') from None
        frame = prepare_dated_history(daily, factors, code=ts_code[:6], signal_date=target,
            source='tushare', evidence_id=parents['adj_factor'])
        # Keep the established publisher parent identity, independently of later
        # suspension requests. Checkpoint frame attrs retain all source parents.
        frame.attrs['history_evidence'] = parents
        frame.attrs['suspension_proofs'] = proofs
        return validate_history(frame, ts_code[:6], target, price)

    def collect(self, strategy_ids, business_date, progress=None):
        target = parse_date(business_date)
        if target is None or isinstance(strategy_ids, (str, bytes)):
            raise ValueError("collector_request_invalid")
        ids = list(dict.fromkeys(strategy_ids))
        if not ids:
            return {}
        runtime = Config()  # Deliberately not Config.from_env; never load credentials here.
        infos = {info.name: info for info in list_strategies(runtime.strategies_dir)}
        coverage = {"technical_limit": self.batch_size, "max_output": 20,
                    "history_basis": "bounded_observed_bars_not_exchange_contiguous"}
        self._evidence = []
        self._suspension_requests = 0
        self._research_context = {}
        request_hash = strategy_catalog_hash(runtime.strategies_dir, target)
        checkpoint = None
        saved = None

        def output(key, state, *, result=None, specific=None, warnings=(), blockers=()):
            return ProducerResult(key, target.isoformat(), state, infos[key].version if key in infos else "",
                                  json_safe(result or {}), json_safe(dict(coverage, **(specific or {}))),
                                  json_safe(list(self._evidence)), list(warnings), list(blockers))
        try:
            current = self._clock()
            if target != current.date():
                raise CollectionError("collector_current_business_day_only")
            if not PUBLICATION_WINDOW_START_TIME <= current.time() < PUBLICATION_WINDOW_END_TIME:
                raise CollectionError("collector_outside_collection_window")
            if self.reference_bundle is None:
                try:
                    self.reference_bundle = ReferenceBundle.load(Path(getattr(self.config, 'data_dir', runtime.data_dir)) / 'historical_references')
                except ValueError:
                    raise CollectionError('reference_bundle_missing_or_invalid') from None
            checkpoint_hash = digest(dict(catalog=request_hash, references=self.reference_bundle.hash))
            checkpoint = CollectionCheckpoint(self.store, target, checkpoint_hash, guard=self.checkpoint_guard)
            try:
                saved = checkpoint.load(current)
            except Exception:
                raise CollectionError('checkpoint_invalid') from None
            if saved:
                frame = unpack_frame(saved['snapshot'])
                coverage.update(saved['coverage'])
                coverage['technical_limit'] = self.batch_size
                self._exchange_codes = saved['exchange_codes']
                self._research_context = saved['research_context']
                self._evidence = saved['evidence']
            else:
                self._client()
                frame = self._market(target, progress, coverage)
            if frame is None:
                return {key: output(key, "skipped", warnings=("non_trading_day",)) for key in ids}
            if not saved:
                self._evidence.append(self.store.capture_payload('collection', 'frozen_snapshot', pack_frame(frame),
                    fetched_at=self._clock(), business_date=target, provenance='derived'))
        except Exception as exc:
            if isinstance(exc, _ProgressInterrupted):
                raise exc.error
            code = str(exc) if isinstance(exc, CollectionError) else "collection_failed"
            return {key: output(key, "blocked", blockers=(code,)) for key in ids}
        self._listing_dates = {code: parse_date(item['listing_date'])
                               for code, item in coverage['listing_eligibility'].items()}
        # First preview freezes the exact shared candidate order even though daily
        # readiness is not yet satisfied. It cannot publish or fetch externally.
        planned, results = {}, {}
        for key in ids:
            if key not in infos:
                results[key] = output(key, "blocked", blockers=("unknown_strategy",))
                continue
            try:
                planned[key] = preview_strategy(key, frame, trade_date=target, universe=tuple(frame.code),
                    calendar_verified=True, universe_verified=True, technical_limit=100, config=runtime, max_output=20)
            except Exception:
                results[key] = output(key, "blocked", blockers=("strategy_planning_failed",))
        union = sorted({code for preview in planned.values() if preview.scope for code in preview.scope.technical_planned})
        # Completed strategies leave the worker's pending set. Keep their frozen
        # vintage and shared history while the remaining strategies resume.
        if saved:
            union = sorted(set(union) | set(saved['pool']))
        pool_strategy_ids = sorted(set(planned) | set(saved.get('pool_strategy_ids', []) if saved else []))
        histories = {code: unpack_frame(value) for code, value in saved['histories'].items()} if saved else {}
        failures = dict(saved['failures']) if saved else {}
        def save_checkpoint():
            checkpoint.save(dict(snapshot=pack_frame(frame), coverage=coverage,
                exchange_codes=self._exchange_codes, research_context=self._research_context,
                evidence=self._evidence, pool=union, pool_strategy_ids=pool_strategy_ids,
                histories={c: pack_frame(h) for c, h in histories.items()},
                failures=failures, pending=[c for c in union if c not in histories and c not in failures]), self._clock())
        save_checkpoint()
        circuit_reason, consecutive_failures = None, 0
        remaining = [code for code in union if code not in histories]
        if remaining:
            self._client()
        for code in remaining[:self.batch_size]:
            if circuit_reason:
                failures[code] = circuit_reason
                continue
            try:
                histories[code] = self._history(self._exchange_codes[code], target, float(frame.loc[frame.code == code, "price"].iloc[0]), progress)
                failures.pop(code, None)
                consecutive_failures = 0
            except Exception as exc:
                if isinstance(exc, _ProgressInterrupted):
                    raise exc.error
                reason = str(exc) if isinstance(exc, CollectionError) else "history_validation_failed"
                failures[code] = reason
                if reason in {"source_permission_denied", "source_rate_limited"}:
                    circuit_reason = reason
                elif reason in {"source_timeout", "source_request_failed"}:
                    consecutive_failures += 1
                    if consecutive_failures >= 5:
                        circuit_reason = reason
                else:
                    consecutive_failures = 0
        save_checkpoint()
        for key in planned:
            try:
                _notify(progress, {"stage": "computing", "strategy_id": key})
                final = preview_strategy(key, frame, trade_date=target, universe=tuple(frame.code),
                    calendar_verified=True, universe_verified=True, daily_histories=histories, technical_limit=100, config=runtime, max_output=20)
                scope = final.scope
                specific = {"required_count": final.readiness.required_count, "valid_count": final.readiness.valid_count,
                            "snapshot_pass_count": len(scope.snapshot_pass) if scope else 0,
                            "technical_planned_count": len(scope.technical_planned) if scope else 0,
                            "technical_not_evaluated_count": len(scope.technical_not_evaluated) if scope else 0,
                            "history_failures": [{"code": code, "reason": failures[code]} for code in scope.technical_planned if code in failures] if scope else []}
                planned_codes = list(scope.technical_planned) if scope else []
                evaluated = [code for code in planned_codes if code in histories]
                failed = [code for code in planned_codes if code in failures]
                pending_codes = [code for code in planned_codes if code not in histories and code not in failures]
                scope_data = dict(business_date=target.isoformat(), strategy_id=key,
                    strategy_hash=request_hash, dependency_version=DEPENDENCY_VERSION,
                    snapshot_hash=digest(pack_frame(frame)), pool_hash=digest(union), codes=planned_codes,
                    pool_strategy_ids=pool_strategy_ids)
                specific.update(contract_version=2, evaluation_scope_count=len(planned_codes),
                    evaluated_count=len(evaluated), pending_count=len(pending_codes), failed_count=len(failed),
                    evaluation_scope=scope_data, scope_hash=digest(scope_data),
                    strategy_dependency_version=DEPENDENCY_VERSION,
                    evaluated_codes=evaluated, pending_codes=pending_codes, failed_codes=failed,
                    daily_evidence={code: histories[code].attrs['evidence_id'] for code in evaluated})
                warnings = [*final.readiness.warnings, "restricted_complete_core_metric_eligible_universe",
                            "300_day_history_exchange_sessions_verified"]
                source_blockers = sorted({item["reason"] for item in specific["history_failures"]
                                          if item["reason"].startswith("source_")})
                result = asdict(final.result) if final.result else {}
                if result:
                    context = dict(self._research_context)
                    codes = {pick["code"] for pick in result.get("picks", [])}
                    context["instruments"] = {code: value for code, value in context.get("instruments", {}).items() if code in codes}
                    result["research_context"] = context
                blockers = (*source_blockers, *final.readiness.blockers)
                if pending_codes:
                    blockers = ('collection_pending', *blockers)
                results[key] = output(key, final.readiness.state, result=result,
                                      specific=specific, warnings=warnings, blockers=blockers)
                _notify(progress, {"stage": "computed", "strategy_id": key})
            except Exception as exc:
                if isinstance(exc, _ProgressInterrupted):
                    raise exc.error
                results[key] = output(key, "blocked", blockers=("strategy_calculation_failed",))
        return {key: results[key] for key in ids}
