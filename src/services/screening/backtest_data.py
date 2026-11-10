"""Bounded SQLite historical batches and strict frozen screening inputs.

Raw batches retain source hashes/vintages; indexed rows avoid deserializing a
multi-year archive. Acquisition is explicit and separate from calculation.
"""
from collections.abc import Mapping
from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time
import re
import sys

import pandas as pd

from data_provider.historical_reference import ReferenceBundle, day, digest, name_state
from src.services.screening.daily import strict_history_start, prepare_dated_history
from src.services.screening.snapshot import _prepare_tushare_snapshot
from src.services.research_delivery.evidence import validate_history
from src.services.screening.eligibility import (scoring_eligibility, suspension_state, validate_history_sessions,
    suspension_proof_candidate, verified_suspension_proof)
from data_provider.historical_fundamentals import missing, supplement_pb, fetch_pb_range, fetch_suspension_status

EXCHANGES = {'SH': 'SSE', 'SZ': 'SZSE', 'BJ': 'BSE'}
FIELDS = {
    'stock_basic': 'ts_code,symbol,name,list_date,delist_date,list_status,exchange',
    'namechange': 'ts_code,name,start_date,end_date,ann_date,change_reason',
    'daily': 'ts_code,trade_date,open,high,low,close,pct_chg,amount,vol',
    'daily_basic': 'ts_code,trade_date,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv,dv_ttm',
    'adj_factor': 'ts_code,trade_date,adj_factor',
    'stk_limit': 'ts_code,trade_date,up_limit,down_limit',
    'suspend_d': 'ts_code,trade_date,suspend_timing,suspend_type',
    'index_daily': 'ts_code,trade_date,open,close',
}


class DataUnavailable(ValueError):
    pass


def _standard_identity(code):
    return isinstance(code, str) and re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', code) is not None


def _fixed_history_start():
    from src.services.screening.backtest import FIXED
    return strict_history_start(min(day(value) for value in FIXED['start_dates']))


def _out_of_period_legacy_listing(row):
    """Recognize only dated T-prefixed SSE delisted master records, not aliases."""
    code = row.get('ts_code')
    if (not isinstance(code, str) or not re.fullmatch(r'T[0-9]{6}\.SH', code)
            or row.get('symbol') != code[:-3] or row.get('exchange') != 'SSE'
            or row.get('list_status') != 'D' or not isinstance(row.get('name'), str)
            or not row['name'].strip()):
        return False
    if any(not isinstance(row.get(key), str) or not re.fullmatch(r'[0-9]{8}', row[key])
           for key in ('list_date', 'delist_date')):
        return False
    try:
        return day(row['list_date']) <= day(row['delist_date']) <= _fixed_history_start()
    except ValueError:
        return False


class BatchStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self._artifact_changes = 0
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS batches (key TEXT PRIMARY KEY, endpoint TEXT, stamp TEXT,
              sha TEXT NOT NULL, raw TEXT NOT NULL, fetched_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rows (batch_key TEXT, endpoint TEXT, code TEXT, stamp TEXT, raw TEXT, row_sha TEXT);
            CREATE INDEX IF NOT EXISTS row_lookup ON rows(endpoint, code, stamp);
            CREATE INDEX IF NOT EXISTS row_batch_lookup ON rows(batch_key);
            CREATE TABLE IF NOT EXISTS artifacts (key TEXT PRIMARY KEY, sha TEXT, raw TEXT);
            CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, key TEXT, sha TEXT, raw TEXT, fetched_at TEXT);
            CREATE TABLE IF NOT EXISTS date_state (stamp TEXT PRIMARY KEY, state TEXT NOT NULL, reference_hash TEXT NOT NULL);
        ''')
        if 'row_sha' not in {row[1] for row in self.db.execute('PRAGMA table_info(rows)')}:
            raise DataUnavailable('historical_store_schema_invalid')

    def close(self):
        self.db.close()

    def source_generation(self):
        """External commits conservatively invalidate; own derived writes do not."""
        return (self.db.execute('PRAGMA data_version').fetchone()[0],
                self.db.execute('PRAGMA schema_version').fetchone()[0],
                self.db.total_changes - self._artifact_changes)

    def capture_attempt(self, endpoint, params, records, fetched_at):
        payload = dict(endpoint=endpoint, params=params, rows=records)
        with self.db:
            self.db.execute('INSERT INTO attempts(key,sha,raw,fetched_at) VALUES(?,?,?,?)',
                (digest(dict(endpoint=endpoint, params=params)), digest(payload),
                 json.dumps(payload, ensure_ascii=False, allow_nan=False), fetched_at))

    def date_status(self, stamp, reference_hash, state=None):
        stamp = day(stamp).strftime('%Y%m%d')
        if state is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO date_state VALUES(?,?,?)', (stamp, state, reference_hash))
            return state
        row = self.db.execute('SELECT state,reference_hash FROM date_state WHERE stamp=?', (stamp,)).fetchone()
        return row[0] if row and row[1] == reference_hash else None

    def put(self, endpoint, params, records, fetched_at):
        key = digest(dict(endpoint=endpoint, params=params))
        payload = dict(endpoint=endpoint, params=params, rows=records)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        hashed = digest(payload)
        stamp = str(params.get('trade_date', ''))
        with self.db:
            existing = self.db.execute('SELECT sha FROM batches WHERE key=?', (key,)).fetchone()
            if existing:
                if existing[0] != hashed:
                    raise DataUnavailable('immutable_batch_conflict')
                return key
            self.db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)', (key, endpoint, stamp, hashed, raw, fetched_at))
            self.db.executemany('INSERT INTO rows VALUES(?,?,?,?,?,?)', [(key, endpoint, str(row.get('ts_code', '')),
                str(row.get('trade_date', '')), json.dumps(row, ensure_ascii=False, allow_nan=False), digest(row)) for row in records])
        return key

    def batch(self, endpoint, params):
        key = digest(dict(endpoint=endpoint, params=params))
        row = self.db.execute('SELECT sha,raw,fetched_at FROM batches WHERE key=?', (key,)).fetchone()
        if row is None:
            raise DataUnavailable('batch_missing:' + endpoint)
        payload = json.loads(row[1])
        if digest(payload) != row[0]:
            raise DataUnavailable('batch_hash_invalid')
        return payload['rows'], row[0], row[2]

    def row(self, endpoint, code, stamp):
        rows = self.db.execute('SELECT raw,row_sha FROM rows WHERE endpoint=? AND code=? AND stamp=?',
                              (endpoint, code, day(stamp).strftime('%Y%m%d'))).fetchall()
        values = {row[1]: self.decode_row(row) for row in rows}
        if len(values) != 1:
            raise DataUnavailable('execution_row_missing_or_conflicting:' + endpoint)
        return next(iter(values.values()))

    def fingerprint(self):
        # Only compact batch hashes, never all market records.
        for batch_key, expected, raw in self.db.execute('SELECT key,sha,raw FROM batches'):
            payload = json.loads(raw)
            if digest(payload) != expected:
                raise DataUnavailable('batch_hash_invalid')
            indexed = [r[0] for r in self.db.execute('SELECT row_sha FROM rows WHERE batch_key=?', (batch_key,))]
            if sorted(indexed) != sorted(digest(r) for r in payload['rows']):
                raise DataUnavailable('indexed_row_coverage_invalid')
        return digest(self.db.execute('SELECT key,sha FROM batches ORDER BY key').fetchall())

    @staticmethod
    def decode_row(row):
        value = json.loads(row[0])
        if digest(value) != row[1]:
            raise DataUnavailable('indexed_row_hash_invalid')
        return value

    def artifact(self, key, value=None):
        if value is not None:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
            before = self.db.total_changes
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?)', (key, digest(value), raw))
            # Discount only the one confirmed derived-row write. Unexpected
            # trigger/source writes, failures and rollbacks remain conservative.
            if self.db.total_changes - before == 1:
                self._artifact_changes += 1
            return value
        row = self.db.execute('SELECT sha,raw FROM artifacts WHERE key=?', (key,)).fetchone()
        if row is None:
            return None
        value = json.loads(row[1])
        if digest(value) != row[0]:
            raise DataUnavailable('artifact_hash_invalid')
        return value


class Acquirer:
    """Explicit bounded provider calls; endpoint/parameter schemas are fixed."""
    def __init__(self, store, client, *, max_requests=1000, page_size=5000,
                 request_interval=.4, clock=time.monotonic, sleep=time.sleep):
        if max_requests < 1 or not 1 <= page_size <= 5000:
            raise ValueError('acquisition_budget_invalid')
        self.store, self.client = store, client
        self.max_requests, self.page_size, self.requests = max_requests, page_size, 0
        self.reference_vintage = None
        self.request_interval, self.clock, self.sleep = max(.0, request_interval), clock, sleep
        self._last_request = None
        self.staged = {}

    def batch(self, endpoint, *, stage=None, **params):
        if stage is None:
            stage = endpoint in {'daily', 'daily_basic', 'adj_factor', 'stk_limit', 'suspend_d'}
        capture_params = dict(params)
        if self.reference_vintage and endpoint in {'stock_basic', 'namechange'}:
            capture_params['reference_vintage'] = self.reference_vintage
        try:
            return self.store.batch(endpoint, capture_params)[0]
        except DataUnavailable as exc:
            if not str(exc).startswith('batch_missing:'):
                raise
        rows, seen = [], set()
        for offset in range(0, 100000, self.page_size):
            if self.requests >= self.max_requests:
                raise DataUnavailable('request_budget_exhausted_restart_safe')
            for attempt in range(3):
                if self.requests >= self.max_requests:
                    raise DataUnavailable('request_budget_exhausted_restart_safe')
                self.requests += 1
                if self._last_request is not None:
                    self.sleep(max(0., self.request_interval - (self.clock() - self._last_request)))
                try:
                    table = self.client.query(endpoint, fields=FIELDS[endpoint], limit=self.page_size,
                        offset=offset, request_timeout=15, **params)
                    break
                except Exception as error:
                    message = str(error).lower()
                    if re.search(r'权限|积分|permission|denied|unauthori|forbidden', message):
                        raise DataUnavailable('source_permission_denied') from None
                    if re.search(r'每分钟|频率|rate.?limit|too many|429', message):
                        raise DataUnavailable('source_rate_limited_restart_later') from None
                    if attempt == 2:
                        raise DataUnavailable('source_request_failed') from None
                    self.sleep(attempt + 1.)
                finally:
                    self._last_request = self.clock()
            if not isinstance(table, pd.DataFrame) or table.columns.has_duplicates:
                raise DataUnavailable('source_schema_invalid')
            if set(table.columns) != set(FIELDS[endpoint].split(',')):
                raise DataUnavailable('source_schema_missing_fields:' + endpoint)
            records = table.astype(object).where(table.notna(), None).to_dict('records')
            if len(records) > self.page_size:
                raise DataUnavailable('source_pagination_invalid')
            for row in records:
                code = row.get('ts_code', '')
                if not _standard_identity(code) and not (endpoint == 'stock_basic'
                        and params.get('list_status') == 'D' and _out_of_period_legacy_listing(row)) and not (
                        endpoint == 'namechange' and isinstance(code, str)
                        and re.fullmatch(r'[A-Z0-9]{1,16}\.(SH|SZ|BJ)', code)):
                    raise DataUnavailable('source_identity_invalid')
                if 'trade_date' in params and row.get('trade_date') != params['trade_date']:
                    raise DataUnavailable('source_date_invalid')
                identity = (code, row.get('trade_date', '')) if endpoint not in {'namechange', 'suspend_d'} else digest(row)
                if identity in seen:
                    if endpoint == 'namechange':
                        continue  # Broad pagination is not certified; interval audit below is mandatory.
                    raise DataUnavailable('source_duplicate_or_unstable_pagination')
                seen.add(identity)
                rows.append(row)
            if len(records) < self.page_size:
                stamp = datetime.now(timezone.utc).isoformat()
                self.store.capture_attempt(endpoint, capture_params, rows, stamp)
                if stage:
                    self.staged[endpoint] = (capture_params, rows, stamp)
                else:
                    self.store.put(endpoint, capture_params, rows, stamp)
                return rows
        raise DataUnavailable('source_pagination_cap')


class HistorySession(Mapping):
    """Success-only LRU of one signal's assembled histories, not trusted features."""
    def __init__(self, dataset, snapshot, signal, max_bytes):
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError('history_cache_budget_invalid')
        self._dataset, self._signal = dataset, day(signal)
        self._prices = dict(zip(snapshot.code, snapshot.price))
        self.max_bytes, self.used_bytes = max_bytes, 0
        self._entries = OrderedDict()
        self._generation = self._context()

    def __len__(self):
        return len(self._prices)

    def __iter__(self):
        return iter(self._prices)

    def _context(self):
        references = self._dataset.references
        # The frozen snapshot already owns classification/name inputs. History
        # additionally consumes mutable alias/calendar contents; fingerprint only
        # those small inputs, not every security's classification on every hit.
        return (id(self._dataset.store), self._dataset.store.source_generation(), id(references),
                digest(dict(hash=references.hash, aliases=references.aliases,
                            calendars=references.calendars)))

    def _discard(self, generation):
        self._entries.clear()
        self.used_bytes = 0
        self._generation = generation
        self._dataset._masters = self._dataset._names = None
        self._dataset.references._calendar_cache.clear()

    @staticmethod
    def _copy(frame):
        result = frame.copy(deep=True)
        result.attrs = deepcopy(frame.attrs)
        return result

    @staticmethod
    def _size(frame, key):
        def objects(value):
            size = sys.getsizeof(value)
            if isinstance(value, dict):
                size += sum(objects(k) + objects(v) for k, v in value.items())
            elif isinstance(value, (list, tuple)):
                size += sum(objects(v) for v in value)
            return size
        # Deliberately conservative: account for frame storage and Python/index
        # overhead, nested provenance, key and LRU entry, not only numeric cells.
        return int(frame.memory_usage(index=True, deep=True).sum()) + sys.getsizeof(frame) + objects(frame.attrs) + objects(key) + 256

    def __getitem__(self, code):
        from src.services.screening.backtest_parallel import uncertain_transaction
        transactional = uncertain_transaction(self._dataset.store)
        if transactional:
            # total_changes does not change on ROLLBACK (including ROLLBACK TO).
            # Never carry any derived input into or out of an uncertain read.
            self._discard(self._context())
        try:
            return self._read(code, transactional)
        finally:
            if transactional or uncertain_transaction(self._dataset.store):
                self._discard(self._context())

    def _read(self, code, transactional):
        from src.services.screening.backtest_parallel import uncertain_transaction
        price = self._prices[code]
        generation = self._context()
        if generation != self._generation:
            self._discard(generation)
        key = (code, self._signal, float(price))
        cached = self._entries.get(key)
        if cached is None:
            frame = self._dataset.history(code, self._signal, price)
            stored = self._copy(frame)
            size = self._size(stored, key)
        else:
            stored, size = cached
        result = self._copy(stored)
        after = self._context()
        if after != generation or uncertain_transaction(self._dataset.store) != transactional:
            self._discard(after)
            raise DataUnavailable('history_source_changed_during_read')
        if cached is not None:
            self._entries.move_to_end(key)
        elif not transactional and size <= self.max_bytes:
            while self.used_bytes + size > self.max_bytes:
                _, (_, evicted) = self._entries.popitem(last=False)
                self.used_bytes -= evicted
            self._entries[key] = (stored, size)
            self.used_bytes += size
        return result


class HistoricalDataset:
    def __init__(self, store, references):
        self.store, self.references = store, references
        self._masters = None
        self._names = None

    def reference_params(self, **params):
        value = self.store.db.execute("SELECT MAX(json_extract(raw,'$.params.reference_vintage')) FROM batches WHERE endpoint='stock_basic'").fetchone()[0]
        return dict(params, reference_vintage=value) if value else params

    @property
    def fingerprint(self):
        return digest(dict(market=self.store.fingerprint(), references=self.references.hash))

    @property
    def last_available_date(self):
        # A partially committed date is resumable, never a freshness watermark.
        required = ('daily', 'daily_basic', 'adj_factor', 'stk_limit', 'suspend_d')
        values = self.store.db.execute("SELECT stamp FROM batches WHERE endpoint IN ('daily','daily_basic','adj_factor','stk_limit','suspend_d') "
            "AND stamp<>'' GROUP BY stamp HAVING COUNT(DISTINCT endpoint)=5 ORDER BY stamp DESC").fetchall()
        for value in values:
            stamp = value[0]
            if self.store.date_status(stamp, self.references.hash) != 'complete':
                continue
            batches = {endpoint: self.store.batch(endpoint, dict(trade_date=stamp))[0] for endpoint in required}
            if all(batches[endpoint] for endpoint in required if endpoint != 'suspend_d'):
                return day(stamp)
        raise DataUnavailable('last_available_date_unverified: resume explicit acquisition to validate dates')

    def masters(self):
        if self._masters is None:
            rows = []
            reference_params = self.reference_params()
            for status in ('L', 'D', 'P'):
                rows.extend(self.store.batch('stock_basic', dict(reference_params, exchange='', list_status=status))[0])
            identities = {}
            for row in rows:
                if _standard_identity(row['ts_code']):
                    canonical = self.references.canonical(row['ts_code'])
                elif _out_of_period_legacy_listing(row):
                    canonical = row['ts_code']  # Opaque; never strip T or manufacture an alias.
                else:
                    raise DataUnavailable('source_identity_invalid')
                if canonical in identities and identities[canonical] != row:
                    raise DataUnavailable('historical_listing_alias_conflict')
                if not row.get('list_date') or (row.get('list_status') == 'D' and not row.get('delist_date')):
                    raise DataUnavailable('historical_listing_dates_missing')
                identities[canonical] = row
            if not identities:
                raise DataUnavailable('historical_universe_empty')
            self._masters = identities
        return self._masters

    def names(self):
        if self._names is None:
            self._names = {}
            reference_params = self.reference_params()
            broad = self.store.batch('namechange', reference_params)[0]
            for value in broad:
                key = self._name_master_key(value['ts_code'])
                if key is not None:
                    self._names.setdefault(key, []).append(value)
            for code, master in self.masters().items():
                try:
                    repaired = self.store.batch('namechange', dict(reference_params, ts_code=master['ts_code']))[0]
                except DataUnavailable as exc:
                    if not str(exc).startswith('batch_missing:'):
                        raise
                    continue
                if any(self._name_master_key(value['ts_code']) != code for value in repaired):
                    raise DataUnavailable('historical_name_identity_conflict')
                self._names[code] = repaired
        return self._names

    def _name_master_key(self, code):
        key = self.references.canonical(code) if _standard_identity(code) else code
        return key if key in self.masters() else None

    def universe(self, signal):
        """Listed lifetime is [list_date, delist_date), not last-trade coverage."""
        signal = day(signal)
        members = {code: row for code, row in self.masters().items() if day(row['list_date']) <= signal
                and (not row.get('delist_date') or signal < day(row['delist_date']))}
        if any(not _standard_identity(row['ts_code']) for row in members.values()):
            raise DataUnavailable('legacy_listing_overlaps_requested_date')
        return members

    def legacy_listing_audit(self, start):
        """Dated scope receipt; raw masters remain immutable and queryable."""
        boundary = min(day(start), _fixed_history_start())
        legacy = [row for row in self.masters().values() if not _standard_identity(row['ts_code'])]
        if any(day(row['delist_date']) > boundary for row in legacy):
            raise DataUnavailable('legacy_listing_overlaps_coverage')
        _, batch_hash, _ = self.store.batch('stock_basic', self.reference_params(exchange='', list_status='D'))
        return dict(coverage_start=boundary.isoformat(), records=[dict(row=row, row_sha256=digest(row),
            batch_sha256=batch_hash, reason='legacy_listing_ended_before_or_at_coverage') for row in legacy])

    def name_reference_audit(self):
        rows, batch_hash, _ = self.store.batch('namechange', self.reference_params())
        result = dict(name_records=[], unmatched_name_records=[])
        for row in rows:
            key = self._name_master_key(row['ts_code'])
            if key is None:
                group, reason = 'unmatched_name_records', 'not_in_validated_listing_master'
            elif not _standard_identity(row['ts_code']):
                group, reason = 'name_records', 'bound_to_out_of_period_legacy_listing'
            else:
                continue
            result[group].append(dict(row=row, row_sha256=digest(row), batch_sha256=batch_hash, reason=reason))
        return result

    def sessions(self, exchange, start, end):
        values, _ = self.references.calendar(exchange, start, end)
        return sorted(d for d, opened in values.items() if opened)

    def suspension(self, code, stamp, events, quotes):
        """Replay is pure: a conflicting event needs previously captured proof."""
        try:
            return suspension_state(events)
        except ValueError:
            if not suspension_proof_candidate(events, quotes, code=code, stamp=stamp):
                raise
        params = dict(ts_code=code, trade_date=day(stamp).strftime('%Y%m%d'))
        records, hashed, fetched = self.store.batch('baostock_suspension', params)
        state = verified_suspension_proof(events, quotes, records, code=code, stamp=stamp)
        state['independent_proof'] = dict(source='baostock.tradestatus', evidence_id=hashed,
            fetched_at=fetched, requested_date=day(stamp).isoformat(), primary_events_sha256=digest(events))
        return state

    def snapshot(self, signal, *, primary_batches=None, allow_pending_pb=False):
        signal = day(signal)
        stamp = signal.strftime('%Y%m%d')
        universe = self.universe(signal)
        if not universe:
            raise DataUnavailable('historical_universe_empty')
        eligible, exclusions = [], []
        def batch(endpoint):
            if primary_batches is not None:
                rows = primary_batches[endpoint]
                return rows, digest(rows), None
            return self.store.batch(endpoint, dict(trade_date=stamp))
        daily, quote_hash, _ = batch('daily')
        basic, basic_hash, _ = batch('daily_basic')
        suspensions, suspension_hash, _ = batch('suspend_d')
        quote_map = {r['ts_code']: r for r in daily}
        basic_map = {r['ts_code']: r for r in basic}
        field_sources = {}
        pending_pb = []
        for code, master in sorted(universe.items()):
            exchange = EXCHANGES[master['ts_code'][-2:]]
            try:
                state = name_state(self.names().get(code, []), signal)
            except ValueError:
                state = dict(name='', status='unknown')
            eligibility = self.references.eligibility(code, master['list_date'], signal, exchange,
                                                      digest(master), name_status=state['status'])
            provider_code = code + '.' + master['ts_code'][-2:]
            if not eligibility['eligible']:
                exclusions.append(dict(code=code, **eligibility, name_state=state['status']))
                continue
            suspension = self.suspension(provider_code, signal,
                [r for r in suspensions if r['ts_code'] == provider_code], daily)
            primary = basic_map.get(provider_code)
            if state['status'] == 'normal' and suspension['status'] != 'full_day' and primary and missing(primary.get('pb')) and not missing(primary.get('pe_ttm')):
                try:
                    supplemental = self.store.row('baostock_pb', provider_code, signal)
                except DataUnavailable as exc:
                    if not str(exc).startswith('execution_row_missing'):
                        raise
                else:
                    updated, metadata = supplement_pb(primary, quote_map.get(provider_code), supplemental,
                                                      evidence_id=digest(supplemental))
                    basic_map[provider_code] = updated
                    if metadata:
                        field_sources[code] = dict(pb_ratio=metadata)
            scoring = scoring_eligibility(reference=eligibility, name_status=state['status'],
                daily=quote_map.get(provider_code), basic=basic_map.get(provider_code), suspension=suspension['status'],
                evidence_id=digest([quote_hash, basic_hash, suspension_hash, suspension.get('independent_proof')]),
                allow_pending_pb=allow_pending_pb)
            if scoring.get('pending_pb'):
                pending_pb.append(provider_code)
            if not scoring['eligible']:
                exclusions.append(dict(code=code, **scoring, name_state=state['status'],
                    suspension_evidence=dict(primary=suspension_hash, daily=quote_hash,
                                             independent=suspension.get('independent_proof'))))
                continue
            eligible.append(dict(master, symbol=code, ts_code=code + '.' + master['ts_code'][-2:],
                                 name=state['name'], **eligibility))
        if not eligible:
            return pd.DataFrame(), dict(exclusions=exclusions, universe_count=len(universe), pending_pb=[])
        codes = {r['ts_code'] for r in eligible}
        # All expected eligible securities remain in the frame, even missing quotes.
        quotes = pd.DataFrame([r for r in daily if r['ts_code'] in codes], columns=FIELDS['daily'].split(','))
        basics = pd.DataFrame([r for r in basic_map.values() if r['ts_code'] in codes], columns=FIELDS['daily_basic'].split(','))
        reference = pd.DataFrame(eligible)
        frame = _prepare_tushare_snapshot(quotes, basics, reference, preserve_invalid_rows=True)
        canonical = reference.set_index('symbol')
        for field in ('industry', 'industry_code', 'industry_taxonomy', 'industry_as_of', 'industry_source', 'industry_evidence_id'):
            frame[field] = frame.code.map(canonical[field])
        frame['name_as_of'] = signal.isoformat()
        frame.attrs['field_trade_date_columns']['name'] = 'name_as_of'
        frame.attrs.update(signal_date=signal.isoformat(), snapshot_source='historical_tushare',
            evidence_id=digest(dict(quotes=quote_hash, basics=basic_hash, refs=self.references.hash)),
            field_source_overrides=field_sources)
        return frame, dict(exclusions=exclusions, universe_count=len(universe), eligible_count=len(frame), pending_pb=pending_pb)

    def history(self, code, signal, price):
        master = self.masters()[code]
        suffix = '.' + master['ts_code'][-2:]
        provider_codes = [code + suffix, *[old + suffix for old, new in self.references.aliases.items() if new == code]]
        start, end = strict_history_start(signal).strftime('%Y%m%d'), signal.strftime('%Y%m%d')
        values = {}
        for endpoint in ('daily', 'adj_factor'):
            markers = ','.join('?' for _ in provider_codes)
            records = self.store.db.execute(f'SELECT raw,row_sha FROM rows WHERE endpoint=? AND code IN ({markers}) AND stamp>=? AND stamp<=? ORDER BY stamp',
                (endpoint, *provider_codes, start, end)).fetchall()
            values[endpoint] = [self.store.decode_row(row) for row in records]
            for value in values[endpoint]:
                value['ts_code'] = code + suffix
        if not values['daily'] or not values['adj_factor']:
            raise DataUnavailable('history_missing')
        daily = pd.DataFrame(values['daily'])
        factors = pd.DataFrame(values['adj_factor'])
        expected = self.sessions(EXCHANGES[master['ts_code'][-2:]], max(strict_history_start(signal), day(master['list_date'])), signal)
        observed = {day(value) for value in daily.trade_date}
        def suspension_rows(stamp):
            suspended, _, _ = self.store.batch('suspend_d', dict(trade_date=stamp.strftime('%Y%m%d')))
            return [r for r in suspended if r['ts_code'] in provider_codes]
        proofs = {}
        def resolve(stamp):
            events = suspension_rows(stamp)
            provider = events[0]['ts_code'] if events else code + suffix
            state = self.suspension(provider, stamp, events, [])
            if 'independent_proof' in state:
                params = dict(trade_date=stamp.strftime('%Y%m%d'))
                proofs[stamp.isoformat()] = dict(state['independent_proof'],
                    primary_batch_sha256=self.store.batch('suspend_d', params)[1],
                    daily_batch_sha256=self.store.batch('daily', params)[1])
            return state
        validate_history_sessions(expected, observed, suspension_rows, resolve=resolve)
        frame = prepare_dated_history(daily, factors, code=code, signal_date=signal,
            source='historical_tushare', evidence_id=digest(dict(values=values, suspension_proofs=proofs)))
        frame.attrs['suspension_proofs'] = proofs
        return validate_history(frame, code, signal, price)

    def histories(self, snapshot, signal, *, max_bytes=128 * 1024 * 1024):
        return HistorySession(self, snapshot, signal, max_bytes)

    def execution(self, code, session):
        stamp = day(session).strftime('%Y%m%d')
        provider_code = code + '.' + self.masters()[code]['ts_code'][-2:]
        suspended, _, _ = self.store.batch('suspend_d', dict(trade_date=stamp))
        quotes, _, _ = self.store.batch('daily', dict(trade_date=stamp))
        suspension = self.suspension(provider_code, session,
            [r for r in suspended if r['ts_code'] == provider_code], quotes)
        if suspension['status'] == 'full_day':
            return dict(suspended=True)
        daily = self.store.row('daily', provider_code, session)
        factors = self.store.row('adj_factor', provider_code, session)
        limits = self.store.row('stk_limit', provider_code, session)
        return dict(daily, **{k: factors[k] for k in ('adj_factor',)}, up_limit=limits['up_limit'],
                    down_limit=limits['down_limit'], suspended=False, no_limit=False,
                    suspended_open=suspension['suspended_open'], suspended_close=suspension['suspended_close'])

    def benchmark(self, session, field):
        return self.store.row('index_daily', '000300.SH', session)[field]


def acquire_market(acquirer, references, start, end, progress=None, pb_fetcher=fetch_pb_range,
                   suspension_fetcher=fetch_suspension_status):
    """Full date batches; repair only names with audited interval failures."""
    acquirer.reference_vintage = day(end).isoformat()
    for status in ('L', 'D', 'P'):
        acquirer.batch('stock_basic', exchange='', list_status=status)
    dataset = HistoricalDataset(acquirer.store, references)
    reference_audit = dataset.legacy_listing_audit(start)
    acquirer.batch('namechange')
    reference_audit.update(dataset.name_reference_audit())
    acquirer.store.artifact('legacy_listing_scope_v1', reference_audit)
    sessions = sorted(set().union(*(dataset.sessions(exchange, start, end) for exchange in EXCHANGES.values())))
    from src.services.screening.backtest import FIXED
    qualification_start = min(day(value) for value in FIXED['start_dates'])
    gaps = {}
    for signal in sessions:
        if acquirer.store.date_status(signal, references.hash) == 'complete':
            continue
        for code, master in dataset.universe(signal).items():
            try:
                name_state(dataset.names().get(code, []), signal)
            except ValueError:
                acquirer.batch('namechange', ts_code=master['ts_code'])
                dataset._names = None
        endpoints = ('daily', 'daily_basic', 'adj_factor', 'stk_limit', 'suspend_d')
        tables = {endpoint: acquirer.batch(endpoint, stage=True, trade_date=signal.strftime('%Y%m%d'))
                  for endpoint in endpoints}
        events_by_provider = {}
        for row in tables['suspend_d']:
            events_by_provider.setdefault(row['ts_code'], []).append(row)
        listed_providers = {code + '.' + master['ts_code'][-2:] for code, master in dataset.universe(signal).items()}
        for code, events in sorted(events_by_provider.items()):
            if code not in listed_providers or not suspension_proof_candidate(events, tables['daily'], code=code, stamp=signal):
                continue
            params = dict(ts_code=code, trade_date=signal.strftime('%Y%m%d'))
            try:
                records = acquirer.store.batch('baostock_suspension', params)[0]
            except DataUnavailable as exc:
                if not str(exc).startswith('batch_missing:'):
                    raise
                if acquirer.requests >= acquirer.max_requests:
                    raise DataUnavailable('request_budget_exhausted_restart_safe')
                acquirer.requests += 1
                if acquirer._last_request is not None:
                    acquirer.sleep(max(0., acquirer.request_interval - (acquirer.clock() - acquirer._last_request)))
                try:
                    records = suspension_fetcher(code, signal)
                except Exception:
                    raise DataUnavailable('suspension_proof_request_failed') from None
                finally:
                    acquirer._last_request = acquirer.clock()
                fetched = datetime.now(timezone.utc).isoformat()
                acquirer.store.capture_attempt('baostock_suspension', params, records, fetched)
                verified_suspension_proof(events, tables['daily'], records, code=code, stamp=signal)
                acquirer.store.put('baostock_suspension', params, records, fetched)
            verified_suspension_proof(events, tables['daily'], records, code=code, stamp=signal)
        # Preserve all raw attempts, but do not freeze a short/incomplete date.
        # A full-day suspension is the sole explanation for absent daily rows;
        # qualification may additionally prove an ineligible scoring security.
        unresolved_events = []
        if signal >= qualification_start:
            qualified, coverage = dataset.snapshot(signal, primary_batches=tables, allow_pending_pb=True)
            for code in coverage['pending_pb']:
                gaps.setdefault(code, []).append(signal)
            if not qualified.empty and not set(qualified.code) <= {references.canonical(r['ts_code']) for r in tables['stk_limit']}:
                raise DataUnavailable('primary_limits_coverage_incomplete_retry')
        else:
            expected = {references.canonical(master['ts_code']) for master in dataset.universe(signal).values()}
            missing_codes = set().union(*(expected - {references.canonical(r['ts_code']) for r in tables[endpoint]}
                                          for endpoint in ('daily', 'daily_basic')))
            events_by_code = {}
            for row in tables['suspend_d']:
                events_by_code.setdefault(references.canonical(row['ts_code']), []).append(row)
            # Warmup requires raw bars, not executable timing. Only missing bars
            # need a full-day explanation; ambiguous events never provide one.
            for code in sorted(missing_codes):
                master = dataset.universe(signal)[code]
                provider = code + '.' + master['ts_code'][-2:]
                if dataset.suspension(provider, signal, events_by_code.get(code, []), tables['daily'])['status'] != 'full_day':
                    raise DataUnavailable('primary_date_coverage_incomplete_retry')
            for code in sorted((expected - missing_codes) & events_by_code.keys()):
                events = events_by_code[code]
                try:
                    suspension_state(events)
                except ValueError as exc:
                    if str(exc) not in {'suspension_schema_unknown', 'suspension_timing_unknown', 'suspension_events_conflict'}:
                        raise
                    unresolved_events.append(dict(code=code, error=str(exc), events=events, events_sha256=digest(events)))
        if not {references.canonical(r['ts_code']) for r in tables['daily']} <= {references.canonical(r['ts_code']) for r in tables['adj_factor']}:
            raise DataUnavailable('primary_factor_coverage_incomplete_retry')
        if not all(tables[endpoint] for endpoint in endpoints if endpoint != 'suspend_d'):
            raise DataUnavailable('primary_date_empty_retry')
        for endpoint, (params, rows, fetched_at) in acquirer.staged.items():
            acquirer.store.put(endpoint, params, rows, fetched_at)
        acquirer.staged.clear()
        if unresolved_events:
            stamp = signal.strftime('%Y%m%d')
            _, suspension_hash, _ = acquirer.store.batch('suspend_d', dict(trade_date=stamp))
            acquirer.store.artifact('warmup_event_timing_v1:' + stamp, dict(scope='raw_warmup_only',
                business_date=signal.isoformat(), reference_hash=references.hash,
                suspension_batch_sha256=suspension_hash, unresolved_events=unresolved_events))
        acquirer.store.date_status(signal, references.hash, 'primary_validated')
        acquirer.batch('index_daily', ts_code='000300.SH', trade_date=signal.strftime('%Y%m%d'))
        if progress:
            progress(dict(stage='acquired', date=signal.isoformat(), requests=acquirer.requests))
    # One bounded range per affected security, only dates not already cached.
    for code, dates in sorted(gaps.items()):
        if code[-2:] not in {'SH', 'SZ'}:
            raise DataUnavailable('pb_supplement_exchange_unsupported')
        params = dict(ts_code=code, start_date=min(dates).isoformat(), end_date=max(dates).isoformat())
        if acquirer.requests >= acquirer.max_requests:
            raise DataUnavailable('request_budget_exhausted_restart_safe')
        acquirer.requests += 1
        try:
            records = pb_fetcher(code, params['start_date'], params['end_date'])
        except Exception:
            raise DataUnavailable('pb_supplement_request_failed') from None
        normalized = [dict(row, ts_code=code, trade_date=day(row['date']).strftime('%Y%m%d')) for row in records]
        fetched_at = datetime.now(timezone.utc).isoformat()
        acquirer.store.capture_attempt('baostock_pb', params, normalized, fetched_at)
        if (len({r['trade_date'] for r in normalized}) != len(normalized)
                or any(not min(dates) <= day(r['date']) <= max(dates) for r in normalized)):
            raise DataUnavailable('pb_supplement_date_or_duplicate_invalid')
        by_date = {day(row['date']): row for row in normalized}
        for stamp in dates:
            if stamp not in by_date:
                raise DataUnavailable('pb_supplement_incomplete_retry')
            supplement_pb(acquirer.store.row('daily_basic', code, stamp),
                acquirer.store.row('daily', code, stamp), by_date[stamp], evidence_id=digest(by_date[stamp]))
        acquirer.store.put('baostock_pb', params, normalized, fetched_at)
    for signal in sessions:
        if acquirer.store.date_status(signal, references.hash) == 'complete':
            continue
        if signal >= qualification_start:
            dataset.snapshot(signal)  # Actual strict qualification: no pending allowance.
        acquirer.store.date_status(signal, references.hash, 'complete')
    return dataset


def screen_closed_session(strategy, config, *, dataset=None, signal_date=None, max_output=None, refresh=None):
    """Ordinary strict API/CLI entry; uses hydrated dated inputs, never Sina.

    Initial hydration is explicit. Existing ordinary-entry caches incrementally
    acquire only missing closed dates; injected/pure calculation never loads env.
    """
    from zoneinfo import ZoneInfo
    from src.services.research_delivery.screening_preview import preview_strategy
    from src.services.screening.models import ScreenResult
    owned = dataset is None
    if owned:
        refs = ReferenceBundle.load(config.data_dir / 'historical_references')
        path = config.data_dir / 'screening_history.sqlite'
        if not path.is_file():
            raise DataUnavailable('closed_session_dataset_missing: hydrate historical date batches')
        dataset = HistoricalDataset(BatchStore(path), refs)
    try:
        if signal_date is None:
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            cutoff = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
            sessions = dataset.sessions('SSE', cutoff - timedelta(days=30), cutoff)
            if not sessions:
                raise DataUnavailable('closed_session_calendar_unknown')
            signal_date = sessions[-1]
        if dataset.last_available_date < signal_date:
            start = dataset.last_available_date + timedelta(days=1)
            if refresh is not None:
                refresh(dataset, start, signal_date)
            elif owned:
                import os
                from data_provider.tushare_fetcher import create_tushare_http_client
                token = os.getenv('TUSHARE_TOKEN', '').strip() or os.getenv('TUSHARE_API_TOKEN', '').strip()
                if not token:
                    raise DataUnavailable('closed_session_refresh_credentials_missing')
                acquire_market(Acquirer(dataset.store, create_tushare_http_client(token, timeout=15), max_requests=100),
                               dataset.references, start, signal_date)
            if dataset.last_available_date < signal_date:
                raise DataUnavailable('closed_session_dataset_stale: resume incremental acquisition')
            dataset._masters = dataset._names = None
        snapshot, coverage = dataset.snapshot(signal_date)
        if snapshot.empty:
            return ScreenResult(strategy=strategy, market='cn', snapshot_count=coverage['universe_count'],
                                after_filter_count=0, degradation=['verified_no_eligible_securities'])
        preview = preview_strategy(strategy, snapshot, trade_date=signal_date, universe=tuple(snapshot.code),
            calendar_verified=True, universe_verified=True, daily_histories=dataset.histories(snapshot, signal_date),
            config=config, max_output=max_output)
        if preview.result is None:
            raise DataUnavailable('closed_session_required_evidence_incomplete')
        preview.result.degradation.append('closed_session:' + signal_date.isoformat())
        return preview.result
    finally:
        if owned:
            dataset.store.close()
