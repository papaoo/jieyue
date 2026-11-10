"""Synthetic independent suspension proofs; no network or production stores."""
import pytest
from datetime import date
import pandas as pd

from src.services.screening.eligibility import suspension_state


def events(code='603897.SH', stamp='20251104', timing=None):
    return [dict(ts_code=code, trade_date=stamp, suspend_type=t,
                 suspend_timing=timing if t == 'S' else None) for t in ('S', 'R')]


def record():
    return dict(date='2025-11-04', code='sh.603897', open='26.5300', high='26.5300',
                low='26.5300', close='26.5300', volume='', amount='', tradestatus='0')


@pytest.mark.parametrize('timing', [None, '09:30-09:30'])
def test_independent_proof_never_relaxes_default_parser(timing):
    from src.services.screening.eligibility import verified_suspension_proof
    with pytest.raises(ValueError, match='suspension_events_conflict|suspension_timing_unknown'):
        suspension_state(events(timing=timing))
    result = verified_suspension_proof(events(timing=timing), [], [record()], code='603897.SH', stamp='20251104')
    assert result == dict(status='full_day', suspended_open=True, suspended_close=True)
    assert not any(k in result for k in ('open', 'close', 'price'))


@pytest.mark.parametrize('field', list(record()))
def test_independent_proof_requires_every_secondary_column(field):
    from src.services.screening.eligibility import verified_suspension_proof
    row = record()
    del row[field]
    with pytest.raises(ValueError, match='schema_invalid'):
        verified_suspension_proof(events(), [], [row], code='603897.SH', stamp='20251104')


@pytest.mark.parametrize('change', [dict(code='sh.600001'), dict(date='2025-11-03'),
    dict(tradestatus='1'), dict(volume='0'), dict(amount='1'), dict(close='26.54'),
    dict(open='nan'), dict(open='0'), dict(open=True), dict(volume=None)])
def test_independent_proof_rejects_unsupported_secondary(change):
    from src.services.screening.eligibility import verified_suspension_proof
    with pytest.raises(ValueError):
        verified_suspension_proof(events(), [], [dict(record(), **change)], code='603897.SH', stamp='20251104')


@pytest.mark.parametrize('primary,quotes,records,code', [
    (events()[:1], [], [record()], '603897.SH'),
    (events(), [dict(ts_code='603897.SH', trade_date='20251104')], [record()], '603897.SH'),
    (events(), [], [], '603897.SH'), (events(), [], [record(), record()], '603897.SH'),
    (events('920001.BJ'), [], [record()], '920001.BJ'),
    (events(stamp='20251103'), [], [record()], '603897.SH'),
    ([dict(r, suspend_timing='09:30-10:00') for r in events()], [], [record()], '603897.SH'),
])
def test_independent_proof_requires_exact_narrow_primary(primary, quotes, records, code):
    from src.services.screening.eligibility import verified_suspension_proof
    with pytest.raises(ValueError):
        verified_suspension_proof(primary, quotes, records, code=code, stamp='20251104')


@pytest.mark.parametrize('timing', ['09:31-09:31', '15:00-15:00', '09:30-09:31',
                                  '09:30-09:30,09:30-09:30', ' 09:30-09:30', 'unknown'])
def test_other_timing_forms_never_qualify_for_independent_proof(timing):
    from src.services.screening.eligibility import verified_suspension_proof
    with pytest.raises(ValueError, match='not_applicable'):
        verified_suspension_proof(events(timing=timing), [], [record()], code='603897.SH', stamp='20251104')


@pytest.mark.parametrize('extra', [None, '', '09:30-09:31', '09:31-09:31'])
def test_zero_timing_cannot_mix_other_suspension_timings(extra):
    from src.services.screening.eligibility import verified_suspension_proof
    rows = events(timing='09:30-09:30') + [dict(events()[0], suspend_timing=extra)]
    with pytest.raises(ValueError, match='not_applicable'):
        verified_suspension_proof(rows, [], [record()], code='603897.SH', stamp='20251104')


def test_zero_timing_requires_null_resumption_and_absent_bar():
    from src.services.screening.eligibility import verified_suspension_proof
    rows = events(timing='09:30-09:30')
    with pytest.raises(ValueError, match='not_applicable'):
        verified_suspension_proof(rows, [dict(ts_code='603897.SH', trade_date='20251104')],
                                  [record()], code='603897.SH', stamp='20251104')
    rows[1]['suspend_timing'] = ''
    with pytest.raises(ValueError, match='not_applicable'):
        verified_suspension_proof(rows, [], [record()], code='603897.SH', stamp='20251104')


