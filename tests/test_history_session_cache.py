"""Bounded same-date replay reuse; original history/preview guards stay active."""
from datetime import date, datetime, timezone
from dataclasses import asdict
from copy import deepcopy
import json
import sqlite3
import pytest


def test_same_date_success_reuses_assembly_and_isolates_returned_frame(tmp_path, monkeypatch):
    from tests.test_screening_backtest_data import seeded_dataset
    dataset = seeded_dataset(tmp_path)
    snapshot, _ = dataset.snapshot(date(2026, 1, 5))
    calls, original = [], dataset.history
    def tracked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(dataset, 'history', tracked)
    histories = dataset.histories(snapshot, date(2026, 1, 5))
    first = histories['600001']
    first.iloc[0, first.columns.get_loc('close')] = 999.
    first.attrs['suspension_proofs']['poison'] = {'nested': True}
    second = histories['600001']
    assert second.iloc[0]['close'] == 10. and 'poison' not in second.attrs['suspension_proofs']
    assert len(calls) == 1
    dataset.store.artifact('derived', {'ok': True})
    histories['600001']
    assert len(calls) == 1
    dataset.store.close()


@pytest.mark.parametrize('text,expected', [('20240229', date(2024, 2, 29)), ('2024-02-29', date(2024, 2, 29))])
def test_bounded_day_parser_reuses_only_exact_strings(text, expected):
    from data_provider.historical_reference import day, _parse_day_string
    _parse_day_string.cache_clear()
    assert day(text) == day(text) == expected
    assert _parse_day_string.cache_info().hits == 1


@pytest.mark.parametrize('timing,opening,closing', [('9:30-9:40', True, False),
    ('10:02-10:12,9:49-9:59', False, False), ('13:00-9:30', False, True)])
def test_timing_width_and_next_open_continuation(timing, opening, closing):
    from src.services.screening.eligibility import suspension_state
    assert suspension_state([dict(suspend_type='S', suspend_timing=timing)]) == dict(
        status='partial', suspended_open=opening, suspended_close=closing)


@pytest.mark.parametrize('finish', ['rollback', 'savepoint', 'commit'])
def test_active_transaction_never_retains_history_or_derived_caches(tmp_path, monkeypatch, finish):
    from tests.test_screening_backtest_data import seeded_dataset
    from data_provider.historical_reference import digest
    dataset = seeded_dataset(tmp_path)
    snapshot, _ = dataset.snapshot(date(2026, 1, 5))
    original, calls = dataset.history, []
    def tracked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(dataset, 'history', tracked)
    histories = dataset.histories(snapshot, date(2026, 1, 5))
    def volume():
        frame = histories['600001']
        return frame.loc[frame.date.dt.strftime('%Y%m%d') == '20251104', 'volume'].iloc[0]
    assert volume() == 1000000.
    db = dataset.store.db
    db.execute('BEGIN')
    if finish == 'savepoint':
        db.execute('SAVEPOINT source_edit')
    rowid, raw = db.execute("SELECT rowid,raw FROM rows WHERE endpoint='daily' AND stamp='20251104'").fetchone()
    record = json.loads(raw)
    record['vol'] = 20000.
    db.execute('UPDATE rows SET raw=?,row_sha=? WHERE rowid=?', (json.dumps(record), digest(record), rowid))
    assert volume() == 2000000.
    assert histories.used_bytes == 0 and not histories._entries
    assert dataset._masters is None and dataset._names is None
    assert not dataset.references._calendar_cache
    if finish == 'savepoint':
        db.execute('ROLLBACK TO source_edit')
        assert db.in_transaction
        assert volume() == 1000000.
        assert histories.used_bytes == 0
        db.execute('RELEASE source_edit')
        db.commit()
    elif finish == 'rollback':
        db.rollback()
    else:
        db.commit()
    expected = 2000000. if finish == 'commit' else 1000000.
    assert volume() == expected
    assembled = len(calls)
    assert volume() == expected and len(calls) == assembled
    dataset.store.artifact('post-transaction', {'ok': True})
    assert volume() == expected and len(calls) == assembled
    dataset.store.close()


