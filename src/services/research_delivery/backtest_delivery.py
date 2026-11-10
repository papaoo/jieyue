"""Read-only backtest review and explicitly reviewed, durable one-attempt delivery.

No environment/model initialization, scheduled-publication authorization or outbox
recovery. A committed unknown receipt is deliberately never eligible for retry.
"""
from collections import Counter
import csv
from dataclasses import asdict
from datetime import date
from hashlib import sha256
import json
from io import StringIO
import math
from pathlib import Path
import re
from statistics import mean, median
import uuid

from sqlalchemy import select, text

from data_provider.historical_reference import digest
from src.services.screening.backtest import FIXED, SEMANTICS, add_months, evaluate_category
from src.services.screening.strategy import load_all_strategies
from src.repositories.research_delivery_models import DeliverySubscription, DeliveryDestination, DeliveryOutbox
from src.services.research_delivery.credentials import credential_fingerprint
from src.notification_sender.feishu_sender import send_once, DeliveryOutcome
from src.storage import utc_naive_now

ROOT = Path(__file__).resolve().parents[3]
FILES = {'config.json', 'data_quality.json', 'metrics.json', 'signals.csv', 'trades.csv', 'report.md'}
COMPLETE = {'completed', 'no-signal'}
METRICS = ('mean_net_return', 'median_net_return', 'positive_cohort_proportion',
           'worst_cohort', 'mean_double_friction_return', 'mean_benchmark_difference')


def registered_strategies():
    """Only bundled strategies, never an environment-selected directory."""
    return {key: asdict(value) for key, value in load_all_strategies(
        ROOT / 'src/services/screening/strategies').items()}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def number(value):
    require(type(value) in (int, float) and math.isfinite(value), 'metric_numeric_invalid')
    return value


def trade_number(value):
    try:
        return number(float(value))
    except (ValueError, TypeError, OverflowError):
        raise ValueError('trade_numeric_invalid') from None


def iso_day(value):
    try:
        require(date.fromisoformat(value).isoformat() == value, 'artifact_date_invalid')
    except (ValueError, TypeError):
        raise ValueError('artifact_date_invalid') from None


