"""Opt-in bounded spawn replay over independently verified pinned SQLite views.

Only the parent delegates selection-artifact writes to the original store.
No providers, acquisition, environment-file loading or alternate scoring path.
"""
from collections import deque
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3

from data_provider.historical_reference import ReferenceBundle, day, digest
from src.services.screening.backtest_data import BatchStore, HistoricalDataset

THREAD_VARIABLES = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')
JOB_TIMEOUT = 3600


def verified_store_identity(store):
    """Rehash accepted batches AND actual indexed bytes, never just stored hashes."""
    market = store.fingerprint()
    for key, endpoint, stamp, raw in store.db.execute('SELECT key,endpoint,stamp,raw FROM batches'):
        payload = json.loads(raw)
        if (key != digest(dict(endpoint=payload['endpoint'], params=payload['params']))
                or endpoint != payload['endpoint'] or stamp != str(payload['params'].get('trade_date', ''))):
            raise ValueError('parallel_batch_identity_invalid')
    for endpoint, code, stamp, raw, expected, parent in store.db.execute(
            'SELECT r.endpoint,r.code,r.stamp,r.raw,r.row_sha,b.endpoint FROM rows r LEFT JOIN batches b ON b.key=r.batch_key'):
        row = store.decode_row((raw, expected))
        if endpoint != parent or code != str(row.get('ts_code', '')) or stamp != str(row.get('trade_date', '')):
            raise ValueError('parallel_index_identity_invalid')
    # Date acceptance participates even though the historical input hash predates it.
    states = store.db.execute('SELECT stamp,state,reference_hash FROM date_state ORDER BY stamp').fetchall()
    return dict(market=market, date_state=digest(states))


