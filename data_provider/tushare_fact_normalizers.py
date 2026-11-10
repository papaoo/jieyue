"""Pure, scoped normalization; derived facts never masquerade as vendor indices."""
from __future__ import annotations

from datetime import date, timedelta
import math

import pandas as pd


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def iso_date(value):
    stamp = pd.to_datetime(str(value), errors='coerce')
    return None if pd.isna(stamp) else stamp.date().isoformat()


def dividend_facts(frame, cutoff):
    """365-calendar-day implemented cash events; keep proposal evidence separate."""
    since = (date.fromisoformat(cutoff) - timedelta(days=365)).isoformat()
    events = []
    invalid_disclosures = int(frame.attrs.get('unusable_disclosure_rows', 0))
    for row in frame.to_dict('records'):
        announced = iso_date(row.get('ann_date'))
        implemented = iso_date(row.get('imp_ann_date'))
        if not announced:
            invalid_disclosures += 1
            continue
        if announced > cutoff or (implemented and implemented > cutoff):
            continue
        events.append({
            'report_date': iso_date(row.get('end_date')), 'announcement_date': announced,
            'implementation_announcement_date': implemented,
            'stage': str(row.get('div_proc') or ''),
            'cash_dividend_per_share': number(row.get('cash_div_tax')),
            'stock_dividend_per_share': number(row.get('stk_div')),
            'ex_dividend_date': iso_date(row.get('ex_date')),
            'record_date': iso_date(row.get('record_date')), 'pay_date': iso_date(row.get('pay_date')),
        })
    events.sort(key=lambda e: (e['implementation_announcement_date'] or e['announcement_date'], e['report_date'] or ''), reverse=True)
    # An implementation supersedes exact duplicate rows, but never a distinct
    # ex-date (a company may distribute more than once for the same period).
    unique, seen = [], set()
    for event in events:
        key = (event['report_date'], event['stage'], event['ex_dividend_date'])
        if key not in seen:
            unique.append(event)
            seen.add(key)
    paid = [e for e in unique if e['stage'] == '实施' and e['ex_dividend_date']
            and since < e['ex_dividend_date'] <= cutoff]
    missing = [e for e in unique if e['stage'] == '实施' and not e['ex_dividend_date']
               and e['announcement_date'] > since]
    amounts = [e['cash_dividend_per_share'] for e in paid]
    cash = None if invalid_disclosures or missing or any(v is None for v in amounts) else sum(amounts)
    complete = not invalid_disclosures and not missing and None not in amounts
    return {'events': unique[:8], 'event_count': len(unique), 'as_of_events': cutoff,
            'window_start_exclusive': since, 'window_end': cutoff,
            'ttm_cash_dividend_per_share': cash, 'implemented_event_count_365d': len(paid),
            'ttm_event_count': len(paid) if complete else None,
            'latest_implemented_ex_date': max((e['ex_dividend_date'] for e in unique
                if e['stage'] == '实施' and e['ex_dividend_date'] and e['ex_dividend_date'] <= cutoff), default=None),
            'cash_unit': 'CNY/share_before_tax', 'events_source': 'tushare:dividend',
            'coverage': 'disclosed_events', 'events_complete': complete,
            'unusable_disclosure_rows': invalid_disclosures,
            'definition_events': 'Implemented unique ex-date cash events in preceding 365 days; proposals excluded; not provider dv_ttm.'}


