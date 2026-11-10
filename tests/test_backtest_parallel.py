"""Temporary, independently pinned inputs and real Windows-spawn replay."""
import json
import csv
import sqlite3
import multiprocessing
import os
from datetime import date

import pytest


@pytest.mark.parametrize('workers', [0, -1, 7, 8, True, 1.5, '6'])
def test_worker_count_is_strict(tmp_path, workers):
    from src.services.screening.backtest import freeze_config
    from src.services.screening.backtest_parallel import ParallelReplay
    with pytest.raises(ValueError, match='execution_workers_invalid'):
        freeze_config(dict(output_dir=str(tmp_path / 'run'), execution_workers=workers))
    with pytest.raises(ValueError, match='execution_workers_invalid'):
        ParallelReplay(None, dict(execution_workers=workers))


def test_cli_rejects_worker_count_before_reading_inputs():
    from scripts.backtest_screening import main
    with pytest.raises(SystemExit) as exc:
        main(['--config', 'not-read.json', '--output-dir', 'not-created', '--workers', '7'])
    assert exc.value.code == 2


@pytest.mark.parametrize('workers', [4, 5, 6])
def test_expanded_worker_counts_are_accepted(tmp_path, workers):
    from src.services.screening.backtest import freeze_config
    from src.services.screening.backtest_parallel import ParallelReplay
    frozen, _ = freeze_config(dict(output_dir=str(tmp_path / 'run'), execution_workers=workers))
    assert frozen['execution_workers'] == ParallelReplay(None, frozen).workers == workers


def test_actual_spawn_matches_serial_and_resumes(tmp_path, monkeypatch):
    from tests.test_screening_backtest_data import seeded_dataset
    from src.services.screening.backtest import run_backtest, selection_key
    from data_provider.historical_reference import digest
    from src.services.research_delivery.backtest_delivery import validate_artifacts
    from src.services.screening.backtest_parallel import ParallelReplay
    original_days, started = ParallelReplay.days, []
    def observed_days(self, jobs):
        yield from original_days(self, jobs)
        started.append(tuple(process.pid for process in self.processes))
    monkeypatch.setattr(ParallelReplay, 'days', observed_days)
    dataset = seeded_dataset(tmp_path)
    try:
        expected = run_backtest(dict(output_dir=str(tmp_path / 'serial'), acquisition_end='2026-01-12'), dataset)
        assert any(row['status'] == 'filled' for row in csv.DictReader(
            (tmp_path / 'serial' / 'trades.csv').read_text(encoding='utf-8-sig').splitlines()))
        input_hash = dataset.fingerprint
        def saved_selections(directory):
            frozen = json.loads((directory / 'config.json').read_text(encoding='utf-8'))
            return [dataset.store.artifact(selection_key(digest(frozen), input_hash, strategy, date(2026, 1, d)))
                    for d in (5, 6, 7, 8, 9, 12) for strategy in sorted(frozen['strategies'])]
        serial_selections = saved_selections(tmp_path / 'serial')
        for count in (1, 3, 6):
            directory = tmp_path / str(count)
            config = dict(output_dir=str(directory), acquisition_end='2026-01-12', execution_workers=count)
            assert run_backtest(config, dataset) == expected
            assert len(started[-1]) == len(set(started[-1])) == count
            assert all(pid is not None for pid in started[-1])
            validate_artifacts(directory)
            assert saved_selections(directory) == serial_selections  # Scores/order/state, not only CSV codes.
            for name in ('metrics.json', 'signals.csv', 'trades.csv', 'data_quality.json', 'report.md'):
                assert (directory / name).read_bytes() == (tmp_path / 'serial' / name).read_bytes()
            changes = dataset.store.db.total_changes
            assert run_backtest(config, dataset) == expected
            assert len(started[-1]) == len(set(started[-1])) == count
            assert dataset.store.db.total_changes == changes
    finally:
        dataset.store.close()


def _guard_probe(connection):
    import __main__
    import dotenv
    import requests
    rejected = []
    for action in (lambda: requests.get('https://example.invalid'),
                   lambda: sqlite3.connect('Z:/not-temp/forbidden.sqlite')):
        try:
            action()
        except AssertionError:
            rejected.append(True)
    connection.send(dict(rejected=rejected, env=dotenv.dotenv_values('Z:/not-temp/forbidden.env'),
                         loaded=dotenv.load_dotenv('Z:/not-temp/forbidden.env'), main=__main__.__name__))
    connection.close()


