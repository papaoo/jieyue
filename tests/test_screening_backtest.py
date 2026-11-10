"""Deterministic retrospective semantics; no provider or realized return inputs."""
from datetime import date

import pandas as pd
import pytest

from src.services.screening.daily import _compute_rsi
from src.services.screening.models import ScreeningConfig
from src.services.screening.requirements import required_daily_fields


@pytest.mark.parametrize('values,expected', [
    (list(range(1, 16)), 100.), (list(range(15, 0, -1)), 0.),
    ([10.] * 15, 50.), ([10., 12., 11.] * 5, 100. * 10. / 19.),
    ([10.] * 14, None),
])
def test_sma_rsi_hand_calculated(values, expected):
    value = _compute_rsi(pd.Series(values))
    assert value is None if expected is None else value == pytest.approx(expected)


def test_strict_rsi_is_required():
    assert 'rsi14' in required_daily_fields(ScreeningConfig(evidence_mode='daily_required'))
    assert 'rsi14' not in required_daily_fields(ScreeningConfig())


def test_strict_unknown_rsi_is_rejected():
    from src.services.screening.pipeline import _merge_frozen_daily_features
    from tests.test_screening_strategy_integrity import frozen_inputs
    snapshot, features = frozen_inputs()
    features['rsi14'] = None
    with pytest.raises((ValueError, TypeError)):
        _merge_frozen_daily_features(snapshot, features, screening=ScreeningConfig(evidence_mode='daily_required'))


def test_technical_window_prefix_invariance_and_future_rejection():
    from src.services.screening.daily import strict_history_window, strict_history_start, compute_daily_features
    signal = date(2026, 10, 16)
    dates = pd.date_range('2025-01-01', signal, freq='B')
    frame = pd.DataFrame(dict(date=dates, close=range(10, len(dates) + 10), volume=100.))
    frame['open'] = frame['close']
    frame['high'] = frame['close'] + 1
    frame['low'] = frame['close'] - 1
    trimmed = strict_history_window(frame, signal)
    changed = frame.copy()
    changed.loc[changed.date.dt.date < strict_history_start(signal), 'close'] = 1e6
    assert compute_daily_features(trimmed) == compute_daily_features(strict_history_window(changed, signal))
    with pytest.raises(ValueError, match='chronology'):
        strict_history_window(frame.iloc[::-1], signal)
    with pytest.raises(ValueError, match='chronology'):
        strict_history_window(frame, date(2026, 10, 15))


class ExecutionDataset:
    def __init__(self):
        self.days = [date(2026, 1, d) for d in (5, 6, 7, 8, 9, 12)]
        self.bars = {d: dict(open=10., high=11., low=9., close=10., adj_factor=1.,
            suspended=False, up_limit=11., down_limit=9., no_limit=False) for d in self.days}
        self.benchmarks = {d: dict(open=100., close=101.) for d in self.days}
    def masters(self): return {'600001': dict(ts_code='600001.SH')}
    def sessions(self, exchange, start, end): return [d for d in self.days if start <= d <= end]
    def execution(self, code, session): return self.bars[session]
    def benchmark(self, session, field): return self.benchmarks[session][field]


@pytest.mark.parametrize('exit_price,factor,gross', [(10., 1., 1.), (5., 2., 1.), (9., 1.2, 1.08)])
def test_adjustment_and_fixed_friction(exit_price, factor, gross):
    from src.services.screening.backtest import evaluate_slot
    dataset = ExecutionDataset()
    dataset.bars[dataset.days[3]].update(close=exit_price, low=4., down_limit=3., adj_factor=factor)
    slot = evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[-1])
    assert slot['entry_date'] == '2026-01-06' and slot['exit_date'] == '2026-01-08'
    assert slot['net_return'] == pytest.approx(gross * (1 - .0013) / (1 + .0008) - 1)
    assert slot['double_friction_return'] == pytest.approx(gross * (1 - .0026) / (1 + .0016) - 1)
    assert slot['benchmark_return'] == pytest.approx(.01)