def validate_artifacts(directory):
    """Validate frozen identity, complete matrix, and every presented cohort.

    This verifies Task 3's immutable audit chain and internal result consistency;
    it does not reacquire history or make provider data independently trustworthy.
    """
    directory = Path(directory)
    try:
        manifest_bytes = (directory / 'manifest.json').read_bytes()
        manifest = json.loads(manifest_bytes)
        require(set(manifest['files']) == FILES, 'artifact_file_set_invalid')
        contents = {name: (directory / name).read_bytes() for name in FILES}
        for name in FILES:
            require(sha256(contents[name]).hexdigest() == manifest['files'][name],
                    'artifact_hash_mismatch:' + name)
        config = json.loads(contents['config.json'])
        require(digest(config) == manifest['config_hash'], 'config_hash_mismatch')
        require(manifest['semantics'] == config['semantics'] == SEMANTICS, 'semantics_mismatch')
        for name in ('dataset_hash', 'reference_hash'):
            require(re.fullmatch('[0-9a-f]{64}', manifest[name]) is not None, name + '_invalid')
        require(all(digest(config.get(key)) == digest(value) for key, value in FIXED.items()), 'fixed_config_mismatch')
        from importlib.metadata import version
        import sys
        require(config['calculation_runtime'] == dict(python=sys.version,
            **{name: version(name) for name in ('pandas', 'numpy', 'PyYAML')}), 'calculation_runtime_changed')
        if 'execution_workers' in config:
            require(type(config['execution_workers']) is int and 1 <= config['execution_workers'] <= 6,
                    'execution_workers_invalid')
        strategies = registered_strategies()
        require(len(strategies) == 10 and digest(config['strategies']) == digest(strategies), 'strategy_version_or_hash_mismatch')
        # Freeze's source closure is mandatory, including industry and scoring.
        hashes = config['implementation_hashes']
        required = {'data_provider/historical_reference.py', 'data_provider/historical_fundamentals.py',
            *['src/services/screening/' + name + '.py' for name in
              ('backtest', 'backtest_data', 'backtest_parallel', 'daily', 'eligibility', 'industry_reference', 'requirements',
               'pipeline', 'filter', 'ranker', 'risk', 'post_analysis', 'strategy', 'snapshot', 'scorer',
               'config', 'models', 'normalize', 'selection_variant')],
            *['src/services/research_delivery/' + name + '.py' for name in
              ('screening_preview', 'evidence', 'contracts', 'dependencies', 'readiness',
               'collection_checkpoint', 'live_contracts')], 'src/config.py'}
        require(set(hashes) == required, 'implementation_closure_invalid')
        for name, expected in hashes.items():
            require(sha256((ROOT / name).read_bytes()).hexdigest() == expected, 'implementation_changed:' + name)
        metrics = json.loads(contents['metrics.json'])
        quality = json.loads(contents['data_quality.json'])
        signal_reader = csv.DictReader(StringIO(contents['signals.csv'].decode('utf-8-sig')))
        trade_reader = csv.DictReader(StringIO(contents['trades.csv'].decode('utf-8-sig')))
        require(set(signal_reader.fieldnames or ()) == {'strategy', 'signal_date', 'code', 'industry_code', 'industry_taxonomy'},
                'signal_columns_invalid')
        require(set(trade_reader.fieldnames or ()) == {'strategy', 'signal_date', 'code', 'holding_sessions', 'status', 'reason',
                'entry_date', 'exit_date', 'net_return', 'double_friction_return', 'benchmark_return'}, 'trade_columns_invalid')
        signals, trades = list(signal_reader), list(trade_reader)
        for d, q in quality.items():
            if not d.startswith('_'):
                iso_day(d)
                require(q['status'] in {'valid', 'unavailable', 'incomplete'}, 'quality_status_invalid')
        expected = {(s, d, m, h) for s in strategies for d in FIXED['start_dates']
                    for m in FIXED['window_months'] for h in FIXED['holding_sessions']}
        cells = metrics['cells']
        actual = [(c['strategy'], c['start'], c['months'], c['holding_sessions']) for c in cells]
        require(len(actual) == len(set(actual)) == 320 and set(actual) == expected
                and metrics['planned_cells'] == 320, 'result_matrix_invalid')
        require(metrics['completed_cells'] == sum(c['status'] in COMPLETE for c in cells), 'completed_count_invalid')
        signal_keys = [(s['strategy'], s['signal_date'], s['code']) for s in signals]
        require(len(signal_keys) == len(set(signal_keys)), 'duplicate_signal')
        for s in signals:
            iso_day(s['signal_date'])
            require(s['strategy'] in strategies and s['industry_code'] and s['industry_taxonomy'] == 'capco_major_v1',
                    'signal_industry_missing')
            require(quality.get(s['signal_date'], {}).get('status') == 'valid', 'signal_quality_invalid')
        for t in trades:
            require(t['holding_sessions'] in {'3', '5', '10', '20'}, 'trade_holding_invalid')
            iso_day(t['signal_date'])
            for field in ('entry_date', 'exit_date'):
                if t[field]:
                    iso_day(t[field])
        trade_keys = [(t['strategy'], t['signal_date'], t['code'], int(t['holding_sessions'])) for t in trades]
        require(Counter(trade_keys) == Counter((*s, h) for s in signal_keys for h in FIXED['holding_sessions']),
                'trade_signal_coverage_invalid')
        for t in trades:
            require(t['status'] in {'filled', 'blocked_entry_cash', 'incomplete'}, 'trade_status_invalid')
        for cell in cells:
            require(cell['status'] in COMPLETE | {'incomplete', 'unavailable'}, 'cell_status_invalid')
            if cell['status'] not in COMPLETE:
                continue
            start, end = cell['start'], add_months(cell['start'], cell['months']).isoformat()
            days = sorted(d for d in quality if not d.startswith('_') and start <= d < end)
            n = cell['scheduled_cohorts']
            require(type(n) is int and n > 0 and len(days) == n and cell['signal_window_complete'] is True
                    and n == cell['valid_cohorts'] == cell['mature_cohorts'] and cell['unresolved_slots'] == 0,
                    'completed_cohort_coverage_invalid')
            require(all(quality[d]['status'] == 'valid' for d in days), 'completed_history_unavailable')
            groups = {d: [] for d in days}
            for t in trades:
                if t['strategy'] == cell['strategy'] and int(t['holding_sessions']) == cell['holding_sessions'] and t['signal_date'] in groups:
                    groups[t['signal_date']].append(t)
            returns, doubles, differences = [], [], []
            filled = blocked = selected = 0
            for rows in groups.values():
                require(len(rows) <= 5, 'cohort_slot_overflow')
                selected += len(rows)
                filled += sum(t['status'] == 'filled' for t in rows)
                blocked += sum(t['status'] == 'blocked_entry_cash' for t in rows)
                require(all(t['status'] != 'incomplete' for t in rows), 'completed_trade_incomplete')
                for t in rows:
                    for field in ('net_return', 'double_friction_return'):
                        trade_number(t[field])
                    if t['status'] == 'blocked_entry_cash':
                        require(all(trade_number(t[k]) == 0 for k in ('net_return', 'double_friction_return', 'benchmark_return')),
                                'blocked_cash_not_zero')
                    else:
                        require(t['entry_date'] and t['exit_date'] and t['entry_date'] > t['signal_date']
                                and t['exit_date'] >= t['entry_date'], 'trade_dates_invalid')
                net = sum(trade_number(t['net_return']) for t in rows) / 5
                returns.append(net)
                doubles.append(sum(trade_number(t['double_friction_return']) for t in rows) / 5)
                differences.append(net - sum(trade_number(t['benchmark_return']) for t in rows) / 5
                    if all(t['benchmark_return'] != '' for t in rows) else None)
            recomputed = (mean(returns), median(returns), mean(v > 0 for v in returns), min(returns),
                          mean(doubles), mean(differences) if all(d is not None for d in differences) else None)
            for key, value in zip(METRICS, recomputed):
                require(cell[key] is None if value is None else
                        math.isclose(number(cell[key]), value, rel_tol=1e-9, abs_tol=1e-12), 'metric_inconsistent:' + key)
            require(blocked == cell['blocked_slots'] and (cell['status'] != 'no-signal' or selected == 0),
                    'execution_counts_inconsistent')
            cell.update(executed_slots=filled, selected_slots=selected)
        return dict(manifest_hash=sha256(manifest_bytes).hexdigest(),
                    config=config, cells=cells, quality=quality, trades=trades)
    except (OSError, KeyError, IndexError, AttributeError, TypeError, UnicodeError, json.JSONDecodeError, csv.Error, OverflowError) as exc:
        raise ValueError('artifact_missing_or_invalid:' + type(exc).__name__) from None