def test_spawn_keeps_offline_guard_without_reentering_pytest():
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_guard_probe, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(30)
        assert parent.recv() == dict(rejected=[True, True], env={}, loaded=False, main='__mp_main__')
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def _crash_worker(connection, descriptor):
    os._exit(17)


def test_worker_crash_does_not_cache_empty_success(tmp_path, monkeypatch):
    from tests.test_screening_backtest_data import seeded_dataset
    from src.services.screening.backtest import run_backtest
    from src.services.screening import backtest_parallel as parallel
    dataset = seeded_dataset(tmp_path)
    before = dataset.store.db.execute('SELECT count(*) FROM artifacts').fetchone()[0]
    monkeypatch.setattr(parallel, 'worker_main', _crash_worker)
    try:
        with pytest.raises(RuntimeError, match='crashed'):
            run_backtest(dict(output_dir=str(tmp_path / 'crash'), execution_workers=1, acquisition_end='2026-01-05'), dataset)
        assert dataset.store.db.execute('SELECT count(*) FROM artifacts').fetchone()[0] == before
        assert (tmp_path / 'crash' / 'replay-input.sqlite').is_file()
        assert not (tmp_path / 'crash' / 'manifest.json').exists()
    finally:
        dataset.store.close()


def test_data_unavailable_day_stays_unavailable_in_spawn(tmp_path):
    from tests.test_screening_backtest_data import seeded_dataset
    from src.services.screening.backtest import run_backtest
    from data_provider.historical_reference import digest
    dataset = seeded_dataset(tmp_path)
    params = dict(trade_date='20260105')
    key = digest(dict(endpoint='suspend_d', params=params))
    with dataset.store.db:
        dataset.store.db.execute('DELETE FROM rows WHERE batch_key=?', (key,))
        dataset.store.db.execute('DELETE FROM batches WHERE key=?', (key,))
    dataset.store.put('suspend_d', params,
        [dict(ts_code='600001.SH', trade_date='20260105', suspend_type=kind, suspend_timing=None) for kind in ('S', 'R')],
        '2026-01-06T00:00:00+00:00')
    try:
        config = dict(output_dir=str(tmp_path / 'serial'), acquisition_end='2026-01-05')
        expected = run_backtest(config, dataset)
        assert run_backtest(dict(config, output_dir=str(tmp_path / 'spawn'), execution_workers=1), dataset) == expected
        quality = json.loads((tmp_path / 'spawn' / 'data_quality.json').read_text(encoding='utf-8'))
        assert quality['2026-01-05']['status'] == 'unavailable'
        assert (tmp_path / 'spawn' / 'data_quality.json').read_bytes() == (tmp_path / 'serial' / 'data_quality.json').read_bytes()
    finally:
        dataset.store.close()


def test_pinned_view_is_readonly_but_memo_and_parent_artifacts_work(tmp_path, monkeypatch):
    from tests.test_screening_backtest_data import seeded_dataset
    from src.services.screening.backtest_parallel import make_snapshot, PinnedReadStore
    from src.services.screening.backtest_data import HistoricalDataset
    dataset = seeded_dataset(tmp_path)
    output = tmp_path / 'pin'
    output.mkdir()
    path, refs, identity = make_snapshot(dataset, output)
    view = PinnedReadStore(path, identity['store'], dataset.store)
    child = PinnedReadStore(path, identity['store'])
    try:
        for sql in ('DELETE FROM rows', 'COMMIT', 'ROLLBACK', 'SAVEPOINT x', 'PRAGMA query_only=OFF'):
            with pytest.raises(sqlite3.DatabaseError):
                view.db.execute(sql)
        with pytest.raises(ValueError, match='forbidden'):
            child.artifact('x', {})
        replay = HistoricalDataset(view, refs)
        snapshot, _ = replay.snapshot(date(2026, 1, 5))
        original, calls = replay.history, []
        def tracked(*args):
            calls.append(args)
            return original(*args)
        monkeypatch.setattr(replay, 'history', tracked)
        histories = replay.histories(snapshot, date(2026, 1, 5))
        histories['600001']
        view.artifact('delegated', {'ok': True})
        histories['600001']
        assert len(calls) == 1 and 0 < histories.used_bytes <= histories.max_bytes
        assert dataset.store.artifact('delegated') == {'ok': True}
        assert view.db.execute("SELECT count(*) FROM artifacts WHERE key='delegated'").fetchone()[0] == 0
    finally:
        child.close()
        view.close()
        dataset.store.close()