@pytest.mark.parametrize('external', [False, True])
def test_source_mutation_invalidates_same_date_cache(tmp_path, monkeypatch, external):
    from tests.test_screening_backtest_data import seeded_dataset
    from data_provider.historical_reference import digest
    dataset = seeded_dataset(tmp_path)
    snapshot, _ = dataset.snapshot(date(2026, 1, 5))
    calls, original = [], dataset.history
    def tracked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(dataset, 'history', tracked)
    histories = dataset.histories(snapshot, date(2026, 1, 5))
    histories['600001']
    writer = sqlite3.connect(dataset.store.path) if external else dataset.store.db
    rowid, raw = writer.execute("SELECT rowid,raw FROM rows WHERE endpoint='daily' AND stamp='20251104'").fetchone()
    record = json.loads(raw)
    record['vol'] = 20000.
    writer.execute('UPDATE rows SET raw=?,row_sha=? WHERE rowid=?', (json.dumps(record), digest(record), rowid))
    writer.commit()
    updated = histories['600001']
    assert len(calls) == 2
    assert updated.loc[updated.date.dt.strftime('%Y%m%d') == '20251104', 'volume'].iloc[0] == 2000000.
    writer.execute("UPDATE rows SET raw='{}' WHERE rowid=?", (rowid,))
    writer.commit()
    with pytest.raises(ValueError, match='indexed_row_hash_invalid'):
        histories['600001']
    assert len(calls) == 3
    if external:
        writer.close()
    dataset.store.close()


def test_reference_change_failed_repair_and_read_race(tmp_path, monkeypatch):
    from tests.test_screening_backtest_data import seeded_dataset
    dataset = seeded_dataset(tmp_path)
    snapshot, _ = dataset.snapshot(date(2026, 1, 5))
    original, calls = dataset.history, []
    def tracked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(dataset, 'history', tracked)
    histories = dataset.histories(snapshot, date(2026, 1, 5))
    histories['600001']
    dataset.references.hash = 'changed-reference'
    histories['600001']
    assert len(calls) == 2
    dataset.references.aliases['600099'] = '600001'
    histories['600001']
    assert len(calls) == 3
    # No negative cache: failure then recovery uses the actual original path.
    failing = dataset.histories(snapshot, date(2026, 1, 5))
    monkeypatch.setattr(dataset, 'history', lambda *a: (_ for _ in ()).throw(ValueError('synthetic transient')))
    with pytest.raises(ValueError, match='transient'):
        failing['600001']
    monkeypatch.setattr(dataset, 'history', tracked)
    assert not failing['600001'].empty
    def racing(*args):
        frame = original(*args)
        dataset.store.db.execute("UPDATE date_state SET state=state WHERE stamp='20260105'")
        return frame
    monkeypatch.setattr(dataset, 'history', racing)
    with pytest.raises(ValueError, match='history_source_changed_during_read'):
        dataset.histories(snapshot, date(2026, 1, 5))['600001']
    dataset.store.close()


def test_eviction_budget_date_and_exact_price_do_not_reuse_wrong_context(tmp_path, monkeypatch):
    from tests.test_screening_backtest_data import seeded_dataset
    dataset = seeded_dataset(tmp_path)
    signal = date(2026, 1, 5)
    snapshot, _ = dataset.snapshot(signal)
    calls, original = [], dataset.history
    def tracked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(dataset, 'history', tracked)
    disabled = dataset.histories(snapshot, signal, max_bytes=0)
    disabled['600001']; disabled['600001']
    assert len(calls) == 2 and disabled.used_bytes == 0
    cached = dataset.histories(snapshot, signal)
    cached['600001']; cached['600001']
    assert len(calls) == 3 and 0 < cached.used_bytes <= cached.max_bytes
    next_snapshot, _ = dataset.snapshot(date(2026, 1, 6))
    assert dataset.histories(next_snapshot, date(2026, 1, 6))['600001'].date.iloc[-1].date() == date(2026, 1, 6)
    wrong = snapshot.copy()
    wrong['price'] = 100.
    with pytest.raises(ValueError, match='price scale'):
        dataset.histories(wrong, signal)['600001']
    dataset.store.close()


