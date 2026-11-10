"""Same-day, content-addressed collection state over the existing evidence store.

Only the collector's whitelisted normalized data is persisted. Mutable pointers
name immutable digests; all records and raw references are verified before reuse.
This detects corruption, not an attacker authorized to rewrite the entire store.
"""
from datetime import datetime
from hashlib import sha256
import json
import re
import os
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict

import pandas as pd

from src.services.research_delivery.live_contracts import json_safe

DEPENDENCY_VERSION = 'daily-complete-v3-sma-rsi-300d'


def digest(value):
    return sha256(json.dumps(json_safe(value), sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def strategy_catalog_hash(strategies_dir, target):
    from src.services.screening.strategy import load_all_strategies
    definitions = load_all_strategies(strategies_dir)
    return digest({'business_date': target.isoformat(), 'dependency_version': DEPENDENCY_VERSION,
        'strategies': {key: asdict(definitions[key]) for key in sorted(definitions)}})


def pack_frame(frame):
    value = frame.copy()
    if 'date' in value:
        value['date'] = pd.to_datetime(value['date']).dt.strftime('%Y-%m-%d')
    return json_safe(dict(columns=list(value.columns), rows=value.astype(object).where(value.notna(), None).values.tolist(),
                          attrs=value.attrs))


def unpack_frame(value):
    frame = pd.DataFrame(value['rows'], columns=value['columns'])
    frame.attrs = value['attrs']
    return frame


class CollectionCheckpoint:
    def __init__(self, store, target, request_hash, *, guard=nullcontext):
        self.store, self.target, self.request_hash = store, target, request_hash
        self.directory = store.directory / target.isoformat()
        self.pointer = self.directory / ('collection-' + request_hash + '.json')
        self.owner = uuid.uuid4().hex
        self.guard = guard

    @contextmanager
    def _locked(self):
        # Guard holds the worker's SQLite write transaction, so lease takeover
        # and pointer publication have a common linearization boundary. File
        # locking additionally protects direct collectors / separate processes.
        with self.guard():
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.pointer.with_suffix('.lock').open('a+b') as lock:
                if lock.tell() == 0:
                    lock.write(b'0')
                    lock.flush()
                lock.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    lock.seek(0)
                    if os.name == 'nt':
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock, fcntl.LOCK_UN)

    def _publish(self, identifier):
        temporary = self.pointer.with_name(self.pointer.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps({'sha256': identifier, 'owner': self.owner}), encoding='utf-8')
            temporary.replace(self.pointer)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, identifier, now):
        if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{64}', identifier):
            raise ValueError('checkpoint_invalid')
        raw = (self.directory / (identifier + '.json')).read_bytes()
        if sha256(raw).hexdigest() != identifier:
            raise ValueError('checkpoint_invalid')
        value = json.loads(raw)
        fetched = datetime.fromisoformat(value['fetched_at'])
        if (value['business_date'] != self.target.isoformat() or fetched.utcoffset() is None
                or fetched.astimezone(now.tzinfo).date() != self.target or fetched > now):
            raise ValueError('checkpoint_invalid')
        return value

    def load(self, now):
        # Every collecting session takes a new generation before market fetch.
        # A stale session can never overwrite a newer claimant, even if both
        # began before the first snapshot was captured.
        with self._locked():
            pointer = json.loads(self.pointer.read_text(encoding='utf-8')) if self.pointer.exists() else {}
            if self.pointer.exists():
                if not isinstance(pointer, dict) or 'sha256' not in pointer:
                    raise ValueError('checkpoint_invalid')
                identifier = pointer['sha256']
                owner = pointer.get('owner')
                if identifier is None:
                    if not isinstance(owner, str) or not re.fullmatch('[0-9a-f]{32}', owner):
                        raise ValueError('checkpoint_invalid')
                elif not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{64}', identifier):
                    raise ValueError('checkpoint_invalid')
            identifier = pointer.get('sha256')
            data = self._load_record(identifier, now) if identifier is not None else None
            self._publish(identifier)
            return data

    def _load_record(self, identifier, now):
        record = self._read(identifier, now)
        if record.get('source') != 'collection' or record.get('dataset') != 'checkpoint':
            raise ValueError('checkpoint_invalid')
        data = record['payload']
        if data['request_hash'] != self.request_hash or data['dependency_version'] != DEPENDENCY_VERSION:
            raise ValueError('checkpoint_invalid')
        if digest(data['snapshot']) != data['snapshot_hash'] or digest(data['pool']) != data['pool_hash']:
            raise ValueError('checkpoint_invalid')
        for entry in data['evidence']:
            raw = self._read(entry['sha256'], now)
            if any(raw.get(k) != entry.get(k) for k in ('source', 'fetched_at', 'business_date', 'endpoint')):
                raise ValueError('checkpoint_invalid')
            # Rebuild paths locally, never trust an arbitrary stored path.
            entry['path'] = str((self.directory / (entry['sha256'] + '.json')).resolve())
        return data

    def save(self, data, now):
        data = dict(data, request_hash=self.request_hash, dependency_version=DEPENDENCY_VERSION,
                    snapshot_hash=digest(data['snapshot']), pool_hash=digest(data['pool']))
        with self._locked():
            pointer = json.loads(self.pointer.read_text(encoding='utf-8')) if self.pointer.exists() else {}
            if pointer.get('owner') != self.owner:
                raise ValueError('checkpoint_superseded')
            ref = self.store.capture_payload('collection', 'checkpoint', data, fetched_at=now,
                business_date=self.target, provenance='derived')
            self._publish(ref['sha256'])