def holder_facts(frame):
    """Compare observed same-name holders only. Entries/exits are NOT buys/sells."""
    required = {'end_date', 'ann_date', 'holder_name', 'hold_amount'}
    if frame.empty or not required.issubset(frame.columns):
        return {}
    frame = frame.copy()
    frame['_period'] = frame.end_date.map(iso_date)
    frame['_ann'] = frame.ann_date.map(iso_date)
    frame = frame.dropna(subset=['_period', '_ann', 'holder_name'])
    frame = frame.sort_values(['_period', '_ann'], ascending=False).drop_duplicates(['_period', 'holder_name'])
    periods = sorted(frame._period.unique(), reverse=True)
    if not periods:
        return {}
    latest = frame.loc[frame._period.eq(periods[0])].head(10)
    previous = frame.loc[frame._period.eq(periods[1])].head(10) if len(periods) > 1 else frame.iloc[:0]
    prev = {r['holder_name']: r for r in previous.to_dict('records')}
    now = {r['holder_name']: r for r in latest.to_dict('records')}
    comparable = []
    for name, row in now.items():
        if name not in prev:
            continue
        current, old = number(row.get('hold_amount')), number(prev[name].get('hold_amount'))
        if current is None or old is None:
            continue
        kind = str(row.get('holder_type') or '')
        custodian = any(token in name for token in ('中央结算', '结算公司', 'HKSCC', '名义持有人'))
        institution = any(token in kind for token in ('基金', '保险', '社保', 'QFII', '券商', '证券公司', '投资公司', '信托', '资管', '资产管理')) and not custodian
        comparable.append({'holder_name': name, 'holder_type': kind, 'current_shares': current,
                           'previous_shares': old, 'change_shares': current-old,
                           'financial_institution': institution, 'custodian': custodian})
    inst = [row for row in comparable if row['financial_institution']]
    # A lack of comparable institutions is unknown, not a zero position change.
    change = sum(r['change_shares'] for r in inst) if inst else None
    import json
    raw = json.loads(latest.drop(columns=['_period', '_ann']).to_json(orient='records', force_ascii=False))
    return {'report_date': periods[0], 'previous_report_date': periods[1] if len(periods) > 1 else None,
            'announcement_date': latest._ann.max(), 'top10_float_holders': raw,
            'institution_holding_change': None, 'top10_holder_change': None,
            'comparable_holder_changes': comparable,
            'new_to_top10': sorted(set(now)-set(prev)) if prev else [],
            'absent_from_top10': sorted(set(prev)-set(now)),
            'comparable_institution_change': {
                'change_shares': change, 'compared_holder_count': len(inst),
                'scope': 'same_named_financial_institutions_in_two_disclosed_top10_float_lists',
                'is_transaction_flow': False, 'unit': 'shares',
                'definition': 'Unadjusted disclosed share-count delta; excludes entrants, exits, custodians and unclassified holders. Corporate actions may change shares without trading.'},
            'coverage': 'top10_float_holders_only', 'full_institution_coverage': False}