def summarize(artifact, strategy):
    cells = [c for c in artifact['cells'] if c['strategy'] == strategy]
    errors = sorted({c.get('reason') or 'historical_prerequisite_unavailable' for c in cells if c['status'] == 'unavailable'})
    errors += sorted(set(artifact['quality'].get('_planned_calendar_errors', {}).values()))
    errors += sorted({q.get('reason') or 'historical_prerequisite_unavailable'
        for d, q in artifact['quality'].items() if not d.startswith('_') and q['status'] == 'unavailable'})
    errors += sorted({t['reason'] for t in artifact['trades'] if t['strategy'] == strategy
        and t['status'] == 'incomplete' and t['reason'] not in
        {'entry_not_yet_available', 'holding_outcome_immature', 'blocked_exit_unresolved_at_cutoff'}})
    if errors:
        raise ValueError('historical_gap:' + strategy + ':' + ','.join(errors))
    done = [c for c in cells if c['status'] in COMPLETE]
    require(done, 'no_completed_cells:' + strategy)
    strategy_config = artifact['config']['strategies'][strategy]
    pending = len(cells) - len(done)
    selected = sum(c['selected_slots'] for c in done)
    executed = sum(c['executed_slots'] for c in done)
    blocked = sum(c['blocked_slots'] for c in done)
    lines = [f"{strategy_config['display_name']} v{strategy_config['version']}",
        f"完成 {len(done)}/32格 · 待成熟 {pending}格",
        '起点：2026-01-05、04-01、07-01、09-01，各1/3个月。',
        'D收盘选股→D+1开盘买入；第H个交易日收盘卖出，卖出受阻顺延。',
        '5份等权（各20%），未选或买入受阻留现金。',
        '假设费用：买入0.08%、卖出0.13%。',
        '以下为各完整格的平均收益范围：']
    def span(rows, key):
        values = [r[key] for r in rows if r[key] is not None]
        return f'{min(values):.2%}～{max(values):.2%}' if values else '不可比较'
    for holding in FIXED['holding_sessions']:
        rows = [c for c in done if c['holding_sessions'] == holding]
        comparable = sum(c['mean_benchmark_difference'] is not None for c in rows)
        missing = f'；{len(rows) - comparable}格缺基准' if comparable < len(rows) else ''
        lines += ['', f'持有{holding}个交易日 · 完整{len(rows)}格',
            f"净收益 {span(rows, 'mean_net_return')}；双成本 {span(rows, 'mean_double_friction_return')}",
            f"相对沪深300 {span(rows, 'mean_benchmark_difference')}（可比/完整 {comparable}/{len(rows)}{missing}）"]
    lines.append('')
    if executed == 0:
        lines.append(f'评价：没有可执行信号；成交0次，现金收益0。选中{selected}次，买入受阻{blocked}次（含重叠窗口重复）。')
    else:
        labels = {'insufficient evidence': '可比较证据不足', 'positive in completed windows': '完整窗口均正',
                  'negative in completed windows': '完整窗口均负', 'mixed across completed windows': '完整窗口混合'}
        lines.append('评价：' + labels[evaluate_category(done)] + f'；成交{executed}次，买入受阻{blocked}次（含重叠窗口重复）。')
    lines += [('下一步：等剩余窗口成熟，再看结果是否一致。' if pending else
               '下一步：继续跟踪新信号，比较后续表现。'),
        '当前规则历史回放；窗口有重叠，基准为沪深300价格指数。']
    return '\n'.join(lines)


