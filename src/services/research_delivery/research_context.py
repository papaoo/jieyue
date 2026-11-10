"""Bounded optional shortlist facts; never selection, scoring or publication authority."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
import multiprocessing
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pandas as pd

from src.services.research_delivery.evidence import business_date as parse_date, canonical_code, finite_number
from src.services.research_delivery.evidence_store import EvidenceStore
from src.services.research_delivery.live_contracts import ProducerResult, json_safe
from src.services.research_delivery.market_context import empty_market_context


SHANGHAI = ZoneInfo('Asia/Shanghai')
TOTAL_SECONDS, FUNDAMENTAL_SECONDS, DAILY_LIMIT = 90., 8., 100
# Search's success join + terminate/kill joins can consume three grace seconds.
CLEANUP_SECONDS = 3.
FIELDS = {
    'valuation': ('as_of', 'pe_ratio', 'pb_ratio', 'total_mv', 'circ_mv', 'dividend_yield_ttm_pct', 'currency', 'price_basis'),
    'growth': ('report_date', 'announcement_date', 'revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin'),
    'boards': ('industry',),
    'financial_report': ('report_date', 'announcement_date', 'revenue', 'net_profit_parent', 'operating_cash_flow', 'roe', 'currency'),
    'stock_flow': ('as_of', 'main_net_inflow', 'inflow_5d', 'inflow_10d', 'provider_net_inflow', 'unit', 'definition'),
    'dragon_tiger': ('checked_trade_date', 'is_on_list', 'recent_count', 'latest_date', 'lookback_sessions'),
    'institution': ('report_date', 'announcement_date', 'previous_report_date', 'coverage', 'full_institution_coverage'),
    'dividend': ('as_of', 'as_of_events', 'ttm_dividend_yield_pct', 'ttm_cash_dividend_per_share',
        'implemented_event_count_365d', 'events_complete', 'latest_implemented_ex_date', 'cash_unit', 'coverage'),
}


class _GuardInterrupted(BaseException):
    """Only progress cancellation uses this, bypassing provider Exception fallbacks."""
    def __init__(self, error):
        self.error = error


def _announcement_worker(connection, code, target):
    try:
        from src.announcement_source import fetch_company_announcements
        connection.send(fetch_company_announcements(code, days=3, limit=10, as_of=target))
    except Exception:
        connection.send(None)  # No provider exception text crosses the boundary.
    finally:
        connection.close()


def _bounded_announcements(code, *, as_of, timeout_seconds, days=3, limit=10):
    """One reaped process; caller reserves cleanup time separately from its poll."""
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_announcement_worker, args=(child, code, as_of), daemon=True)
    deadline = time.monotonic() + timeout_seconds
    started = False
    try:
        process.start()
        started = True
        child.close()
        if not parent.poll(max(0., deadline - time.monotonic())):
            raise TimeoutError('announcement_deadline')
        return parent.recv()
    finally:
        parent.close()
        child.close()
        if started:
            process.join(1.)
            if process.is_alive():
                process.terminate()
                process.join(1.)
            if process.is_alive():
                process.kill()
                process.join(1.)
            if not process.is_alive():
                process.close()


def _stamp(value, target, now):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.utcoffset() is not None and stamp.astimezone(SHANGHAI).date() == target and stamp <= now
    except (TypeError, ValueError):
        return False


def _public_url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
    except (TypeError, ValueError):
        return False


def _dated_data(data, domain, code, target):
    if not isinstance(data, dict) or data.get('issuer_code') != code:
        return {}
    if domain in {'growth', 'financial_report', 'institution'}:
        period, disclosed = parse_date(data.get('report_date')), parse_date(data.get('announcement_date'))
        if period is None or disclosed is None or not period <= disclosed <= target:
            return {}
    elif domain in {'valuation', 'stock_flow'}:
        if parse_date(data.get('as_of')) != target:
            return {}
    elif domain == 'dragon_tiger' and parse_date(data.get('checked_trade_date')) != target:
        return {}
    elif domain == 'dividend':
        if parse_date(data.get('as_of')) != target:
            data = {k: v for k, v in data.items() if k not in {'ttm_dividend_yield_pct', 'as_of'}}
        if parse_date(data.get('as_of_events')) != target:
            data = {k: v for k, v in data.items() if k in {'issuer_code', 'as_of', 'ttm_dividend_yield_pct'}}
    selected = {}
    for field in FIELDS[domain]:
        value = data.get(field)
        if value is None:
            continue
        if field.endswith('date') or field in {'as_of', 'as_of_events'}:
            parsed = parse_date(value)
            if parsed and parsed <= target:
                selected[field] = parsed.isoformat()
        elif isinstance(value, bool):
            if field in {'is_on_list', 'full_institution_coverage', 'events_complete'}:
                selected[field] = value
        elif isinstance(value, str):
            if field in {'industry', 'currency', 'price_basis', 'unit', 'definition', 'coverage', 'cash_unit'} and len(value) <= 300:
                selected[field] = value
        else:
            try:
                selected[field] = finite_number(value)
            except (ValueError, TypeError, OverflowError):
                pass
    return selected


class ResearchContextEnricher:
    def __init__(self, config, *, fundamental_fetcher=None, news_fetcher=None,
                 announcement_fetcher=None, clock=None, evidence_store=None, evidence_dir=None, monotonic=None):
        self.config = config
        self.fundamental_fetcher, self.news_fetcher = fundamental_fetcher, news_fetcher
        self.announcement_fetcher = announcement_fetcher
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.monotonic = monotonic or time.monotonic
        self.store = evidence_store or (EvidenceStore(evidence_dir) if evidence_dir is not None else None)
        self._background, self._requested, self._market = {}, {}, {}
        self._query = self._search = None

    def configure(self, config):
        """Refresh explicit credentials/settings without losing same-day facts."""
        if config is not self.config:
            self._query = self._search = None
        self.config = config

    def _now(self, target):
        now = self.clock()
        if not isinstance(now, datetime) or now.utcoffset() is None or now.astimezone(SHANGHAI).date() != target:
            raise ValueError('research_context_clock_invalid')
        return now

    @staticmethod
    def _progress(progress, code=None):
        if progress:
            try:
                progress(dict(stage='research_context', code=code))
            except Exception as error:
                raise _GuardInterrupted(error) from None

    @staticmethod
    def _completed(value, day):
        data = value.to_dict() if isinstance(value, ProducerResult) else value
        if (isinstance(data, dict) and data.get('business_date') == day
                and data.get('state') in {'ready', 'degraded'} and not data.get('blockers')
                and isinstance(data.get('result'), dict) and isinstance(data['result'].get('picks'), list)):
            return data
        return None

    def _rehydrate(self, related, target):
        if self.store is None:
            return
        directory = self.store.directory.resolve()
        day = target.isoformat()
        for batch in related:
            for entry in batch.get('evidence', []):
                if (not isinstance(entry, dict) or entry.get('dataset') != 'candidate_background'
                        or entry.get('business_date') != day or entry.get('source') != 'research_context'):
                    continue
                try:
                    path = Path(entry['path']).resolve(strict=True)
                    if not path.is_relative_to(directory) or path.stat().st_size > 2_000_000:
                        continue
                    raw = path.read_bytes()
                    if sha256(raw).hexdigest() != entry.get('sha256'):
                        continue
                    capture = json.loads(raw)
                    if any(capture.get(k) != entry.get(k) for k in ('source', 'dataset', 'business_date', 'fetched_at', 'provenance')):
                        continue
                    if not _stamp(capture.get('fetched_at'), target, self._now(target)):
                        continue
                    payload = capture['payload']
                    code, background = payload['issuer_code'], payload['background']
                    if not canonical_code(code) or background.get('issuer_code') != code or payload.get('version') != 'candidate-background-v1':
                        continue
                    # Never trust an attached context as a fresh external source.
                    key = (day, code)
                    if key not in self._background and len(self._requested.setdefault(day, set())) < DAILY_LIMIT:
                        self._requested[day].add(code)
                        self._background[key] = (deepcopy(background), dict(entry))
                except (KeyError, TypeError, ValueError, OSError):
                    continue

    def _fundamental(self, code, target, budget, deadline, progress, on_block):
        if self.fundamental_fetcher is not None:
            fetch = self.fundamental_fetcher if callable(self.fundamental_fetcher) else self.fundamental_fetcher.fetch
            return fetch(code, budget_seconds=budget, as_of=target.isoformat(), on_block=on_block)
        token = getattr(self.config, 'tushare_token', None)
        if not token:
            return None
        if self._query is None:
            from data_provider.tushare_fetcher import create_tushare_http_client
            self._query = create_tushare_http_client(token, timeout=5).query
        from data_provider.tushare_fundamental_adapter import TushareFundamentalAdapter
        symbol_deadline = min(deadline, self.monotonic() + budget)

        def query(api, **params):
            self._progress(progress, code)
            remaining = symbol_deadline - self.monotonic()
            if remaining <= .1:
                raise TimeoutError('fundamental_budget_exhausted')
            params['request_timeout'] = min(5., remaining, params.get('request_timeout', 5.))
            market_key = (target.isoformat(), api, json.dumps({k: v for k, v in params.items() if k != 'request_timeout'}, sort_keys=True))
            if not params.get('ts_code') and market_key in self._market:
                cached = self._market[market_key]
                if cached is None:
                    raise ValueError('shared_market_unavailable')
                return cached.copy(deep=True)
            try:
                frame = self._query(api, **params)
                if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
                    raise ValueError('fundamental_schema_invalid')
                if not params.get('ts_code'):
                    self._market[market_key] = frame.copy(deep=True)
                # Adapter selects exact issuer and disclosed dates itself; its
                # normalized blocks below are captured with this requested code.
                return frame
            except Exception:
                if not params.get('ts_code'):
                    self._market[market_key] = None
                raise

        def publish(name, value):
            # The existing adapter stamps system time. This explicit runtime
            # clock is the observation timestamp for this actual provider call.
            value['fetched_at'] = self._now(target).isoformat()
            on_block(name, value, trusted_issuer=True)

        return TushareFundamentalAdapter(query).fetch(code, budget_seconds=budget,
            as_of=target.isoformat(), on_block=publish)

    def _news(self, code, name, timeout):
        fetch = self.news_fetcher
        if fetch is None:
            keys = {field: getattr(self.config, field, []) for field in ('bocha_api_keys', 'tavily_api_keys',
                'anspire_api_keys', 'brave_api_keys', 'serpapi_keys', 'minimax_api_keys', 'searxng_base_urls')}
            # Do not enable public instances or load global config implicitly.
            if not any(keys.values()) and not getattr(self.config, 'searxng_public_instances_enabled', False):
                return None
            if self._search is None:
                from src.search_service import SearchService
                self._search = SearchService(**{k.replace('_api_keys', '_keys'): v for k, v in keys.items()},
                    searxng_public_instances_enabled=getattr(self.config, 'searxng_public_instances_enabled', False),
                    searxng_timeout_seconds=getattr(self.config, 'searxng_timeout_seconds', None),
                    news_max_age_days=getattr(self.config, 'news_max_age_days', 3),
                    news_strategy_profile=getattr(self.config, 'news_strategy_profile', 'short'))
            fetch = self._search.search_topic_news_bounded
        return fetch(name + ' ' + code, max_results=5, focus_keywords=[name, code], timeout_seconds=timeout)

    def _candidate(self, code, name, target, deadline, progress, background):
        def on_block(domain, value, trusted_issuer=False):
            if not isinstance(value, dict) or value.get('source') != 'tushare' or not _stamp(value.get('fetched_at'), target, self._now(target)):
                return
            data = value.get('data', {})
            if not isinstance(data, dict):
                return
            if domain == 'earnings':
                gaps = value.get('missing_fields', [])
                on_block('dividend', dict(value, data=data.get('dividend', {}), missing_fields=[field for field in gaps
                    if isinstance(field, str) and ('dividend' in field or 'event' in field)]), trusted_issuer)
                domain, data = 'financial_report', data.get('financial_report', {})
                value = dict(value, missing_fields=[field for field in gaps if field in FIELDS['financial_report']])
            elif domain == 'capital_flow':
                domain, data = 'stock_flow', data.get('stock_flow', {})
            if domain not in FIELDS or not isinstance(data, dict):
                return
            data = dict(data)
            if trusted_issuer:
                data['issuer_code'] = code
            accepted = _dated_data(data, domain, code, target)
            missing = [field for field in value.get('missing_fields', [])
                if isinstance(field, str) and re.fullmatch('[a-z_]{1,80}', field)]
            status = 'partial' if value.get('status') != 'ok' or missing else 'available'
            background[domain] = dict(status=status if accepted else 'unavailable', data=accepted,
                source='tushare', fetched_at=value['fetched_at'], missing_fields=missing)
            # Aggregate gaps describe the latest accepted domain state, not the
            # adapter's preliminary blocks. Other domains/control gaps survive.
            background['missing'] = [gap for gap in background['missing']
                if not gap.startswith(domain + ':') and gap != domain + '_stale_or_invalid']
            background['missing'].extend(domain + ':' + field for field in missing)
            if not accepted:
                background['missing'].append(domain + '_stale_or_invalid')
            # The request already completed. Retain its validated partial facts
            # in memory before cancellation can interrupt the next stage.
            self._progress(progress, code)

        remaining = deadline - self.monotonic()
        if remaining <= .1:
            background['status'] = 'budget_exhausted'
            background['statuses']['fundamental'] = 'not_requested'
            return background
        try:
            fundamental_deadline = self.monotonic() + min(FUNDAMENTAL_SECONDS, remaining)
            background['statuses']['fundamental'] = 'requested'
            blocks = self._fundamental(code, target, min(FUNDAMENTAL_SECONDS, remaining), deadline, progress, on_block)
            if blocks is None:
                background['statuses']['fundamental'] = 'not_configured'
            elif isinstance(blocks, dict):
                for domain, value in blocks.items():
                    on_block(domain, value, trusted_issuer=self.fundamental_fetcher is None)
                background['statuses']['fundamental'] = 'budget_exhausted' if self.monotonic() >= fundamental_deadline else 'completed'
            else:
                background['statuses']['fundamental'] = 'invalid_response'
        except Exception:
            background['statuses']['fundamental'] = 'budget_exhausted' if self.monotonic() >= fundamental_deadline else 'source_failed'
        for domain in ('news', 'announcements'):
            self._progress(progress, code)
            residual = deadline - self.monotonic() - CLEANUP_SECONDS
            if residual <= .1:
                background['statuses'][domain] = 'budget_exhausted'
                continue
            try:
                background['statuses'][domain] = 'requested'
                if domain == 'news':
                    response = self._news(code, name, min(12., residual))
                    if response is None:
                        background['statuses'][domain] = 'not_configured'
                        continue
                    if not getattr(response, 'success', False):
                        raise ValueError('news_unavailable')
                    for row in getattr(response, 'results', [])[:5]:
                        published = parse_date(getattr(row, 'published_date', None))
                        title = getattr(row, 'title', '')
                        # A company-related search hit is a lead, never issuer verification.
                        if (published is not None and target - timedelta(days=2) <= published <= target
                                and isinstance(title, str) and 0 < len(title) <= 500
                                and (re.search(r'(?<!\d)' + re.escape(code) + r'(?!\d)', title) or (name != code and name in title))
                                and _public_url(getattr(row, 'url', None))):
                            background['news'].append(dict(issuer_code=code, title=title, url=row.url,
                                published_date=published.isoformat(), evidence_kind='company_news_lead',
                                source='search', fetched_at=self._now(target).isoformat()))
                else:
                    fetch = self.announcement_fetcher or _bounded_announcements
                    kwargs = dict(days=3, limit=10, as_of=target)
                    if self.announcement_fetcher is None:
                        kwargs['timeout_seconds'] = min(12., residual)
                    response = fetch(code, **kwargs)
                    if (not isinstance(response, dict) or response.get('ok') is not True
                            or not _stamp(response.get('fetched_at'), target, self._now(target))):
                        raise ValueError('announcement_unavailable')
                    from src.services.research_delivery.auxiliary_producers import _ann_url
                    for row in response.get('records', [])[:10]:
                        if not isinstance(row, dict):
                            continue
                        published = parse_date(row.get('published_date'))
                        title = row.get('title')
                        if (row.get('issuer_code') == code and row.get('evidence_kind') == 'announcement_metadata'
                                and published is not None and target - timedelta(days=2) <= published <= target
                                and isinstance(title, str) and 0 < len(title) <= 500
                                and _ann_url(row.get('url'), row.get('source'), code)):
                            background['announcements'].append({k: row[k] for k in ('issuer_code', 'title', 'url',
                                'source', 'published_date', 'evidence_kind')} | {'fetched_at': response['fetched_at']})
                background['statuses'][domain] = 'completed'
            except Exception:
                background['statuses'][domain] = 'budget_exhausted' if self.monotonic() >= deadline else 'source_failed'
        background['missing'] = sorted(set(background['missing']))
        return background

    def enrich(self, result, *, related_results, top_n_by_strategy, progress=None):
        """Append evidence-backed backgrounds and same-day shortlist overlaps."""
        try:
            return self._enrich(result, related_results, top_n_by_strategy, progress)
        except _GuardInterrupted as interruption:
            raise interruption.error

    def restore(self, related_results, *, business_date, progress=None):
        """Recover same-day attempts only; never grant comparison eligibility."""
        target = parse_date(business_date)
        if target is None:
            raise ValueError('research_context_date_invalid')
        self._now(target)
        try:
            self._progress(progress)
            self._rehydrate([value for item in related_results
                if (value := self._completed(item, target.isoformat())) is not None], target)
            self._progress(progress)
        except _GuardInterrupted as interruption:
            raise interruption.error

    def _enrich(self, result, related_results, limits, progress):
        target = parse_date(result.business_date)
        if target is None or self._completed(result, result.business_date) is None:
            return result
        self._now(target)
        day = target.isoformat()
        self._progress(progress)
        deadline = self.monotonic() + TOTAL_SECONDS
        # Retain only current day: sharing is bounded even for a long-lived worker.
        self._background = {key: value for key, value in self._background.items() if key[0] == day}
        self._market = {key: value for key, value in self._market.items() if key[0] == day}
        self._requested = {day: self._requested.get(day, set())}
        known = [value for item in related_results if (value := self._completed(item, day)) is not None]
        self._rehydrate(known, target)
        completed = {value['category_id']: value for value in known}
        completed[result.category_id] = result.to_dict()
        selected, overlaps = {}, {}
        for category in sorted(completed):
            limit = limits.get(category, 0)
            if type(limit) is not int or limit < 1:
                continue
            selected[category] = [row for row in completed[category]['result']['picks'][:min(limit, 20)]
                if isinstance(row, dict) and canonical_code(row.get('code'))]
            for row in selected[category]:
                overlaps.setdefault(row['code'], []).append(category)
        payload = deepcopy(result.result)
        context = deepcopy(payload.get('research_context'))
        if not isinstance(context, dict) or context.get('business_date') != day:
            context = empty_market_context(target)
        context.update(background={}, cross_strategy={}, compared_strategies=sorted(selected))
        evidence = deepcopy(result.evidence)
        for row in selected.get(result.category_id, []):
            code = row['code']
            self._progress(progress, code)
            context['cross_strategy'][code] = sorted(set(overlaps.get(code, [])))
            key = (day, code)
            if key not in self._background:
                if len(self._requested[day]) >= DAILY_LIMIT:
                    context['background'][code] = dict(issuer_code=code, status='daily_limit', missing=['daily_limit'])
                    continue
                self._requested[day].add(code)
                name = row.get('name') if isinstance(row.get('name'), str) and row['name'].strip() else code
                background = dict(issuer_code=code, business_date=day, status='partial',
                    missing=[], news=[], announcements=[], statuses={})
                self._background[key] = (background, None)
                try:
                    self._candidate(code, name, target, deadline, progress, background)
                    self._progress(progress, code)
                except _GuardInterrupted:
                    # In-memory bookkeeping only: no provider or evidence I/O is
                    # permitted after the guard rejects the current authority.
                    background['status'] = 'interrupted'
                    background['missing'].append('enrichment_interrupted')
                    for domain in ('fundamental', 'news', 'announcements'):
                        status = background['statuses'].get(domain, 'not_requested')
                        background['statuses'][domain] = 'interrupted' if status == 'requested' else status
                    raise
            background, capture = self._background[key]
            if capture is None and background['status'] != 'evidence_capture_failed':
                # On authorized resume, freeze the retained attempt/partial
                # without re-running any provider stage for this same-day code.
                # Snapshot PE/PB remain known basic valuation independently of
                # provider risk-context incompleteness; original risk flags stay intact.
                valuation = {}
                for field in ('pe_ratio', 'pb_ratio'):
                    try:
                        if field in row:
                            valuation[field] = finite_number(row[field])
                    except (TypeError, ValueError, OverflowError):
                        pass
                if valuation:
                    background['valuation'] = dict(status='available', data=valuation | {'as_of': day},
                        source='screening_snapshot', evidence_ids=[e.get('sha256') for e in result.evidence if isinstance(e, dict) and e.get('sha256')])
                capture = None
                facts = dict(version='candidate-background-v1', issuer_code=code, background=background)
                stamp = self._now(target)
                if self.store is None:
                    # Pure callers receive normalized inline evidence. The live
                    # worker always supplies a disk store for immutable freezing.
                    capture = dict(source='research_context', dataset='candidate_background', payload=deepcopy(facts),
                        fetched_at=stamp.isoformat(), business_date=day, provenance='provider_normalized')
                    capture['sha256'] = sha256(json.dumps(json_safe(capture), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    capture['evidence_id'] = capture['sha256']
                else:
                    try:
                        capture = self.store.capture_payload('research_context', 'candidate_background', facts,
                            fetched_at=stamp, business_date=target, provenance='provider_normalized')
                    except Exception:
                        # A failed evidence write must not cause another strategy
                        # to repeat a provider request or use unsupported facts.
                        self._background[key] = (dict(issuer_code=code, business_date=day,
                            status='evidence_capture_failed', missing=['evidence_capture_failed'],
                            news=[], announcements=[], statuses={}), None)
                        raise
                self._background[key] = (background, capture)
            context['background'][code] = deepcopy(background)
            if capture is not None:
                context['background'][code]['evidence_id'] = capture['sha256']
                if capture not in evidence:
                    evidence.append(dict(capture))
        context.setdefault('statuses', {})['background'] = 'bounded_optional'
        context['statuses']['cross_strategy'] = 'known_completed_same_day_top_n_only'
        context['background_scope'] = dict(max_unique_codes=DAILY_LIMIT, requested_count=len(self._requested[day]),
            total_budget_seconds=TOTAL_SECONDS, fundamental_budget_seconds=FUNDAMENTAL_SECONDS)
        payload['research_context'] = context
        return replace(result, result=json_safe(payload), evidence=evidence)