def industry_facts(basics, daily, flows, trade_date):
    """Current membership, same-date observed traded universe; not backtest data."""
    required = {'ts_code', 'industry'}
    if not required.issubset(basics.columns) or not {'ts_code', 'trade_date', 'pct_chg'}.issubset(daily.columns):
        return {}, {}
    members = basics[list(required)].drop_duplicates('ts_code').copy()
    members['industry'] = members.industry.fillna('').astype(str).str.strip()
    bars = daily.loc[daily.trade_date.astype(str).eq(trade_date)].drop_duplicates('ts_code').copy()
    bars['pct_chg'] = pd.to_numeric(bars.pct_chg, errors='coerce').map(number)
    universe = bars.merge(members, on='ts_code', how='left', validate='one_to_one')
    mapped = universe.loc[universe.industry.fillna('').ne('')].copy()
    prices = []
    fund_rows = []
    flow_fields = ['buy_lg_amount', 'buy_elg_amount', 'sell_lg_amount', 'sell_elg_amount']
    if {'ts_code', 'trade_date', *flow_fields}.issubset(flows.columns):
        cash = flows.loc[flows.trade_date.astype(str).eq(trade_date)].drop_duplicates('ts_code').copy()
        for field in flow_fields:
            cash[field] = cash[field].map(number)
        cash['main_net_inflow'] = (cash.buy_lg_amount + cash.buy_elg_amount - cash.sell_lg_amount - cash.sell_elg_amount)*10000
        mapped = mapped.merge(cash[['ts_code', 'main_net_inflow']], on='ts_code', how='left', validate='one_to_one')
    else:
        mapped['main_net_inflow'] = float('nan')
    for name, group in mapped.groupby('industry', sort=True):
        returns = group.pct_chg.dropna()
        values = group.main_net_inflow.dropna()
        common = {'name': name, 'as_of': iso_date(trade_date), 'population_count': len(group)}
        if len(returns):
            prices.append({**common, 'change_pct': float(returns.mean()), 'covered_count': len(returns),
                           'coverage_ratio': len(returns)/len(group)})
        if len(values):
            fund_rows.append({**common, 'main_net_inflow': float(values.sum()), 'covered_count': len(values),
                              'coverage_ratio': len(values)/len(group), 'unit': 'CNY'})
    base = {'as_of': iso_date(trade_date), 'membership_basis': 'current_tushare_stock_basic_industry',
            'membership_is_point_in_time': False, 'population_basis': 'observed_daily_traded_stocks',
            'traded_count': len(bars), 'mapped_count': len(mapped), 'source': 'computed_from_tushare',
            'rejected_daily_rows': int((~daily.trade_date.astype(str).eq(trade_date)).sum()),
            'unmapped_codes': sorted(universe.loc[universe.industry.fillna('').eq(''), 'ts_code'].astype(str).tolist()),
            'definition_warning': 'Own aggregate, not Eastmoney/THS/SW index or money-flow series.'}
    def ranking(rows, metric, definition):
        if not rows:
            return {}
        rows.sort(key=lambda r: (r[metric], r['name']), reverse=True)
        for rank, row in enumerate(rows, 1):
            row['rank'] = rank
        return {**base, 'definition': definition, 'sector_count': len(rows),
                'top': rows[:10], 'bottom': list(reversed(rows[-10:])), 'all': rows,
                'coverage_complete': base['rejected_daily_rows'] == 0 and len(mapped) == len(bars) and sum(r['covered_count'] for r in rows) == len(bars)}
    return (ranking(prices, 'change_pct', 'equal_weight_return_of_observed_traded_constituents'),
            ranking(fund_rows, 'main_net_inflow', 'sum_large_plus_extra_large_buy_minus_sell_CNY'))


def dragon_tiger_facts(ts_code, sessions, tables):
    checked, missing, records = [], [], []
    for day in sessions:
        table = tables.get(day)
        # An empty whole-market table might mean publication delay or outage.
        if table is None or table.empty or not {'ts_code', 'trade_date', 'reason'}.issubset(table.columns):
            missing.append(iso_date(day))
            continue
        daily = table.loc[table.trade_date.astype(str).eq(day)]
        if daily.empty:
            missing.append(iso_date(day))
            continue
        checked.append(iso_date(day))
        for row in daily.loc[daily.ts_code.eq(ts_code)].to_dict('records'):
            records.append({'trade_date': iso_date(day), 'reason': str(row.get('reason') or ''),
                            'net_amount': number(row.get('net_amount')), 'unit': 'CNY'})
    records = list({(r['trade_date'], r['reason']): r for r in records}.values())
    dates = sorted({r['trade_date'] for r in records}, reverse=True)
    complete = bool(sessions) and not missing
    return {'is_on_list': True if records else False if complete else None,
            'recent_count': len(dates) if complete or records else None,
            'observed_session_count': len(dates), 'event_row_count': len(records),
            'latest_date': dates[0] if dates else None, 'events': records[:20],
            'checked_trade_date': checked[0] if checked else None,
            'lookback_sessions': len(sessions), 'checked_sessions': checked, 'missing_sessions': missing,
            'coverage_complete': complete,
            'definition': 'Unique trading sessions with observed listing; multiple reasons are not separate sessions and amounts must not be summed across reasons.'}