def fake_status(code, stamp):
    return [dict(record(), code=code[-2:].lower() + '.' + code[:6], date=stamp.isoformat())]


def acquisition_client(missing=True, timing=None):
    from tests.test_screening_backtest_data import LegacyListingClient, LEGACY_MASTER
    class Client(LegacyListingClient):
        def query(self, endpoint, **params):
            table = super().query(endpoint, **params)
            if endpoint == 'stock_basic':
                return table.loc[table.ts_code != 'T600018.SH'].copy() if params['list_status'] == 'D' else table
            if endpoint in ('daily', 'daily_basic', 'adj_factor', 'stk_limit'):
                extra = table.copy()
                extra['ts_code'] = '600002.SH'
                if missing and params.get('trade_date') in {'20251104', '20260105'} and endpoint in ('daily', 'daily_basic'):
                    table = table.iloc[:0]
                return pd.concat([table, extra], ignore_index=True)
            if endpoint == 'suspend_d' and params.get('trade_date') in {'20251104', '20260105'}:
                return pd.DataFrame(events('600001.SH', params['trade_date'], timing))
            return table
    return Client(dict(LEGACY_MASTER))


@pytest.mark.parametrize('target', [date(2025, 11, 4), date(2026, 1, 5)])
@pytest.mark.parametrize('timing', [None, '09:30-09:30'])
def test_actual_acquire_and_pure_replay_preserve_dual_evidence_without_prices(tmp_path, target, timing):
    from tests.screening_reference_fixtures import reference_bundle
    from src.services.screening.backtest_data import Acquirer, BatchStore, acquire_market
    store = BatchStore(tmp_path / 'proof.sqlite')
    calls = []
    def fetch(code, stamp):
        calls.append((code, stamp))
        return fake_status(code, stamp)
    refs, client = reference_bundle(), acquisition_client(timing=timing)
    dataset = acquire_market(Acquirer(store, client, request_interval=0), refs, target, target, suspension_fetcher=fetch)
    assert calls == [('600001.SH', target)]
    assert store.date_status(target, refs.hash) == 'complete'
    assert dataset.execution('600001', target) == {'suspended': True}
    snapshot, coverage = dataset.snapshot(target)
    assert snapshot.empty
    exclusion = coverage['exclusions'][0]
    assert exclusion['reason'] == 'verified_full_day_suspension'
    proof = exclusion['suspension_evidence']['independent']
    assert proof['source'] == 'baostock.tradestatus' and proof['fetched_at']
    records, hashed, _ = store.batch('baostock_suspension', dict(ts_code='600001.SH', trade_date=target.strftime('%Y%m%d')))
    assert records == fake_status('600001.SH', target) and hashed == proof['evidence_id']
    assert all(r['ts_code'] != '600001.SH' for r in store.batch('daily', dict(trade_date=target.strftime('%Y%m%d')))[0])
    def forbidden(*args, **kwargs):
        pytest.fail('pure replay/resume must not fetch')
    client.query = forbidden
    acquire_market(Acquirer(store, client), refs, target, target, suspension_fetcher=forbidden)
    assert dataset.execution('600001', target) == {'suspended': True}
    store.close()


def test_invalid_attempt_is_not_accepted_and_retry_can_repair(tmp_path):
    from tests.screening_reference_fixtures import reference_bundle
    from src.services.screening.backtest_data import Acquirer, BatchStore, acquire_market, DataUnavailable
    store, target = BatchStore(tmp_path / 'retry.sqlite'), date(2025, 11, 4)
    refs, client = reference_bundle(), acquisition_client()
    with pytest.raises(ValueError, match='suspension_proof_not_applicable'):
        acquire_market(Acquirer(store, client, request_interval=0), refs, target, target, suspension_fetcher=lambda *a: [])
    with pytest.raises(DataUnavailable, match='batch_missing'):
        store.batch('baostock_suspension', dict(ts_code='600001.SH', trade_date='20251104'))
    assert store.db.execute("SELECT COUNT(*) FROM attempts WHERE raw LIKE '%baostock_suspension%'").fetchone()[0] == 1
    assert store.date_status(target, refs.hash) is None
    dataset = acquire_market(Acquirer(store, client, request_interval=0), refs, target, target, suspension_fetcher=fake_status)
    assert dataset.execution('600001', target) == {'suspended': True}
    store.close()


