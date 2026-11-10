"""Full denominator and real frozen-vintage resume regressions (no providers)."""
from datetime import timedelta
from types import SimpleNamespace
import pandas as pd
import pytest

from tests.test_research_delivery_live_collector import LiveCollector
from src.services.research_delivery.screening_preview import preview_strategy
from tests.test_research_delivery_live_collector import Client, T, NOW
from tests.test_research_delivery_preview import verified_snapshot, history, T as PREVIEW_DAY


def test_all_150_cells_required_and_full_pool_budget_invariant():
    codes = tuple(f'{600001+i:06d}' for i in range(150))
    frame = verified_snapshot(codes)
    histories = {code: history(code) for code in codes}
    for code, bars in histories.items():
        if code != codes[119]:
            close = pd.Series([12. - i / 30 for i in range(61)])
            bars['open'], bars['close'], bars['high'], bars['low'] = close, close, close + .1, close - .1
    def run(budget, data):
        return preview_strategy('momentum_quality', frame, trade_date=PREVIEW_DAY, universe=codes,
            calendar_verified=True, universe_verified=True, daily_histories=data, technical_limit=budget)
    partial = run(100, dict(list(histories.items())[:100]))
    assert len(partial.scope.technical_planned) == 150
    assert partial.scope.technical_planned[119] == codes[119]
    assert partial.result is None and partial.readiness.state == 'blocked'
    small, large = run(1, histories), run(1000, histories)
    assert small.result is not None and large.result is not None
    assert [(p.code, p.final_score) for p in small.result.picks] == [(p.code, p.final_score) for p in large.result.picks]
    assert codes[119] in [p.code for p in small.result.picks[:5]]


def test_real_checkpoint_resumes_without_recollecting_market(tmp_path):
    client = Client(150)
    def collector(now):
        return LiveCollector(SimpleNamespace(), tmp_path, client=client, now=now, request_interval=0)
    first = collector(NOW).collect(['dual_low'], T)['dual_low']
    assert first.state == 'blocked' and 'collection_pending' in first.blockers
    assert first.coverage['contract_version'] == 2
    assert first.coverage['pending_count'] == 50 and first.coverage['evaluated_count'] == 100
    market_calls = len([c for c in client.calls if 'ts_code' not in c[1]])
    second = collector(NOW + timedelta(minutes=10)).collect(['dual_low'], T)['dual_low']
    assert second.state == 'degraded' and second.coverage['pending_count'] == 0
    assert second.coverage['evaluated_count'] == second.coverage['evaluation_scope_count'] == 150
    assert len([c for c in client.calls if 'ts_code' not in c[1]]) == market_calls
    assert len([c for c in client.calls if c[0] == 'daily' and 'ts_code' in c[1]]) == 150
    assert first.coverage['scope_hash'] == second.coverage['scope_hash']
    assert {e['sha256'] for e in first.evidence} <= {e['sha256'] for e in second.evidence}
    baseline = LiveCollector(SimpleNamespace(), tmp_path / 'whole', client=Client(150), now=NOW,
        request_interval=0, batch_size=1000).collect(['dual_low'], T)['dual_low']
    assert second.result['picks'] == baseline.result['picks']


def test_completed_no_pick_is_not_failure(tmp_path):
    class NoPick(Client):
        def query(self, endpoint, **kwargs):
            frame = super().query(endpoint, **kwargs)
            if endpoint == 'daily' and 'ts_code' not in kwargs:
                frame['amount'] = 0.
            return frame
    result = LiveCollector(SimpleNamespace(), tmp_path, client=NoPick(), now=NOW,
        request_interval=0).collect(['dual_low'], T)['dual_low']
    assert result.state == 'degraded' and not result.blockers
    assert result.result['picks'] == []
    assert result.coverage['evaluation_scope_count'] == result.coverage['evaluated_count'] == 0


def test_checkpoint_tampering_blocks_before_any_provider_call(tmp_path):
    import json
    client = Client(3)
    LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW, request_interval=0,
        batch_size=1).collect(['dual_low'], T)
    pointer = next(tmp_path.rglob('collection-*.json'))
    identifier = json.loads(pointer.read_text())['sha256']
    record = pointer.parent / (identifier + '.json')
    record.write_text('{}')
    client.calls.clear()
    result = LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW,
        request_interval=0).collect(['dual_low'], T)['dual_low']
    assert result.blockers == ['checkpoint_invalid'] and not client.calls


