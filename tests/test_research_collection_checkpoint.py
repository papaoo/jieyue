"""Deterministic competing collector and stale writer tests, no network."""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from src.services.research_delivery.collection_checkpoint import CollectionCheckpoint
from src.services.research_delivery.evidence_store import EvidenceStore
from tests.test_research_delivery_live_collector import LiveCollector
from tests.test_research_delivery_live_collector import Client, T, NOW


@pytest.mark.parametrize('pointer', [
    {'sha256': value} for value in ('', False, 0, [], {}, True, 1, 'a' * 63, 'g' * 64)
] + [{}, {'sha256': None}, {'sha256': None, 'owner': ''},
     {'sha256': None, 'owner': 11111111111111111111111111111111}])
def test_corrupt_pointer_rejected_without_replacement(tmp_path, pointer):
    checkpoint = CollectionCheckpoint(EvidenceStore(tmp_path), T, 'a' * 64)
    checkpoint.directory.mkdir(parents=True, exist_ok=True)
    original = json.dumps(pointer).encode()
    checkpoint.pointer.write_bytes(original)
    with pytest.raises(ValueError, match='checkpoint_invalid'):
        checkpoint.load(NOW)
    assert checkpoint.pointer.read_bytes() == original


def test_owned_null_initialization_can_be_reclaimed(tmp_path):
    first = CollectionCheckpoint(EvidenceStore(tmp_path), T, 'a' * 64)
    assert first.load(NOW) is None
    second = CollectionCheckpoint(EvidenceStore(tmp_path), T, 'a' * 64)
    assert second.load(NOW) is None
    assert json.loads(second.pointer.read_text()) == {'sha256': None, 'owner': second.owner}


def state(tmp_path):
    LiveCollector(SimpleNamespace(), tmp_path, client=Client(3), now=NOW,
        request_interval=0, batch_size=1).collect(['dual_low'], T)
    pointer = next(tmp_path.rglob('collection-*.json'))
    return pointer.stem.removeprefix('collection-')


def test_stale_writer_cannot_erase_takeover_progress(tmp_path):
    identity = state(tmp_path)
    old = CollectionCheckpoint(EvidenceStore(tmp_path), T, identity)
    stale = old.load(NOW)
    new = CollectionCheckpoint(EvidenceStore(tmp_path), T, identity)
    current = new.load(NOW)
    # A takeover now owns this vintage; an old state must not publish again.
    current['failures']['600002'] = 'source_timeout'
    current['pending'] = ['600003']
    new.save(current, NOW)
    with pytest.raises(ValueError, match='checkpoint_superseded'):
        old.save(stale, NOW)
    restored = CollectionCheckpoint(EvidenceStore(tmp_path), T, identity).load(NOW)
    assert restored['failures'] == {'600002': 'source_timeout'}
    assert restored['pending'] == ['600003']


def test_initialization_is_also_fenced(tmp_path):
    identity = state(tmp_path / 'template')
    template = CollectionCheckpoint(EvidenceStore(tmp_path / 'template'), T, identity).load(NOW)
    first = CollectionCheckpoint(EvidenceStore(tmp_path / 'new'), T, identity)
    second = CollectionCheckpoint(EvidenceStore(tmp_path / 'new'), T, identity)
    assert first.load(NOW) is None
    assert second.load(NOW) is None
    second.save(template, NOW)
    with pytest.raises(ValueError, match='checkpoint_superseded'):
        first.save(deepcopy(template), NOW)