def test_acquisition_proof_respects_request_budget_and_preserves_staging(tmp_path):
    from tests.screening_reference_fixtures import reference_bundle
    from src.services.screening.backtest_data import Acquirer, BatchStore, acquire_market
    store = BatchStore(tmp_path / 'budget.sqlite')
    acquirer = Acquirer(store, acquisition_client(), request_interval=0, max_requests=9)
    with pytest.raises(ValueError, match='request_budget_exhausted_restart_safe'):
        acquire_market(acquirer, reference_bundle(), date(2025, 11, 4), date(2025, 11, 4),
                       suspension_fetcher=lambda *a: pytest.fail('over budget'))
    assert acquirer.requests == 9 and len(acquirer.staged) == 5
    assert store.date_status('20251104', reference_bundle().hash) is None
    assert store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 9
    store.close()


def test_present_quote_never_uses_independent_status_to_override_conflict(tmp_path):
    from tests.screening_reference_fixtures import reference_bundle
    from src.services.screening.backtest_data import Acquirer, BatchStore, acquire_market
    store = BatchStore(tmp_path / 'present.sqlite')
    with pytest.raises(ValueError, match='suspension_events_conflict'):
        acquire_market(Acquirer(store, acquisition_client(False), request_interval=0), reference_bundle(),
                       date(2026, 1, 5), date(2026, 1, 5),
                       suspension_fetcher=lambda *a: pytest.fail('present quote cannot be overridden'))
    store.close()


@pytest.mark.parametrize('timing', [None, '09:30-09:30'])
def test_actual_history_missing_session_requires_saved_proof(tmp_path, timing):
    from tests.test_screening_backtest_data import seeded_dataset
    from data_provider.historical_reference import digest
    dataset = seeded_dataset(tmp_path)
    store = dataset.store
    # Replace only synthetic fixture data to model the missing session.
    for endpoint in ('daily', 'suspend_d'):
        rows = [] if endpoint == 'daily' else events('600001.SH', timing=timing)
        payload = dict(endpoint=endpoint, params=dict(trade_date='20251104'), rows=rows)
        key = digest(dict(endpoint=endpoint, params=payload['params']))
        store.db.execute('DELETE FROM rows WHERE batch_key=?', (key,))
        store.db.execute('DELETE FROM batches WHERE key=?', (key,))
        store.put(endpoint, payload['params'], rows, '2026-01-05T08:00:00+00:00')
    with pytest.raises(ValueError, match='batch_missing:baostock_suspension'):
        dataset.history('600001', date(2026, 1, 5), 10.)
    store.put('baostock_suspension', dict(ts_code='600001.SH', trade_date='20251104'),
              fake_status('600001.SH', date(2025, 11, 4)), '2026-01-05T08:00:00+00:00')
    history = dataset.history('600001', date(2026, 1, 5), 10.)
    assert '2025-11-04' in history.attrs['suspension_proofs']
    assert not any(pd.Timestamp(v).date() == date(2025, 11, 4) for v in history['date'])
    store.db.execute("UPDATE batches SET raw='{}' WHERE endpoint='baostock_suspension'")
    with pytest.raises(ValueError, match='batch_hash_invalid'):
        dataset.history('600001', date(2026, 1, 5), 10.)
    store.close()