def test_absent_entry_never_shifts_and_missing_middle_bar_does_not_shift_exit():
    from src.services.screening.backtest import evaluate_slot
    dataset = ExecutionDataset()
    del dataset.bars[dataset.days[1]]
    assert evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[-1])['status'] == 'incomplete'
    dataset = ExecutionDataset()
    del dataset.bars[dataset.days[2]]
    assert evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[-1])['exit_date'] == '2026-01-08'


def test_known_blocked_entry_is_cash_and_unknown_limits_are_not_unlimited():
    from src.services.screening.backtest import evaluate_slot, cohort
    dataset = ExecutionDataset()
    dataset.bars[dataset.days[1]] = dict(suspended=True)
    slot = evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[-1])
    assert slot['status'] == 'blocked_entry_cash' and cohort([slot])['net_return'] == 0
    dataset = ExecutionDataset()
    dataset.bars[dataset.days[1]]['up_limit'] = None
    assert evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[-1])['net_return'] is None
    dataset.bars[dataset.days[1]].update(no_limit=True, no_limit_evidence_id='verified_notice')
    assert evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[-1])['status'] == 'filled'


def test_limit_locked_exit_delays_but_unknown_exit_never_forward_fills():
    from src.services.screening.backtest import evaluate_slot
    dataset = ExecutionDataset()
    dataset.bars[dataset.days[3]]['close'] = 9.
    assert evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[-1])['exit_date'] == '2026-01-09'
    del dataset.bars[dataset.days[3]]
    assert evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[-1])['net_return'] is None
    dataset.bars[dataset.days[3]] = dict(suspended=True)
    assert evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[3])['reason'] == 'blocked_exit_unresolved_at_cutoff'
    with pytest.raises(ValueError, match='T_plus_1'):
        evaluate_slot(dataset, '600001', dataset.days[0], 1, dataset.days[-1])


def test_missing_factor_and_benchmark_have_distinct_null_effects():
    from src.services.screening.backtest import evaluate_slot
    dataset = ExecutionDataset()
    dataset.benchmarks = {}
    slot = evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[-1])
    assert slot['net_return'] is not None and slot['benchmark_return'] is None
    dataset.bars[dataset.days[3]]['adj_factor'] = None
    assert evaluate_slot(dataset, '600001', dataset.days[0], 3, dataset.days[-1])['net_return'] is None


@pytest.mark.parametrize('unknown_net', [-.02, .02])
def test_category_requires_benchmark_for_every_completed_cell(unknown_net):
    from src.services.screening.backtest import evaluate_category
    known = dict(status='completed', mean_net_return=.02, mean_benchmark_difference=.01)
    unknown = dict(status='completed', mean_net_return=unknown_net, mean_benchmark_difference=None)
    assert evaluate_category([known, unknown]) == 'insufficient evidence'
    assert unknown['mean_benchmark_difference'] is None


@pytest.mark.parametrize('returns,expected', [
    ([(.02, .01), (.03, .02)], 'positive in completed windows'),
    ([(-.02, -.01), (-.03, -.02)], 'negative in completed windows'),
    ([(.02, .01), (-.03, -.02)], 'mixed across completed windows'),
    ([], 'insufficient evidence'),
])
def test_category_preserves_four_full_coverage_outcomes(returns, expected):
    from src.services.screening.backtest import evaluate_category
    cells = [dict(status='completed', mean_net_return=net, mean_benchmark_difference=excess)
             for net, excess in returns]
    assert evaluate_category(cells) == expected