def test_actual_two_strategy_preview_cache_matches_uncached(tmp_path, monkeypatch):
    from tests.test_screening_backtest_data import seeded_dataset
    from src.services.research_delivery.screening_preview import preview_strategy
    from src.services.screening import models
    class FrozenClock(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 1, 5, 18)
    monkeypatch.setattr(models, 'datetime', FrozenClock)
    dataset = seeded_dataset(tmp_path)
    signal = date(2026, 1, 5)
    snapshot, _ = dataset.snapshot(signal)
    original, calls = dataset.history, []
    def tracked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(dataset, 'history', tracked)
    def run(limit):
        histories = dataset.histories(snapshot, signal, max_bytes=limit)
        features = {}
        return [asdict(preview_strategy(strategy, snapshot, trade_date=signal, universe=tuple(snapshot.code),
            calendar_verified=True, universe_verified=True, daily_histories=histories, feature_cache=features))
            for strategy in ('dual_low', 'balanced_alpha')]
    uncached = run(0)
    uncached_calls = len(calls)
    calls.clear()
    cached = run(128 * 1024 * 1024)
    for result in [*cached, *uncached]:
        result['result'].pop('run_id')  # Per-invocation UUID, not a computed result.
    assert cached == uncached and uncached_calls == 2 and len(calls) == 1
    dataset.store.close()


def test_date_caches_preserve_errors_type_timezones_and_finite_bound():
    from datetime import timedelta
    from data_provider.historical_reference import day, _parse_day_string
    from src.services.research_delivery.evidence import business_date, _parse_business_date_string
    for value in ('20230229', '2026-13-01', '', '20260101 extra'):
        with pytest.raises(ValueError):
            day(value)
        assert business_date(value) is None
    for value in (datetime(2026, 1, 1, 1), datetime(2026, 1, 1, tzinfo=timezone.utc)):
        with pytest.raises(ValueError):
            day(value)
        assert business_date(value) is None
    assert business_date(datetime(2026, 1, 1)) == date(2026, 1, 1)
    assert day(date(2026, 1, 1)) == business_date(date(2026, 1, 1)) == date(2026, 1, 1)
    _parse_day_string.cache_clear(); _parse_business_date_string.cache_clear()
    for offset in range(4100):
        text = (date(2000, 1, 1) + timedelta(days=offset)).isoformat()
        assert day(text) == business_date(text)
    assert _parse_day_string.cache_info().currsize == _parse_business_date_string.cache_info().currsize == 4096
    before = _parse_day_string.cache_info().misses
    assert day('2000-01-01') == date(2000, 1, 1)
    assert _parse_day_string.cache_info().misses == before + 1


def test_actual_fixed_matrix_cached_and_uncached_outputs_match(tmp_path, monkeypatch):
    from tests.test_screening_backtest_data import seeded_dataset
    from src.services.screening.backtest import run_backtest
    dataset = seeded_dataset(tmp_path)
    histories = dataset.histories
    calls, original = [], dataset.history
    def tracked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(dataset, 'history', tracked)
    monkeypatch.setattr(dataset, 'histories', lambda snapshot, signal: histories(snapshot, signal, max_bytes=0))
    before = run_backtest(dict(output_dir=str(tmp_path / 'uncached'), acquisition_end='2026-01-06'), dataset)
    no_cache_calls = len(calls)
    calls.clear()
    monkeypatch.setattr(dataset, 'histories', histories)
    # Different output/config identity forces real calculation, not artifact reuse.
    after = run_backtest(dict(output_dir=str(tmp_path / 'cached'), acquisition_end='2026-01-06'), dataset)
    assert after == before and after['planned_cells'] == 320
    assert len(calls) == len(set((args[0], args[1]) for args in calls)) == 2
    assert no_cache_calls > len(calls)
    for filename in ('signals.csv', 'trades.csv', 'metrics.json', 'data_quality.json', 'report.md'):
        assert (tmp_path / 'cached' / filename).read_bytes() == (tmp_path / 'uncached' / filename).read_bytes()
    dataset.store.close()