@pytest.mark.parametrize('timing', [None, '09:30-09:30'])
def test_actual_formal_market_history_resume_and_publisher_bind_all_originals(tmp_path, timing):
    from dataclasses import asdict
    from types import SimpleNamespace
    import json
    from pathlib import Path
    from copy import deepcopy
    from tests.test_research_delivery_live_collector import Client, LiveCollector, NOW, T
    from src.services.research_delivery.formal_publisher import FormalPublisher
    class Mixed(Client):
        def query(self, endpoint, **params):
            table = super().query(endpoint, **params)
            if endpoint in ('daily', 'daily_basic') and 'ts_code' not in params:
                return table.loc[table.ts_code != self.codes[0]].copy()
            if endpoint == 'daily' and params.get('ts_code') == self.codes[1]:
                return table.loc[table.trade_date != '20260901'].copy()
            if endpoint == 'suspend_d':
                if 'ts_code' not in params:
                    return pd.DataFrame(events(self.codes[0], '20261015', timing))
                if params['ts_code'] == self.codes[1]:
                    return pd.DataFrame(events(self.codes[1], '20260901', timing))
            return table
    client, calls = Mixed(20), []
    def fetch(code, stamp):
        calls.append((code, stamp))
        return fake_status(code, stamp)
    def run():
        return LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW, request_interval=0,
            batch_size=2, suspension_fetcher=fetch).collect(['dual_low'], T)['dual_low']
    result = run()
    assert result.coverage['scoring_eligibility']['600001']['reason'] == 'verified_full_day_suspension'
    assert result.coverage['pending_count'] > 0
    for _ in range(10):
        result = run()
        if result.coverage['pending_count'] == 0:
            break
    assert result.state == 'degraded' and not result.blockers
    assert calls == [('600001.SH', T), ('600002.SH', date(2026, 9, 1))]
    source = asdict(result)
    FormalPublisher._coverage(source)
    FormalPublisher._evidence(source, NOW)
    from src.services.research_delivery.collection_checkpoint import digest
    current_proof = source['coverage']['scoring_eligibility']['600001']['suspension_proof']
    historical_capture = next(json.loads(Path(e['path']).read_text(encoding='utf-8'))['payload']
        for e in result.evidence if e.get('dataset') == 'suspension_status'
        and json.loads(Path(e['path']).read_text(encoding='utf-8'))['payload']['requested_date'] == '2026-09-01')
    historical_id = next(e['sha256'] for e in result.evidence if e.get('dataset') == 'suspension_status'
        and json.loads(Path(e['path']).read_text(encoding='utf-8'))['payload']['requested_date'] == '2026-09-01')
    swapped = dict((k, v) for k, v in historical_capture.items() if k != 'records')
    swapped['evidence_id'] = historical_id
    mutations = [dict(swapped)] + [{k: v for k, v in current_proof.items() if k != omitted}
                                  for omitted in current_proof]
    for proof in mutations:
        damaged = deepcopy(source)
        value = damaged['coverage']['scoring_eligibility']['600001']
        value.update(suspension_proof=proof, evidence_id=digest(proof))
        with pytest.raises(Exception, match='evidence_invalid'):
            FormalPublisher._evidence(damaged, NOW)
    # Key association is independent of internal proof validity.
    for wrong_key in ('600002', '600003'):
        damaged = deepcopy(source)
        damaged['coverage']['scoring_eligibility'][wrong_key] = deepcopy(
            damaged['coverage']['scoring_eligibility'].pop('600001'))
        with pytest.raises(Exception, match='evidence_invalid'):
            FormalPublisher._evidence(damaged, NOW)
    # A valid same-security historical proof is not a current-day exclusion.
    damaged = deepcopy(source)
    value = damaged['coverage']['scoring_eligibility']['600002']
    value.update(eligible=False, reason='verified_full_day_suspension', suspension_proof=swapped, evidence_id=digest(swapped))
    damaged['coverage']['excluded'].append(dict(code='600002', reasons=['verified_full_day_suspension']))
    with pytest.raises(Exception, match='evidence_invalid'):
        FormalPublisher._evidence(damaged, NOW)
    for field, wrong in [('eligible', True), ('reason', 'excluded_st')]:
        damaged = deepcopy(source)
        damaged['coverage']['scoring_eligibility']['600001'][field] = wrong
        with pytest.raises(Exception, match='evidence_invalid'):
            FormalPublisher._evidence(damaged, NOW)
    damaged = deepcopy(source)
    damaged['coverage']['listing_eligibility']['600001']['exchange'] = 'SZSE'
    with pytest.raises(Exception, match='evidence_invalid'):
        FormalPublisher._evidence(damaged, NOW)
    entries = [r for r in result.evidence if r.get('dataset') == 'suspension_status']
    assert len(entries) == 2
    for entry in entries:
        capture = json.loads(Path(entry['path']).read_text(encoding='utf-8'))
        assert capture['business_date'] == T.isoformat()
        assert capture['payload']['requested_date'] in {'2026-10-15', '2026-09-01'}
        assert capture['payload']['records'][0]['tradestatus'] == '0'
        for parent in ('daily', 'primary'):
            assert capture['payload'][parent] in {r['sha256'] for r in result.evidence}
        damaged = deepcopy(source)
        damaged['evidence'] = [r for r in damaged['evidence'] if r['sha256'] != capture['payload']['primary']]
        with pytest.raises(Exception, match='evidence_invalid'):
            FormalPublisher._evidence(damaged, NOW)
    from src.services.research_delivery.evidence_store import EvidenceStore
    history_entry = next(e for e in entries if json.loads(Path(e['path']).read_text(encoding='utf-8'))['payload']['requested_date'] == '2026-09-01')
    original = json.loads(Path(history_entry['path']).read_text(encoding='utf-8'))['payload']
    # Even rehashed forged source claims cannot borrow a different-date parent.
    current = next(e for e in result.evidence if e.get('endpoint') == 'suspend_d' and 'trade_date' in e['params'])
    forged = EvidenceStore(tmp_path / 'forged').capture_payload('baostock', 'suspension_status',
        dict(original, primary=current['sha256']), fetched_at=NOW, business_date=T, provenance='provider_normalized')
    damaged = deepcopy(source)
    damaged['evidence'] = [forged if r['sha256'] == history_entry['sha256'] else r for r in damaged['evidence']]
    with pytest.raises(Exception, match='evidence_invalid'):
        FormalPublisher._evidence(damaged, NOW)
    count = len(client.calls)
    assert run().state == 'degraded' and len(client.calls) == count