def test_full_fixed_matrix_retains_future_windows_and_no_signal(tmp_path):
    from src.services.screening.backtest import run_backtest
    from src.services.screening.backtest_data import BatchStore
    from tests.test_screening_backtest_data import bundle
    class EmptyDataset:
        fingerprint = 'synthetic-fixed-dataset'
        last_available_date = date(2026, 10, 16)
        references = bundle()
        store = BatchStore(tmp_path / 'empty.sqlite')
        def sessions(self, exchange, start, end):
            calendar, _ = self.references.calendar(exchange, start, end)
            return sorted(d for d, opened in calendar.items() if opened)
        def snapshot(self, signal):
            return pd.DataFrame(), dict(universe_count=1, exclusions=[dict(code='600001', reason='verified_ST')])
    dataset = EmptyDataset()
    result = run_backtest(dict(output_dir=str(tmp_path / 'run')), dataset)
    assert result['planned_cells'] == 320 and result['completed_cells'] == 280
    assert {c['status'] for c in result['cells']} == {'no-signal', 'incomplete'}
    assert all(c['mean_net_return'] is None for c in result['cells'] if c['status'] == 'incomplete')
    assert len(list((tmp_path / 'run').iterdir())) == 7
    dataset.store.close()


def test_real_strategy_replay_artifacts_deduplicate_exposure_and_resume(tmp_path, monkeypatch):
    import csv
    from src.services.screening.backtest import run_backtest
    from tests.test_screening_backtest_data import seeded_dataset
    from tests.test_screening_compute_only import forbid_remote
    dataset = seeded_dataset(tmp_path)
    forbid_remote(monkeypatch)
    config = dict(output_dir=str(tmp_path / 'real-run'), acquisition_end='2026-01-09')
    result = run_backtest(config, dataset)
    with (tmp_path / 'real-run/signals.csv').open(encoding='utf-8-sig', newline='') as stream:
        signals = list(csv.DictReader(stream))
    dual = [row for row in signals if row['strategy'] == 'dual_low']
    assert dual and all(row['code'] == '600001' and row['signal_date'] <= '2026-01-09' for row in dual)
    assert result['industry_exposure']['dual_low']['counts'] == {'capco_major_v1:J66': len(dual)}
    assert result['planned_cells'] == 320 and result['completed_cells'] == 0
    assert all(cell['mean_net_return'] is None for cell in result['cells'])
    assert run_backtest(config, dataset) == result
    dataset.store.close()


def test_unknown_calendar_retains_all_unavailable_cells(tmp_path):
    from src.services.screening.backtest import run_backtest
    from src.services.screening.backtest_data import BatchStore
    from tests.test_screening_backtest_data import bundle
    class MissingCalendar:
        fingerprint = 'missing-calendar-fixture'
        last_available_date = date(2026, 10, 16)
        references = bundle()
        store = BatchStore(tmp_path / 'missing.sqlite')
        def sessions(self, *args):
            raise ValueError('calendar_unavailable')
    dataset = MissingCalendar()
    result = run_backtest(dict(output_dir=str(tmp_path / 'missing-run')), dataset)
    assert result['planned_cells'] == 320 and result['completed_cells'] == 0
    assert {cell['status'] for cell in result['cells']} == {'unavailable'}
    assert all(cell['scheduled_cohorts'] is None for cell in result['cells'])
    dataset.store.close()


@pytest.mark.parametrize('dependency', ['scorer.py', 'config.py', 'models.py', 'normalize.py', 'selection_variant.py'])
def test_pure_dependency_change_rejects_same_run_resume(tmp_path, monkeypatch, dependency):
    from pathlib import Path
    from src.services.screening.backtest import freeze_config
    config = dict(output_dir=str(tmp_path / 'frozen'))
    frozen, _ = freeze_config(config)
    key = 'src/services/screening/' + dependency
    assert key in frozen['implementation_hashes']
    original = Path.read_bytes
    def changed(path):
        value = original(path)
        return value + b'\n# synthetic source change\n' if path.as_posix().endswith(key) else value
    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError, match='immutable_run_config_conflict'):
        freeze_config(config)