class BacktestDelivery:
    def __init__(self, repository, vault=None, *, sender=send_once):
        self.repository, self.vault, self.sender = repository, vault, sender

    def _review(self, directory, session):
        artifact = validate_artifacts(directory)
        cards, blockers = [], []
        strategies = artifact['config']['strategies']
        active = list(session.scalars(select(DeliverySubscription).where(DeliverySubscription.enabled.is_(True))))
        subscriptions = [s for s in active if s.strategy_id in strategies]
        counts = Counter(s.strategy_id for s in subscriptions)
        destinations = Counter(s.destination_id for s in active)
        for sub in sorted(subscriptions, key=lambda s: s.strategy_id):
            strategy = sub.strategy_id
            target = session.get(DeliveryDestination, sub.destination_id)
            reason = None
            if counts[strategy] != 1 or destinations[sub.destination_id] != 1:
                reason = 'route_ambiguous:' + strategy
            elif target is None or not target.enabled or not target.credential_ref or target.kind != 'strategy':
                reason = 'destination_unavailable:' + strategy
            if reason:
                blockers.append(reason)
                continue
            try:
                content = summarize(artifact, strategy)
            except ValueError as error:
                blockers.append(str(error))
                continue
            payload = dict(msg_type='interactive', card=dict(config=dict(wide_screen_mode=True),
                header=dict(title=dict(tag='plain_text', content='阶跃 · 策略固定回放')),
                elements=[dict(tag='div', text=dict(tag='lark_md', content=(target.keyword + '\n' if target.keyword else '') + content))]))
            binding = dict(subscription_id=sub.id, subscription_version=sub.version, destination_version=target.version,
                credential_ref=target.credential_ref, credential_fingerprint=target.credential_fingerprint,
                subscription_name=sub.name, destination_alias=target.alias, signing_configured=target.signing_configured,
                strategy_hash=digest(strategies[strategy]))
            cards.append(dict(strategy_id=strategy, destination_id=target.id, payload=payload, binding=binding))
        review = dict(manifest_hash=artifact['manifest_hash'], cards=cards, blockers=sorted(set(blockers)))
        return review | dict(review_hash=digest(review))

    def preview(self, directory):
        with self.repository.session_factory() as session:
            session.execute(text('BEGIN'))
            try:
                review = self._review(directory, session)
            except ValueError as error:
                return dict(cards=[], blockers=[str(error)], review_hash=None)
        # References/fingerprints are internal approval bindings, not preview output.
        return {**review, 'cards': [{k: v for k, v in c.items() if k != 'binding'} for c in review['cards']]}

    def send(self, directory, expected_review_hash):
        with self.repository.session_factory() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            review = self._review(directory, session)
            require(review['review_hash'] == expected_review_hash, 'review_changed')
            # Any unresolved route/history gap blocks this reviewed batch.
            require(not review['blockers'] and review['cards'], 'backtest_delivery_blocked')
        results = []
        for card in review['cards']:
            key = 'backtest:' + digest([review['manifest_hash'], card['strategy_id'], card['destination_id']])
            with self.repository.session_factory() as session:
                session.execute(text('BEGIN IMMEDIATE'))
                existing = session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.request_id == key))
                if existing is not None:
                    results.append(dict(strategy_id=card['strategy_id'], status=existing.status))
                    continue
                row = DeliveryOutbox(id=uuid.uuid4().hex, request_id=key, kind='backtest',
                    destination_id=card['destination_id'], destination_version=card['binding']['destination_version'],
                    credential_ref=card['binding']['credential_ref'], payload_json=json.dumps(card, ensure_ascii=False),
                    payload_hash=digest(card), status='delivery_unknown', error_code='claimed_before_transport',
                    attempt_count=1, expires_at=utc_naive_now(), sending_at=utc_naive_now())
                session.add(row)
                row_id = row.id
                session.commit()  # Durable once-only claim BEFORE resolving secrets or transport.
            with self.repository.session_factory() as session:
                session.execute(text('BEGIN IMMEDIATE'))
                row = session.get(DeliveryOutbox, row_id)
                try:
                    current = self._review(directory, session)
                    require(current['review_hash'] == expected_review_hash, 'configuration_changed')
                    require(digest(json.loads(row.payload_json)) == row.payload_hash == digest(card), 'payload_changed')
                    credentials = self.vault.resolve(card['binding']['credential_ref'])
                    require(credential_fingerprint(credentials.webhook_url) == card['binding']['credential_fingerprint'],
                            'credential_binding_changed')
                    target = session.get(DeliveryDestination, card['destination_id'])
                    require(bool(credentials.signing_secret) == bool(target.signing_configured), 'signing_binding_changed')
                except Exception:
                    outcome = DeliveryOutcome('rejected', 'binding_or_artifact_changed')
                else:
                    try:
                        outcome = self.sender(credentials, card['payload'])
                        if not isinstance(outcome, DeliveryOutcome) or outcome.status not in {'sent', 'rejected', 'delivery_unknown'}:
                            outcome = DeliveryOutcome('delivery_unknown', 'invalid_transport_outcome')
                    except Exception:
                        outcome = DeliveryOutcome('delivery_unknown', 'transport_exception')
                row.status = outcome.status
                # Only fixed internal codes; never persist a custom transport exception/message.
                row.error_code = None if outcome.status == 'sent' else ('provider_rejected' if outcome.status == 'rejected' else 'response_unknown')
                row.completed_at = utc_naive_now()
                session.commit()
                results.append(dict(strategy_id=card['strategy_id'], status=outcome.status))
        return results