def verify_runtime(frozen):
    from importlib.metadata import version
    import sys
    from src.services.screening.config import Config
    from src.services.screening.strategy import load_all_strategies
    root = Path(__file__).resolve().parents[3]
    for name, expected in frozen['implementation_hashes'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('parallel_implementation_mismatch')
    runtime = Config()
    catalog = {key: asdict(value) for key, value in sorted(load_all_strategies(runtime.strategies_dir).items())}
    versions = dict(python=sys.version, **{name: version(name) for name in ('pandas', 'numpy', 'PyYAML')})
    if catalog != frozen['strategies'] or versions != frozen['calculation_runtime']:
        raise ValueError('parallel_catalog_or_runtime_mismatch')
    return runtime


class PinnedReadStore(BatchStore):
    """Internal capability: ro + query_only + non-releasable verified read pin."""
    def __init__(self, path, expected, artifact_store=None):
        self.path = Path(path).resolve()
        self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        self._artifact_changes = 0
        self._artifact_store = artifact_store
        try:
            self.db.execute('PRAGMA query_only=ON')
            self.db.execute('BEGIN')
            # First read pins SQLite's snapshot BEFORE fingerprint verification.
            self.db.execute('SELECT count(*) FROM batches').fetchone()
            if verified_store_identity(self) != expected:
                raise ValueError('parallel_input_mismatch')
            self.db.set_authorizer(self._authorize)
        except BaseException:
            self.db.close()
            raise

    @staticmethod
    def _authorize(action, arg1, arg2, database, origin):
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        if action in allowed or (action == sqlite3.SQLITE_PRAGMA and arg1 in {'data_version', 'schema_version', 'query_only'} and arg2 is None):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def artifact(self, key, value=None):
        if self._artifact_store is None:
            raise ValueError('parallel_child_artifact_access_forbidden')
        return self._artifact_store.artifact(key, value)


def uncertain_transaction(store):
    # No boolean flag/attrs escape hatch for ordinary mutable stores. Only this
    # exact verified constructor can retain caches within its unreleasable pin.
    if type(store) is PinnedReadStore:
        if not store.db.in_transaction or store.db.execute('PRAGMA query_only').fetchone()[0] != 1:
            raise ValueError('parallel_read_pin_lost')
        return False
    return store.db.in_transaction


def make_snapshot(dataset, output):
    if type(dataset) is not HistoricalDataset or type(dataset.store) is not BatchStore or not dataset.store.path.is_file():
        raise ValueError('parallel_requires_disk_historical_dataset')
    if dataset.store.db.in_transaction:
        raise ValueError('parallel_source_transaction_active')
    references = ReferenceBundle(deepcopy(dataset.references.payload))
    if references.hash != dataset.references.hash:
        raise ValueError('parallel_reference_mismatch')
    original = verified_store_identity(dataset.store)
    expected = dict(store=original, reference_hash=references.hash,
                    input_hash=digest(dict(market=original['market'], references=references.hash)))
    output = Path(output)
    snapshot, identity = output / 'replay-input.sqlite', output / 'replay-input.json'
    if snapshot.exists() or identity.exists():
        if not snapshot.is_file() or not identity.is_file() or json.loads(identity.read_text(encoding='utf-8')) != expected:
            raise ValueError('parallel_snapshot_conflict')
    else:
        # Exclusive creation: never overwrite a prior partial/different snapshot.
        with snapshot.open('xb'):
            pass
        destination = sqlite3.connect(snapshot)
        try:
            dataset.store.db.backup(destination)
        finally:
            destination.close()
        if verified_store_identity(dataset.store) != original:
            raise ValueError('parallel_source_changed_during_backup')
        check = PinnedReadStore(snapshot, original)
        check.close()
        with identity.open('x', encoding='utf-8') as stream:
            json.dump(expected, stream, sort_keys=True)
    return snapshot, references, expected


def envelope(identity, job, strategies, computed):
    value = dict(identity=identity, date=job['date'], strategies=strategies, computed=computed)
    return dict(value, content_digest=digest(value))


def validate_envelope(value, identity, job, strategies):
    if not isinstance(value, dict) or set(value) != {'identity', 'date', 'strategies', 'computed', 'content_digest'}:
        raise ValueError('parallel_result_schema_invalid')
    payload = {key: item for key, item in value.items() if key != 'content_digest'}
    if value['identity'] != identity or value['date'] != job['date'] or value['strategies'] != strategies or digest(payload) != value['content_digest']:
        raise ValueError('parallel_result_identity_invalid')
    computed = value['computed']
    if not isinstance(computed, dict) or set(computed) != {'quality', 'selections'}:
        raise ValueError('parallel_result_schema_invalid')
    status = computed['quality'].get('status')
    selections = computed['selections']
    if status not in {'valid', 'unavailable'} or set(selections) != (set(strategies) if status == 'valid' else set()):
        raise ValueError('parallel_result_scope_invalid')
    for selection in selections.values():
        if (set(selection) != {'status', 'selected'} or selection['status'] not in {'valid', 'unavailable'}
                or not isinstance(selection['selected'], list) or len(selection['selected']) > 5
                or (selection['status'] != 'valid' and selection['selected'])):
            raise ValueError('parallel_selection_invalid')
    return computed


def worker_main(connection, descriptor):
    """Spawn target: independent verification once, then one date at a time."""
    store = None
    try:
        from src.services.screening.backtest import select_day
        runtime = verify_runtime(descriptor['frozen'])
        if digest(descriptor['frozen']) != descriptor['identity']['config_hash']:
            raise ValueError('parallel_config_mismatch')
        references = ReferenceBundle(descriptor['references'])
        if references.hash != descriptor['identity']['reference_hash']:
            raise ValueError('parallel_reference_mismatch')
        store = PinnedReadStore(descriptor['snapshot'], descriptor['identity']['store'])
        if digest(dict(market=descriptor['identity']['store']['market'], references=references.hash)) != descriptor['identity']['input_hash']:
            raise ValueError('parallel_input_mismatch')
        dataset = HistoricalDataset(store, references)
        strategies = sorted(descriptor['frozen']['strategies'])
        connection.send(dict(ready=descriptor['identity']))
        while True:
            job = connection.recv()
            if job is None:
                break
            if set(job) != {'date', 'saved'} or not set(job['saved']) <= set(strategies):
                raise ValueError('parallel_job_invalid')
            computed = select_day(dataset, day(job['date']), strategies, runtime, job['saved'])
            connection.send(envelope(descriptor['identity'], job, strategies, computed))
    except BaseException as exc:
        # Infrastructure/contract errors are never converted into empty picks.
        try:
            connection.send(dict(error=type(exc).__name__ + ':' + str(exc)))
        except (OSError, EOFError):
            pass
    finally:
        if store is not None:
            store.close()
        connection.close()


def receive(connection, process, timeout=JOB_TIMEOUT):
    if not connection.poll(timeout):
        raise RuntimeError('parallel_worker_timeout')
    try:
        value = connection.recv()
    except (EOFError, OSError) as exc:
        raise RuntimeError('parallel_worker_crashed') from exc
    if isinstance(value, dict) and 'error' in value:
        raise RuntimeError('parallel_worker_failed:' + value['error'])
    return value


class ParallelReplay:
    def __init__(self, dataset, frozen):
        self.frozen, self.workers = frozen, frozen['execution_workers']
        if type(self.workers) is not int or not 1 <= self.workers <= 6:
            raise ValueError('execution_workers_invalid')
        self.processes, self.connections = [], []
        self.dataset = None
        self.source = dataset

    def __enter__(self):
        verify_runtime(self.frozen)
        snapshot, references, identity = make_snapshot(self.source, self.frozen['output_dir'])
        self.identity = dict(identity, config_hash=digest(self.frozen))
        view = PinnedReadStore(snapshot, identity['store'], self.source.store)
        self.dataset = HistoricalDataset(view, references)
        self.descriptor = dict(snapshot=str(snapshot), references=references.payload, frozen=self.frozen, identity=self.identity)
        return self

    def days(self, jobs):
        jobs = iter(jobs)
        pending = deque()
        ctx = multiprocessing.get_context('spawn')
        previous = {key: os.environ.get(key) for key in THREAD_VARIABLES}
        try:
            for key in THREAD_VARIABLES:
                os.environ[key] = '1'
            for _ in range(self.workers):
                job = next(jobs, None)
                if job is None:
                    break
                parent, child = ctx.Pipe()
                process = ctx.Process(target=worker_main, args=(child, self.descriptor))
                try:
                    process.start()
                except BaseException:
                    parent.close()
                    child.close()
                    raise
                child.close()
                self.processes.append(process)
                self.connections.append(parent)
                pending.append((parent, process, job))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        for connection, process, job in pending:
            if receive(connection, process) != dict(ready=self.identity):
                raise ValueError('parallel_worker_identity_invalid')
            connection.send(job)
        while pending:
            connection, process, job = pending.popleft()
            computed = validate_envelope(receive(connection, process), self.identity, job, sorted(self.frozen['strategies']))
            # Consume in original date order, regardless of which child finished.
            yield job, computed
            following = next(jobs, None)
            if following is not None:
                connection.send(following)
                pending.append((connection, process, following))

    def __exit__(self, kind, value, traceback):
        for connection, process in zip(self.connections, self.processes):
            if process.is_alive() and kind is None:
                try:
                    connection.send(None)
                except (EOFError, OSError):
                    pass
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            connection.close()
        if self.dataset is not None:
            self.dataset.store.close()