def test_subset_of_original_strategy_union_reuses_vintage_and_histories(tmp_path):
    client = Client(3)
    first = LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW, request_interval=0,
        batch_size=1).collect(['dual_low', 'momentum_quality'], T)
    initial = len([c for c in client.calls if 'ts_code' not in c[1]])
    result = LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW + timedelta(minutes=1),
        request_interval=0).collect(['momentum_quality'], T)['momentum_quality']
    assert result.state == 'degraded'
    assert len([c for c in client.calls if 'ts_code' not in c[1]]) == initial
    assert len([c for c in client.calls if c[0] == 'daily' and 'ts_code' in c[1]]) == 3
    assert first['momentum_quality'].coverage['scope_hash'] == result.coverage['scope_hash']


def test_verified_recent_ipo_excluded_not_failed_but_seasoned_short_history_fails(tmp_path):
    class Recent(Client):
        def query(self, endpoint, **kwargs):
            frame = super().query(endpoint, **kwargs)
            if endpoint == 'stock_basic':
                frame.loc[0, 'list_date'] = '20261001'
            return frame
    client = Recent()
    result = LiveCollector(SimpleNamespace(), tmp_path, client=client, now=NOW,
        request_interval=0).collect(['dual_low'], T)['dual_low']
    assert result.state == 'degraded' and result.coverage['failed_count'] == 0
    assert result.coverage['ipo_ineligible_count'] == 1
    assert result.coverage['excluded'][0]['reasons'] == ['ipo_fewer_than_61_sessions']
    proof = result.coverage['listing_eligibility']['600001']
    assert proof['observed_sessions'] < 61 and proof['calendar_evidence_id'] and proof['listing_evidence_id']
    assert all(c[1].get('ts_code') != '600001.SH' for c in client.calls)
    client = Client()
    client.damage = 'short'
    result = LiveCollector(SimpleNamespace(), tmp_path / 'seasoned', client=client, now=NOW,
        request_interval=0).collect(['dual_low'], T)['dual_low']
    assert result.coverage['failed_count'] == 2 and result.coverage['ipo_ineligible_count'] == 0


def test_v2_publisher_rejects_pending_hash_and_evidence_count_forgery(tmp_path):
    from copy import deepcopy
    from src.services.research_delivery.formal_publisher import FormalPublisher
    from src.repositories.research_delivery_repository import ManagementError
    source = LiveCollector(SimpleNamespace(), tmp_path, client=Client(), now=NOW,
        request_interval=0, batch_size=500).collect(['dual_low'], T)['dual_low'].to_dict()
    FormalPublisher._coverage(source)
    FormalPublisher._evidence(source, NOW)
    for change in ({'pending_count': 1}, {'failed_count': 1}, {'scope_hash': '0' * 64},
                   {'evaluated_count': 1}, {'daily_evidence': {}}):
        broken = deepcopy(source)
        broken['coverage'].update(change)
        with pytest.raises(ManagementError):
            FormalPublisher._coverage(broken)


def test_publisher_rejects_coherent_subset_even_with_rehashed_claim(tmp_path):
    from copy import deepcopy
    from src.services.research_delivery.collection_checkpoint import digest
    from src.services.research_delivery.formal_publisher import FormalPublisher
    from src.repositories.research_delivery_repository import ManagementError
    source = LiveCollector(SimpleNamespace(), tmp_path, client=Client(3), now=NOW,
        request_interval=0).collect(['dual_low'], T)['dual_low'].to_dict()
    broken = deepcopy(source)
    coverage = broken['coverage']
    removed = coverage['evaluation_scope']['codes'].pop()
    coverage['evaluated_codes'].remove(removed)
    coverage['daily_evidence'].pop(removed)
    for name in ('evaluation_scope_count', 'evaluated_count', 'technical_planned_count', 'snapshot_pass_count'):
        coverage[name] -= 1
    coverage['scope_hash'] = digest(coverage['evaluation_scope'])
    broken['result']['picks'] = [p for p in broken['result']['picks'] if p['code'] != removed]
    with pytest.raises(ManagementError):
        FormalPublisher._coverage(broken)
        FormalPublisher._evidence(broken, NOW)
    for key in ('strategy_hash', 'pool_hash'):
        broken = deepcopy(source)
        broken['coverage']['evaluation_scope'][key] = '0' * 64
        broken['coverage']['scope_hash'] = digest(broken['coverage']['evaluation_scope'])
        with pytest.raises(ManagementError):
            FormalPublisher._coverage(broken)
            FormalPublisher._evidence(broken, NOW)