@pytest.mark.parametrize('bad', [[], [dict(record(), tradestatus='1')]])
def test_formal_current_missing_unproven_status_stays_blocked(tmp_path, bad):
    from types import SimpleNamespace
    from tests.test_research_delivery_live_collector import Client, LiveCollector, NOW, T
    class Missing(Client):
        def query(self, endpoint, **params):
            table = super().query(endpoint, **params)
            if endpoint in ('daily', 'daily_basic') and 'ts_code' not in params:
                return table.loc[table.ts_code != self.codes[0]].copy()
            if endpoint == 'suspend_d':
                return pd.DataFrame(events(self.codes[0], '20261015'))
            return table
    result = LiveCollector(SimpleNamespace(), tmp_path, client=Missing(20), now=NOW, request_interval=0,
        suspension_fetcher=lambda *a: bad).collect(['dual_low'], T)['dual_low']
    assert result.state == 'blocked' and not result.result
    assert not any(e.get('dataset') == 'suspension_status' for e in result.evidence)


@pytest.mark.parametrize('count', [2, 20])
def test_formal_market_reuses_one_daily_record_list(tmp_path, monkeypatch, count):
    from types import SimpleNamespace
    from tests.test_research_delivery_live_collector import Client, ProductionCollector, NOW, T
    from tests.screening_reference_fixtures import reference_bundle
    seen, conversions = [], []
    real = pd.DataFrame.to_dict
    def tracked(frame, *args, **kwargs):
        if set(frame.columns) == {'ts_code', 'trade_date', 'close', 'pct_chg', 'amount'}:
            conversions.append(len(frame))
        return real(frame, *args, **kwargs)
    monkeypatch.setattr(pd.DataFrame, 'to_dict', tracked)
    class Collector(ProductionCollector):
        def _suspension(self, code, requested, target, events, quotes, parents, progress):
            seen.append(quotes)
            return super()._suspension(code, requested, target, events, quotes, parents, progress)
    collector = Collector(SimpleNamespace(), tmp_path, client=Client(count), now=NOW, request_interval=0,
                          reference_bundle=reference_bundle())
    assert len(collector._market(T, None, {})) == count
    assert len(seen) == count and all(rows is seen[0] for rows in seen)
    assert conversions == [count]


@pytest.mark.parametrize('timing,opening,closing', [
    ('9:30-9:40', True, False), ('09:30-09:40', True, False),
    ('10:02-10:12,9:49-9:59', False, False), ('13:00-9:30', False, True), ('13:00-09:30', False, True)])