def test_lru_eviction_and_generation_change_during_hit(tmp_path, monkeypatch):
    import pandas as pd
    from tests.test_screening_backtest_data import seeded_dataset
    dataset = seeded_dataset(tmp_path)
    snapshot, _ = dataset.snapshot(date(2026, 1, 5))
    original, calls = dataset.history, []
    def container_fixture(code, signal, price):
        calls.append(code)
        frame = original('600001', signal, price)
        frame.attrs['code'] = code
        return frame
    monkeypatch.setattr(dataset, 'history', container_fixture)
    # Cache-container unit check; selection equivalence above uses real securities.
    inputs = pd.concat([snapshot, snapshot.assign(code='600002')], ignore_index=True)
    sized = dataset.histories(inputs, date(2026, 1, 5))
    sized['600001']
    bounded = dataset.histories(inputs, date(2026, 1, 5), max_bytes=sized.used_bytes)
    calls.clear()
    for code in ('600001', '600002', '600001'):
        bounded[code]
        assert bounded.used_bytes <= bounded.max_bytes
    assert calls == ['600001', '600002', '600001']
    real_copy = bounded._copy
    def mutate_on_copy(frame):
        result = real_copy(frame)
        dataset.store.db.execute("UPDATE date_state SET state=state WHERE stamp='20260105'")
        return result
    monkeypatch.setattr(bounded, '_copy', mutate_on_copy)
    with pytest.raises(ValueError, match='history_source_changed_during_read'):
        bounded['600001']
    assert bounded.used_bytes == 0
    dataset.store.close()


def test_artifact_trigger_source_writes_are_not_discounted(tmp_path):
    from tests.test_screening_backtest_data import seeded_dataset
    dataset = seeded_dataset(tmp_path)
    store = dataset.store
    store.db.execute("CREATE TRIGGER source_touch AFTER INSERT ON artifacts BEGIN UPDATE date_state SET state=state WHERE stamp='20260105'; END")
    before = store.source_generation()
    store.artifact('triggered', {'value': 1})
    assert store.source_generation() != before
    store.close()


@pytest.mark.parametrize('timing', ['9:3-9:40', '24:00-9:30', '9:60-10:00', '９:30-10:00',
    '09:30-09:30', '9:30-9:30', '14:00-10:00', '13:00-9:29', '12:00-9:30',
    '11:30-9:30', '15:00-9:30', '13:00-9:30,14:00-15:00'])
def test_invalid_or_unsupported_timing_remains_unknown(timing):
    from src.services.screening.eligibility import suspension_state
    with pytest.raises(ValueError, match='suspension_timing_unknown'):
        suspension_state([dict(suspend_type='S', suspend_timing=timing)])


@pytest.mark.parametrize('extra', [dict(suspend_type='R', suspend_timing=None),
    dict(suspend_type='S', suspend_timing='14:00-9:30'), dict(suspend_type='S', suspend_timing='14:00-15:00')])
def test_continuation_cannot_mix_other_event_semantics(extra):
    from src.services.screening.eligibility import suspension_state
    with pytest.raises(ValueError, match='suspension_timing_unknown'):
        suspension_state([dict(suspend_type='S', suspend_timing='13:00-9:30'), extra])


@pytest.mark.parametrize('code', ['600001.SH', '000001.SZ', '920001.BJ'])
def test_interval_order_duplicates_width_and_exchange_do_not_change_state(code):
    from src.services.screening.eligibility import suspension_state
    rows = [dict(ts_code=code, suspend_type='S', suspend_timing='10:02-10:12,9:49-9:59'),
            dict(ts_code=code, suspend_type='R', suspend_timing=None)]
    expected = dict(status='partial', suspended_open=False, suspended_close=False)
    assert suspension_state(rows) == expected
    rows[0]['suspend_timing'] = '09:49-09:59,10:02-10:12,09:49-09:59'
    assert suspension_state(rows[::-1]) == expected