@pytest.mark.parametrize('mutation', ['raw', 'index', 'accepted_state'])
def test_corrupt_or_changed_private_snapshot_is_rejected(tmp_path, mutation):
    from tests.test_screening_backtest_data import seeded_dataset
    from src.services.screening.backtest_parallel import make_snapshot, PinnedReadStore
    dataset = seeded_dataset(tmp_path)
    output = tmp_path / 'pin'
    output.mkdir()
    try:
        path, _, identity = make_snapshot(dataset, output)
        with sqlite3.connect(path) as writer:
            writer.execute({'raw': "UPDATE rows SET raw='{}' WHERE endpoint='daily'",
                            'index': "UPDATE rows SET code='600999.SH' WHERE endpoint='daily'",
                            'accepted_state': "UPDATE date_state SET state='incomplete'"}[mutation])
        with pytest.raises(ValueError):
            PinnedReadStore(path, identity['store'])
    finally:
        dataset.store.close()


def test_reference_implementation_catalog_and_incompatible_dataset_reject(tmp_path):
    from copy import deepcopy
    from tests.test_screening_backtest_data import seeded_dataset
    from src.services.screening.backtest import freeze_config
    from src.services.screening.backtest_parallel import make_snapshot, verify_runtime
    dataset = seeded_dataset(tmp_path)
    output = tmp_path / 'run'
    frozen, _ = freeze_config(dict(output_dir=str(output), execution_workers=1))
    try:
        assert 'src/services/screening/backtest_parallel.py' in frozen['implementation_hashes']
        altered = deepcopy(frozen)
        altered['implementation_hashes']['src/services/screening/backtest_parallel.py'] = '0' * 64
        with pytest.raises(ValueError, match='implementation'):
            verify_runtime(altered)
        altered = deepcopy(frozen)
        altered['strategies'].pop(next(iter(altered['strategies'])))
        with pytest.raises(ValueError, match='catalog'):
            verify_runtime(altered)
        dataset.references.payload['aliases_sha256'] = 'f' * 64
        with pytest.raises(ValueError, match='reference'):
            make_snapshot(dataset, output)
        with pytest.raises(ValueError, match='requires_disk'):
            make_snapshot(object(), output)
    finally:
        dataset.store.close()


@pytest.mark.parametrize('change', ['reference', 'implementation', 'config'])
def test_actual_child_independently_rejects_descriptor_changes(tmp_path, change):
    from tests.test_screening_backtest_data import seeded_dataset
    from src.services.screening.backtest import freeze_config
    from src.services.screening.backtest_parallel import ParallelReplay
    dataset = seeded_dataset(tmp_path)
    frozen, _ = freeze_config(dict(output_dir=str(tmp_path / 'run'), execution_workers=1))
    try:
        with ParallelReplay(dataset, frozen) as replay:
            if change == 'reference':
                replay.descriptor['references']['aliases_sha256'] = 'f' * 64
            elif change == 'implementation':
                replay.descriptor['frozen']['implementation_hashes']['src/services/screening/backtest_parallel.py'] = '0' * 64
            else:
                replay.descriptor['frozen']['execution_workers'] = 2
            with pytest.raises(RuntimeError, match='parallel_worker_failed:ValueError:parallel_' + change + '_mismatch'):
                list(replay.days(iter([dict(date='2026-01-05', saved={})])))
    finally:
        dataset.store.close()


def _readonly_probe(connection, path, identity):
    from src.services.screening.backtest_parallel import PinnedReadStore
    store = PinnedReadStore(path, identity)
    rejected = []
    for action in (lambda: store.db.execute('DELETE FROM rows'), lambda: store.db.execute('COMMIT'),
                   lambda: store.artifact('child', {'bad': True})):
        try:
            action()
        except (sqlite3.DatabaseError, ValueError):
            rejected.append(True)
    connection.send(rejected)
    store.close()
    connection.close()