def test_normalized_partial_real_acquire_snapshot_execution_and_formal(tmp_path, timing, opening, closing):
    from types import SimpleNamespace
    from dataclasses import asdict
    from pathlib import Path
    import json
    from tests.screening_reference_fixtures import reference_bundle
    from tests.test_research_delivery_live_collector import Client, LiveCollector, NOW, T
    from src.services.screening.backtest_data import Acquirer, BatchStore, acquire_market
    from src.services.research_delivery.formal_publisher import FormalPublisher
    def shaped(code, stamp):
        rows = events(code, stamp, timing)
        return rows[:1] if timing.startswith('13:00') else rows
    client = acquisition_client(False, timing)
    original_query = client.query
    def query(endpoint, **params):
        table = original_query(endpoint, **params)
        return pd.DataFrame(shaped('600001.SH', params['trade_date'])) if endpoint == 'suspend_d' else table
    client.query = query
    store = BatchStore(tmp_path / 'partial.sqlite')
    dataset = acquire_market(Acquirer(store, client, request_interval=0), reference_bundle(),
        date(2026, 1, 5), date(2026, 1, 5), suspension_fetcher=lambda *a: pytest.fail('partial is not a supplement'))
    assert dataset.snapshot(date(2026, 1, 5))[0].code.tolist() == ['600001']
    state = dataset.execution('600001', date(2026, 1, 5))
    assert state['suspended_open'] is opening and state['suspended_close'] is closing
    assert state['open'] == 10. and state['close'] == 10. and not state['suspended']
    assert store.batch('suspend_d', dict(trade_date='20260105'))[0][0]['suspend_timing'] == timing
    # Next session is independently unresolved, even for a predicted opening end.
    store.put('suspend_d', dict(trade_date='20260106'), events('600001.SH', '20260106'), '2026-01-06T08:00:00+00:00')
    store.put('daily', dict(trade_date='20260106'), [dict(ts_code='600001.SH', trade_date='20260106', close=10.)], '2026-01-06T08:00:00+00:00')
    with pytest.raises(ValueError, match='suspension_events_conflict'):
        dataset.execution('600001', date(2026, 1, 6))
    store.close()
    class FormalClient(Client):
        def query(self, endpoint, **params):
            table = super().query(endpoint, **params)
            return pd.DataFrame(shaped(self.codes[0], '20261015')) if endpoint == 'suspend_d' else table
    result = LiveCollector(SimpleNamespace(), tmp_path / 'formal', client=FormalClient(), now=NOW,
        request_interval=0, suspension_fetcher=lambda *a: pytest.fail('observed bar cannot supplement')).collect(['dual_low'], T)['dual_low']
    assert result.state == 'degraded' and not result.blockers
    FormalPublisher._coverage(asdict(result))
    FormalPublisher._evidence(asdict(result), NOW)
    captured = next(e for e in result.evidence if e.get('endpoint') == 'suspend_d')
    raw = json.loads(Path(captured['path']).read_text(encoding='utf-8'))
    assert timing in [row[raw['columns'].index('suspend_timing')] for row in raw['rows']]


@pytest.mark.parametrize('timing', ['9:30-9:40', '13:00-9:30'])
def test_normalized_partial_never_explains_missing_acquire_or_formal_history(tmp_path, timing):
    from types import SimpleNamespace
    from tests.screening_reference_fixtures import reference_bundle
    from tests.test_research_delivery_live_collector import HistoricalGapClient, LiveCollector, NOW, T
    from src.services.screening.backtest_data import Acquirer, BatchStore, acquire_market
    from src.services.research_delivery.formal_publisher import FormalPublisher
    from dataclasses import asdict
    client = acquisition_client(True, timing)
    query = client.query
    def only_s(endpoint, **params):
        table = query(endpoint, **params)
        return table.iloc[:1] if endpoint == 'suspend_d' else table
    client.query = only_s
    store = BatchStore(tmp_path / 'missing.sqlite')
    with pytest.raises(ValueError, match='required_market_row_missing'):
        acquire_market(Acquirer(store, client, request_interval=0), reference_bundle(), date(2026, 1, 5), date(2026, 1, 5),
                       suspension_fetcher=lambda *a: pytest.fail('partial cannot supplement'))
    assert store.date_status('20260105', reference_bundle().hash) is None
    store.close()
    result = LiveCollector(SimpleNamespace(), tmp_path / 'formal', client=HistoricalGapClient(timing), now=NOW,
        request_interval=0, suspension_fetcher=lambda *a: pytest.fail('partial cannot supplement')).collect(['dual_low'], T)['dual_low']
    assert result.state == 'blocked'
    assert {f['reason'] for f in result.coverage['history_failures']} == {'unexplained_historical_bar_gap'}
    with pytest.raises(Exception, match='coverage_invalid'):
        FormalPublisher._coverage(asdict(result))
