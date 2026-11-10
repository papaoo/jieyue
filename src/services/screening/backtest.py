"""Fixed descriptive cohorts using the real frozen strategy implementation.

Pure with respect to environment/network/publishing. The explicit output
directory receives the frozen configuration before any data are evaluated.
"""
from collections import Counter
from dataclasses import asdict
from datetime import date
import csv
import json
import math
from pathlib import Path
from statistics import mean, median

from data_provider.historical_reference import day, digest
from src.services.research_delivery.screening_preview import preview_strategy
from src.services.screening.backtest_data import DataUnavailable, EXCHANGES
from src.services.screening.config import Config
from src.services.screening.strategy import load_all_strategies

SEMANTICS = 'retrospective-capco-sma-rsi-300d-v1'
FIXED = dict(start_dates=['2026-01-05', '2026-04-01', '2026-07-01', '2026-09-01'],
             window_months=[1, 3], holding_sessions=[3, 5, 10, 20], top_n=5,
             commission_bps=3, slippage_bps=5, sell_stamp_tax_bps=5,
             history_calendar_days=300, minimum_warmup_sessions=120)


def add_months(start, months):
    start = day(start)
    index = start.year * 12 + start.month - 1 + months
    return date(index // 12, index % 12 + 1, start.day)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False), encoding='utf-8')


def freeze_config(config):
    """Freeze rules/costs/category definitions before acquisition or returns."""
    allowed = set(FIXED) | {'output_dir', 'reference_bundle', 'dataset_path', 'acquisition_end', 'reference_plan', 'execution_workers'}
    if set(config) - allowed:
        raise ValueError('backtest_config_unknown_fields')
    if 'execution_workers' in config and (type(config['execution_workers']) is not int or not 1 <= config['execution_workers'] <= 6):
        raise ValueError('execution_workers_invalid')
    for key, value in FIXED.items():
        if key in config and config[key] != value:
            raise ValueError('fixed_matrix_or_costs_changed:' + key)
    runtime = Config()
    strategies = load_all_strategies(runtime.strategies_dir)
    from hashlib import sha256
    from importlib.metadata import version
    import sys
    root = Path(__file__).resolve().parents[3]
    implementation_paths = [
        'data_provider/historical_reference.py', 'data_provider/historical_fundamentals.py',
        *['src/services/screening/' + name + '.py' for name in
          ('backtest', 'backtest_data', 'backtest_parallel', 'daily', 'eligibility', 'industry_reference', 'requirements',
           'pipeline', 'filter', 'ranker', 'risk', 'post_analysis', 'strategy', 'snapshot',
           'scorer', 'config', 'models', 'normalize', 'selection_variant')],
        'src/services/research_delivery/screening_preview.py', 'src/services/research_delivery/evidence.py',
        'src/services/research_delivery/contracts.py', 'src/services/research_delivery/dependencies.py',
        'src/services/research_delivery/readiness.py', 'src/services/research_delivery/collection_checkpoint.py',
        'src/services/research_delivery/live_contracts.py', 'src/config.py']
    frozen = dict(FIXED, **config, semantics=SEMANTICS,
        implementation_hashes={name: sha256((root / name).read_bytes()).hexdigest() for name in implementation_paths},
        calculation_runtime=dict(python=sys.version, **{name: version(name) for name in ('pandas', 'numpy', 'PyYAML')}),
        strategies={key: asdict(value) for key, value in sorted(strategies.items())},
        evaluation_categories=['insufficient evidence', 'positive in completed windows',
                               'negative in completed windows', 'mixed across completed windows'],
        execution='after_close_D_to_exact_D_plus_1_open;exit_entry_index_plus_H_minus_1_close',
        benchmark='CSI300 price index; executed-slot matched endpoints; not dividend-matched alpha',
        weighting='five fixed 20% fractional slots; no lots/minimum commission',
        sensitivity='double total buy/sell friction with identical selections/fills')
    output = Path(config['output_dir'])
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'config.json'
    if path.exists() and json.loads(path.read_text(encoding='utf-8')) != frozen:
        raise ValueError('immutable_run_config_conflict_use_new_output_directory')
    if not path.exists():
        write_json(path, frozen)
    return frozen, runtime


def positive(value):
    if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
        raise DataUnavailable('execution_numeric_invalid')
    return float(value)


def execution_bar(value):
    if type(value.get('suspended')) is not bool:
        raise DataUnavailable('suspension_state_unknown')
    if value['suspended']:
        return value
    result = dict(value)
    for field in ('open', 'high', 'low', 'close', 'adj_factor'):
        result[field] = positive(value[field])
    if result['low'] > min(result['open'], result['close']) or result['high'] < max(result['open'], result['close']):
        raise DataUnavailable('execution_ohlc_invalid')
    if type(value.get('no_limit')) is not bool:
        raise DataUnavailable('limit_state_unknown')
    if value['no_limit']:
        if not value.get('no_limit_evidence_id'):
            raise DataUnavailable('no_limit_evidence_missing')
    else:
        for field in ('up_limit', 'down_limit'):
            result[field] = positive(value[field])
        if result['down_limit'] > result['up_limit']:
            raise DataUnavailable('execution_limits_invalid')
    return result


def evaluate_slot(dataset, code, signal, holding, cutoff):
    """Calendar-indexed exact entry, conservative delayed known-blocked exits."""
    if holding < 2:
        raise ValueError('A_share_T_plus_1_requires_at_least_two_sessions')
    result = dict(code=code, signal_date=signal.isoformat(), holding_sessions=holding,
        status='incomplete', reason='', entry_date=None, exit_date=None, net_return=None,
        double_friction_return=None, benchmark_return=None)
    try:
        exchange = EXCHANGES[dataset.masters()[code]['ts_code'][-2:]]
        sessions = dataset.sessions(exchange, signal, cutoff)
        if not sessions or sessions[0] != signal:
            raise DataUnavailable('signal_calendar_invalid')
        if len(sessions) < 2:
            return dict(result, reason='entry_not_yet_available')
        entry_date = sessions[1]
        result['entry_date'] = entry_date.isoformat()
        entry = execution_bar(dataset.execution(code, entry_date))
        if entry['suspended'] or entry.get('suspended_open', False) or (not entry['no_limit'] and entry['open'] >= entry['up_limit']):
            return dict(result, status='blocked_entry_cash', reason='suspended_or_upper_limit_open',
                        net_return=0., double_friction_return=0., benchmark_return=0.)
        if len(sessions) <= holding:
            return dict(result, reason='holding_outcome_immature')
        for exit_date in sessions[holding:]:
            exit_bar = execution_bar(dataset.execution(code, exit_date))
            if exit_bar['suspended'] or exit_bar.get('suspended_close', False) or (not exit_bar['no_limit'] and exit_bar['close'] <= exit_bar['down_limit']):
                continue
            gross = exit_bar['close'] * exit_bar['adj_factor'] / (entry['open'] * entry['adj_factor'])
            buy, sell = .0008, .0013
            result.update(status='filled', reason='', exit_date=exit_date.isoformat(),
                gross_return=gross - 1, net_return=gross * (1 - sell) / (1 + buy) - 1,
                double_friction_return=gross * (1 - 2 * sell) / (1 + 2 * buy) - 1)
            try:
                result['benchmark_return'] = positive(dataset.benchmark(exit_date, 'close')) / positive(dataset.benchmark(entry_date, 'open')) - 1
            except (ValueError, KeyError, TypeError):
                result['benchmark_reason'] = 'benchmark_endpoint_unknown'
            return result
        return dict(result, reason='blocked_exit_unresolved_at_cutoff')
    except (ValueError, KeyError, TypeError, OverflowError):
        return dict(result, reason='required_execution_input_unknown')


def cohort(slots):
    complete = all(s['net_return'] is not None for s in slots)
    benchmark_complete = all(s['benchmark_return'] is not None for s in slots)
    net = sum(s['net_return'] for s in slots) / 5 if complete else None
    benchmark = sum(s['benchmark_return'] for s in slots) / 5 if benchmark_complete else None
    return dict(net_return=net,
        double_friction_return=sum(s['double_friction_return'] for s in slots) / 5 if complete else None,
        benchmark_difference=net - benchmark if net is not None and benchmark is not None else None,
        blocked_slots=sum(s['status'] == 'blocked_entry_cash' for s in slots),
        unresolved_slots=sum(s['net_return'] is None for s in slots), selected_slots=len(slots))


def evaluate_category(cells):
    valid = [c for c in cells if c['status'] in {'completed', 'no-signal'}]
    # An all-completed-window conclusion cannot be inferred from the subset
    # with known benchmarks, even when every member of that subset is positive.
    if not valid or any(c['mean_benchmark_difference'] is None or c['mean_net_return'] is None for c in valid):
        return 'insufficient evidence'
    if all(c['mean_net_return'] > 0 and c['mean_benchmark_difference'] > 0 for c in valid):
        return 'positive in completed windows'
    if all(c['mean_net_return'] < 0 and c['mean_benchmark_difference'] < 0 for c in valid):
        return 'negative in completed windows'
    return 'mixed across completed windows'


def run_backtest(config, dataset) -> dict:
    frozen, runtime = freeze_config(config)
    if 'execution_workers' in config:
        from src.services.screening.backtest_parallel import ParallelReplay
        with ParallelReplay(dataset, frozen) as parallel:
            return _run_backtest(config, frozen, runtime, parallel.dataset, parallel)
    return _run_backtest(config, frozen, runtime, dataset)


def selection_key(config_hash, input_hash, strategy, signal):
    return digest(dict(config=config_hash, inputs=input_hash, strategy=strategy, date=signal.isoformat()))


def select_day(dataset, signal, strategy_ids, runtime, saved):
    """One original preview path, shared by serial parent and spawned workers."""
    try:
        snapshot, coverage = dataset.snapshot(signal)
    except (ValueError, KeyError, TypeError) as exc:
        return dict(quality=dict(status='unavailable', reason=str(exc).split(':')[0]), selections={})
    feature_cache = {}
    histories = dataset.histories(snapshot, signal) if not snapshot.empty else {}
    selections = {}
    for strategy in strategy_ids:
        value = saved.get(strategy)
        if value is None:
            if snapshot.empty:
                selected, status = [], 'valid'
            else:
                preview = preview_strategy(strategy, snapshot, trade_date=signal,
                    universe=tuple(snapshot.code), calendar_verified=True, universe_verified=True,
                    daily_histories=histories, config=runtime, max_output=5, feature_cache=feature_cache)
                status = 'valid' if preview.result is not None else 'unavailable'
                selected = [asdict(pick) for pick in preview.result.picks] if preview.result is not None else []
            value = dict(status=status, selected=selected)
        selections[strategy] = value
    return dict(quality=dict(status='valid', **coverage), selections=selections)


def selection_jobs(dataset, dates, strategy_ids, config_hash, input_hash):
    for signal in dates:
        saved = {strategy: dataset.store.artifact(selection_key(config_hash, input_hash, strategy, signal))
                 for strategy in strategy_ids}
        yield dict(date=signal.isoformat(), saved={key: value for key, value in saved.items() if value is not None})


def _run_backtest(config, frozen, runtime, dataset, parallel=None):
    output = Path(frozen['output_dir'])
    config_hash, input_hash = digest(frozen), dataset.fingerprint
    cutoff = dataset.last_available_date
    if config.get('acquisition_end'):
        cutoff = min(cutoff, day(config['acquisition_end']))
    strategy_ids = sorted(frozen['strategies'])
    schedules, schedule_errors, all_dates = {}, {}, set()
    for start in FIXED['start_dates']:
        for months in FIXED['window_months']:
            end = add_months(start, months)
            # All planned sessions, including future ones; no cutoff shrink.
            from datetime import timedelta
            try:
                sessions = sorted(set().union(*(dataset.sessions(exchange, day(start), end - timedelta(days=1))
                                               for exchange in EXCHANGES.values())))
                if not sessions:
                    raise DataUnavailable('planned_calendar_empty')
            except (ValueError, KeyError, TypeError):
                sessions = []
                schedule_errors[(start, months)] = 'planned_calendar_unavailable'
            schedules[(start, months)] = sessions
            all_dates.update(sessions)
    results, quality, exposures, signals, trades = {}, {}, {}, [], []
    for signal in sorted(all_dates):
        if signal > cutoff:
            quality[signal.isoformat()] = dict(status='incomplete', reason='future_signal')
    jobs = selection_jobs(dataset, sorted(s for s in all_dates if s <= cutoff), strategy_ids, config_hash, input_hash)
    days = (parallel.days(jobs) if parallel else
            ((job, select_day(dataset, day(job['date']), strategy_ids, runtime, job['saved'])) for job in jobs))
    for job, computed in days:
        signal = day(job['date'])
        quality[signal.isoformat()] = computed['quality']
        if computed['quality']['status'] != 'valid':
            continue
        for strategy in strategy_ids:
            saved = computed['selections'][strategy]
            if strategy not in job['saved'] and saved['status'] == 'valid':
                dataset.store.artifact(selection_key(config_hash, input_hash, strategy, signal), saved)
            if saved['status'] != 'valid':
                results[(strategy, signal)] = None
                continue
            picks = saved['selected']
            exposure = exposures.setdefault(strategy, Counter())
            for pick in picks:
                exposure[pick['industry_taxonomy'] + ':' + pick['industry_code']] += 1
                signals.append(dict(strategy=strategy, signal_date=signal.isoformat(), code=pick['code'],
                    industry_code=pick['industry_code'], industry_taxonomy=pick['industry_taxonomy']))
            holds = {}
            for holding in FIXED['holding_sessions']:
                slots = [dict(evaluate_slot(dataset, pick['code'], signal, holding, cutoff), strategy=strategy) for pick in picks]
                trades.extend(slots)
                holds[holding] = cohort(slots)
            results[(strategy, signal)] = holds
    cells = []
    for strategy in strategy_ids:
        for (start, months), schedule in schedules.items():
            for holding in FIXED['holding_sessions']:
                cohorts = [results[(strategy, signal)][holding] for signal in schedule
                    if results.get((strategy, signal)) is not None]
                mature = [c for c in cohorts if c['net_return'] is not None]
                window_complete = all(signal <= cutoff for signal in schedule)
                status = ('unavailable' if (start, months) in schedule_errors or any(quality.get(s.isoformat(), {}).get('status') == 'unavailable'
                          or (s <= cutoff and results.get((strategy, s)) is None) for s in schedule)
                          else 'incomplete' if not window_complete or len(mature) != len(schedule)
                          else 'no-signal' if all(c['selected_slots'] == 0 for c in cohorts) else 'completed')
                valid = status in {'completed', 'no-signal'}
                returns = [c['net_return'] for c in mature]
                differences = [c['benchmark_difference'] for c in mature]
                cells.append(dict(strategy=strategy, start=start, months=months, holding_sessions=holding,
                    status=status, reason=schedule_errors.get((start, months)),
                    signal_window_complete=window_complete if schedule else False,
                    scheduled_cohorts=len(schedule) if schedule else None,
                    valid_cohorts=len(cohorts), mature_cohorts=len(mature),
                    blocked_slots=sum(c['blocked_slots'] for c in cohorts),
                    unresolved_slots=sum(c['unresolved_slots'] for c in cohorts),
                    mean_net_return=mean(returns) if valid and returns else None,
                    median_net_return=median(returns) if valid and returns else None,
                    positive_cohort_proportion=mean(r > 0 for r in returns) if valid and returns else None,
                    worst_cohort=min(returns) if valid and returns else None,
                    mean_double_friction_return=mean(c['double_friction_return'] for c in mature) if valid and mature else None,
                    mean_benchmark_difference=mean(differences) if valid and differences and all(d is not None for d in differences) else None))
    quality['_planned_calendar_errors'] = {f'{start}/{months}': reason for (start, months), reason in schedule_errors.items()}
    metrics = dict(category=evaluate_category(cells), planned_cells=len(cells),
        completed_cells=sum(c['status'] in {'completed', 'no-signal'} for c in cells), cells=cells,
        benchmark_comparable_cells=sum(c['status'] in {'completed', 'no-signal'} and c['mean_benchmark_difference'] is not None for c in cells),
        industry_exposure={key: dict(counts=dict(value), leading_industry=value.most_common(1)[0][0] if value else None,
            leading_share=value.most_common(1)[0][1] / sum(value.values()) if value else None) for key, value in exposures.items()})
    for name, value in [('metrics.json', metrics), ('data_quality.json', quality)]:
        write_json(output / name, value)
    for name, rows, fields in [('signals.csv', signals, ['strategy', 'signal_date', 'code', 'industry_code', 'industry_taxonomy']),
                               ('trades.csv', trades, ['strategy', 'signal_date', 'code', 'holding_sessions', 'status', 'reason', 'entry_date', 'exit_date', 'net_return', 'double_friction_return', 'benchmark_return'])]:
        with (output / name).open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
    (output / 'report.md').write_text(
        f"# Current-rule retrospective replay\n\n{metrics['category']}; completed/planned: "
        f"{metrics['completed_cells']}/{metrics['planned_cells']}. Last verified data: {cutoff}.\n\n"
        "Publication-lagged CAPCO reconstruction, not original decisions or untouched out-of-sample evidence. "
        "Overlapping cohorts/windows are descriptive and not independent observations. "
        "Five fractional 20% slots; unselected/known-blocked slots retain cash; no replacement. "
        "Commission 3 bps and slippage 5 bps per side; sell stamp tax 5 bps assumed. "
        "Provider adjustment, not a separately audited dividend ledger. CSI300 is a price index, not dividend-matched alpha. "
        "Daily-bar fills are conservative assumptions, not proof of execution. No NAV, drawdown, annualization or predictive guarantee. "
        "Industry exposure is descriptive, not a causal explanation of returns. Unknown metrics remain null.\n", encoding='utf-8')
    from hashlib import sha256
    write_json(output / 'manifest.json', dict(config_hash=config_hash, dataset_hash=input_hash,
        reference_hash=dataset.references.hash, semantics=SEMANTICS,
        files={name: sha256((output / name).read_bytes()).hexdigest() for name in
               ('config.json', 'data_quality.json', 'metrics.json', 'signals.csv', 'trades.csv', 'report.md')}))
    return metrics