def test_actual_child_cannot_write_or_release_input_pin(tmp_path):
    from tests.test_screening_backtest_data import seeded_dataset
    from src.services.screening.backtest_parallel import make_snapshot
    dataset = seeded_dataset(tmp_path)
    output = tmp_path / 'pin'
    output.mkdir()
    path, _, identity = make_snapshot(dataset, output)
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_readonly_probe, args=(child, str(path), identity['store']))
    process.start()
    child.close()
    try:
        assert parent.poll(30) and parent.recv() == [True, True, True]
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        dataset.store.close()


@pytest.mark.parametrize('bad', ['date', 'identity', 'missing', 'duplicate', 'digest'])
def test_foreign_missing_duplicate_envelopes_reject(bad):
    from src.services.screening.backtest_parallel import envelope, validate_envelope
    from data_provider.historical_reference import digest
    identity, job, strategies = {'config_hash': 'abc'}, {'date': '2026-01-05'}, ['a', 'b']
    computed = dict(quality={'status': 'valid'}, selections={s: dict(status='valid', selected=[]) for s in strategies})
    value = envelope(identity, job, strategies, computed)
    if bad == 'date':
        value['date'] = '2026-01-06'
    elif bad == 'identity':
        value['identity'] = {'config_hash': 'foreign'}
    elif bad == 'missing':
        del value['computed']['selections']['b']
    elif bad == 'duplicate':
        value['strategies'] = ['a', 'a']
    else:
        value['content_digest'] = '0' * 64
    if bad != 'digest':
        value['content_digest'] = digest({k: v for k, v in value.items() if k != 'content_digest'})
    with pytest.raises(ValueError, match='parallel_result'):
        validate_envelope(value, identity, job, strategies)


def test_timeout_is_infrastructure_failure():
    from src.services.screening.backtest_parallel import receive
    class Silent:
        def poll(self, timeout):
            assert timeout == .001
            return False
    with pytest.raises(RuntimeError, match='timeout'):
        receive(Silent(), None, .001)


def test_bounded_scheduler_consumes_date_order_and_restores_thread_env(monkeypatch):
    from src.services.screening import backtest_parallel as parallel
    replay = parallel.ParallelReplay(None, {'execution_workers': 3, 'strategies': {'a': {}}})
    replay.identity, replay.descriptor = {'id': 'fixture'}, {}
    active, maximum, submitted, completion_order = set(), [], [], []
    class Connection:
        ready = True
        def send(self, job):
            self.job = job
            active.add(job['date'])
            submitted.append(job['date'])
            maximum.append(len(active))
            if len(submitted) == 3:
                completion_order.extend(reversed(submitted))
            elif len(submitted) > 3:
                completion_order.append(job['date'])
        def close(self):
            pass
    class Process:
        def __init__(self, **kwargs):
            pass
        def start(self):
            assert all(os.environ[key] == '1' for key in parallel.THREAD_VARIABLES)
    class Context:
        def Pipe(self):
            return Connection(), Connection()
    Context.Process = Process
    def receive(connection, process):
        if connection.ready:
            connection.ready = False
            return {'ready': replay.identity}
        # All submitted jobs may have finished in reverse order; receipt is
        # deliberately selected by the parent's expected serial position.
        active.remove(connection.job['date'])
        return parallel.envelope(replay.identity, connection.job, ['a'],
            dict(quality={'status': 'valid'}, selections={'a': dict(status='valid', selected=[])}))
    monkeypatch.setattr(parallel.multiprocessing, 'get_context', lambda method: Context())
    monkeypatch.setattr(parallel, 'receive', receive)
    monkeypatch.setenv('OMP_NUM_THREADS', '7')
    jobs = [dict(date=f'2026-01-{index:02d}', saved={}) for index in range(5, 12)]
    assert [job for job, _ in replay.days(iter(jobs))] == jobs
    assert max(maximum) == 3 and len(replay.processes) == 3
    assert completion_order[:3] == list(reversed([job['date'] for job in jobs[:3]]))
    assert os.environ['OMP_NUM_THREADS'] == '7'
