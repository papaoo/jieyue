"""Dated A-share facts using the already configured Tushare permissions.

Every block is independent: a denied/empty endpoint must not discard successful
facts from other endpoints. Values are not synthesized from an LLM or zero-filled.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import time
from typing import Callable

import pandas as pd
from .tushare_fact_normalizers import dividend_facts, holder_facts, industry_facts, dragon_tiger_facts


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _date(value):
    if value is None or str(value).lower() in {'none', 'nan', 'nat', ''}:
        return None
    stamp = pd.to_datetime(str(value), errors='coerce')
    return None if pd.isna(stamp) else stamp.date().isoformat()


class TushareFundamentalAdapter:
    def __init__(self, query: Callable):
        self.query = query

    def fetch(self, stock_code: str, *, budget_seconds: float, as_of: str | None = None,
              on_block: Callable | None = None) -> dict:
        today = date.fromisoformat(as_of) if as_of else date.today()
        cutoff = today.isoformat()
        code = stock_code.split('.')[0]
        ts_code = stock_code if '.' in stock_code else code + ('.SH' if code.startswith('6') else '.BJ' if code.startswith(('4', '8', '92')) else '.SZ')
        deadline = time.monotonic() + max(0, budget_seconds)
        fetched_at = datetime.now().astimezone().isoformat()
        traces, errors = {}, {}

        class Blocks(dict):
            def __setitem__(self, name, value):
                super().__setitem__(name, value)
                if on_block is not None:
                    on_block(name, value)

        result = Blocks()

        def fetch(api, **params):
            started = time.monotonic()
            remaining = deadline - started
            try:
                if remaining <= 0:
                    raise TimeoutError('fundamental stage budget exhausted')
                frame = self.query(api, ts_code=ts_code, request_timeout=min(remaining, 5), **params)
                if not isinstance(frame, pd.DataFrame):
                    raise ValueError('malformed table')
                if api == 'dividend' and not {'ts_code', 'ann_date', 'end_date', 'div_proc', 'cash_div_tax', 'ex_date'}.issubset(frame.columns):
                    raise ValueError('missing dividend schema')
                if not frame.empty:
                    if 'ts_code' not in frame:
                        raise ValueError('missing company identity')
                    frame = frame.loc[frame.ts_code.astype(str).eq(ts_code)].copy()
                    if frame.empty:
                        raise ValueError('response does not match company identity')
                    for column in ('ann_date', 'f_ann_date', 'end_date', 'trade_date'):
                        if column in frame:
                            dates = frame[column].map(_date)
                            if api == 'dividend' and column == 'ann_date':
                                frame.attrs['unusable_disclosure_rows'] = int(dates.isna().sum())
                            # Missing f_ann_date may fall back to ann_date; a
                            # missing primary publication/session date cannot.
                            allowed = dates.le(cutoff)
                            if column == 'f_ann_date':
                                allowed |= dates.isna()
                            frame = frame.loc[allowed]
                    sort = [c for c in ('end_date', 'trade_date', 'f_ann_date', 'ann_date') if c in frame]
                    if sort:
                        frame = frame.sort_values(sort, ascending=False, kind='stable')
                    if api in {'daily_basic', 'moneyflow'} and 'trade_date' in frame:
                        frame = frame.drop_duplicates('trade_date', keep='first')
                status = 'ok' if not frame.empty or api in {'top_list', 'dividend'} else 'empty'
                if status == 'empty':
                    errors[api] = 'no usable disclosed records'
                traces[api] = {'provider': f'tushare:{api}', 'result': status,
                               'duration_ms': int((time.monotonic()-started)*1000), 'record_count': len(frame)}
                return frame
            except Exception as exc:
                # Do not persist server text which could contain authentication.
                errors[api] = f'{type(exc).__name__}: request failed or permission unavailable'
                traces[api] = {'provider': f'tushare:{api}', 'result': 'failed',
                               'duration_ms': int((time.monotonic()-started)*1000), 'record_count': 0}
                return pd.DataFrame()

        def first(frame):
            return frame.iloc[0].to_dict() if not frame.empty else {}

        def block(data, apis, *, complete=True, missing=()):
            block_errors = [f'{api}: {errors[api]}' for api in apis if api in errors]
            status = 'ok' if data and complete and not block_errors and not missing else 'partial' if data else 'failed'
            return {'status': status, 'data': data, 'source_chain': [traces[a] for a in apis if a in traces],
                    'errors': block_errors, 'missing_fields': list(missing), 'source': 'tushare',
                    'source_url': 'https://tushare.pro/document/2', 'fetched_at': fetched_at}

        basic = first(fetch('stock_basic', fields='ts_code,name,industry,list_date', limit=1))
        latest = first(fetch('daily_basic', start_date=(today-timedelta(days=35)).strftime('%Y%m%d'),
                             end_date=today.strftime('%Y%m%d'), limit=10,
                             fields='ts_code,trade_date,close,pe_ttm,pb,total_mv,circ_mv,dv_ttm'))
        valuation = {k: _number(latest.get(v)) for k, v in {'pe_ratio':'pe_ttm', 'pb_ratio':'pb', 'total_mv':'total_mv', 'circ_mv':'circ_mv', 'dividend_yield_ttm_pct':'dv_ttm'}.items()}
        for field in ('total_mv', 'circ_mv'):
            if valuation[field] is not None:
                valuation[field] *= 10000
        if latest:
            valuation.update(as_of=_date(latest.get('trade_date')), price_basis='daily_close', currency='CNY')
        result['valuation'] = block(valuation if latest else {}, ['daily_basic'],
                                    missing=[k for k in ('pe_ratio','pb_ratio','total_mv','circ_mv') if valuation[k] is None])
        industry = str(basic.get('industry') or '').strip()
        result['boards'] = block({'industry': industry, 'belong_boards': [{'name': industry, 'type': 'industry', 'source': 'tushare:stock_basic'}]} if industry else {},
                                 ['stock_basic'], complete=False, missing=['sector_rankings'])

        indicator = first(fetch('fina_indicator', limit=10,
                                fields='ts_code,ann_date,end_date,roe,tr_yoy,or_yoy,netprofit_yoy,grossprofit_margin'))
        period = str(indicator.get('end_date') or '')
        growth = {k: _number(indicator.get(v)) for k, v in {'revenue_yoy':'tr_yoy', 'net_profit_yoy':'netprofit_yoy', 'roe':'roe', 'gross_margin':'grossprofit_margin'}.items()}
        if growth['revenue_yoy'] is None:
            growth['revenue_yoy'] = _number(indicator.get('or_yoy'))
        if indicator:
            growth.update(report_date=_date(period), announcement_date=_date(indicator.get('ann_date')))
        result['growth'] = block(growth if indicator else {}, ['fina_indicator'],
                                 missing=[k for k in ('revenue_yoy','net_profit_yoy','roe') if growth[k] is None])

        # Anchor financial statements to the indicator period; otherwise select
        # income's latest disclosed period and require cash flow to match it.
        income = first(fetch('income', **({'period': period} if period else {}), report_type='1', limit=10,
                             fields='ts_code,ann_date,f_ann_date,end_date,total_revenue,revenue,n_income_attr_p'))
        period = period or str(income.get('end_date') or '')
        cash = first(fetch('cashflow', **({'period': period} if period else {}), report_type='1', limit=10,
                           fields='ts_code,ann_date,f_ann_date,end_date,n_cashflow_act'))
        period = period or str(cash.get('end_date') or '')
        income = income if str(income.get('end_date') or '') == period else {}
        cash = cash if str(cash.get('end_date') or '') == period else {}
        report = {'report_date': _date(period), 'revenue': _number(income.get('total_revenue')),
                  'net_profit_parent': _number(income.get('n_income_attr_p')),
                  'operating_cash_flow': _number(cash.get('n_cashflow_act')),
                  'roe': growth['roe'] if str(indicator.get('end_date') or '') == period else None,
                  'announcement_date': max(filter(None, [_date(income.get('ann_date')), _date(cash.get('ann_date'))]), default=None),
                  'currency': 'CNY'}
        if report['revenue'] is None:
            report['revenue'] = _number(income.get('revenue'))
        missing = [k for k in ('revenue', 'net_profit_parent', 'operating_cash_flow') if report[k] is None]
        earnings = {'financial_report': report} if income or cash else {}
        yield_pct = valuation.get('dividend_yield_ttm_pct')
        if yield_pct is not None:
            earnings['dividend'] = {
                'ttm_dividend_yield_pct': yield_pct,
                'as_of': valuation.get('as_of'), 'source': 'tushare:daily_basic.dv_ttm',
                'coverage': 'provider_ttm_yield_only', 'price_basis': 'daily_close',
                'definition': 'Tushare daily_basic dv_ttm (%); not recomputed from alternate-provider events',
            }
        result['earnings'] = block(earnings, ['income', 'cashflow', 'daily_basic'],
                                   missing=missing + ['dividend_events'])

        flows = fetch('moneyflow', start_date=(today-timedelta(days=35)).strftime('%Y%m%d'), end_date=today.strftime('%Y%m%d'), limit=20,
                      fields='ts_code,trade_date,buy_lg_amount,buy_elg_amount,sell_lg_amount,sell_elg_amount,net_mf_amount')
        values = []
        for row in flows.to_dict('records'):
            amounts = [_number(row.get(k)) for k in ('buy_lg_amount','buy_elg_amount','sell_lg_amount','sell_elg_amount')]
            values.append(None if any(x is None for x in amounts) else (amounts[0]+amounts[1]-amounts[2]-amounts[3])*10000)
        flow = {}
        if values and values[0] is not None:
            flow = {'main_net_inflow': values[0], 'inflow_5d': sum(values[:5]) if len(values)>=5 and None not in values[:5] else None,
                    'inflow_10d': sum(values[:10]) if len(values)>=10 and None not in values[:10] else None,
                    'as_of': _date(flows.iloc[0]['trade_date']), 'unit': 'CNY',
                    'definition': 'large + extra-large buy minus sell; daily closed session'}
            provider_net = _number(flows.iloc[0].get('net_mf_amount'))
            flow['provider_net_inflow'] = provider_net * 10000 if provider_net is not None else None
        result['capital_flow'] = block({'stock_flow': flow} if flow else {}, ['moneyflow'], complete=False, missing=['sector_rankings'])

        holders = fetch('top10_floatholders', limit=100,
                        fields='ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio,hold_float_ratio,hold_change,holder_type')
        holder_data = holder_facts(holders)
        result['institution'] = block(holder_data, ['top10_floatholders'], complete=False,
                                      missing=['full_institution_holding_change'])

        trade_date = str(latest.get('trade_date') or '')
        if trade_date:
            listings = fetch('top_list', trade_date=trade_date, limit=100,
                             fields='ts_code,trade_date,reason,net_amount')
            valid = 'top_list' not in errors
            data = {'is_on_list': not listings.empty, 'recent_count': len(listings), 'latest_date': _date(trade_date) if not listings.empty else None,
                    'checked_trade_date': _date(trade_date), 'lookback_sessions': 1} if valid else {}
            result['dragon_tiger'] = block(data, ['top_list'], complete=False, missing=['multi_session_history'])
        else:
            result['dragon_tiger'] = block({}, [], missing=['trade_date'])

        # Optional enrichment runs after core facts have been published. An
        # exhausted deadline cannot discard earlier successful blocks.
        dividends = fetch('dividend', limit=2000,
                          fields='ts_code,end_date,ann_date,div_proc,stk_div,cash_div_tax,record_date,ex_date,pay_date,imp_ann_date')
        dividend_missing = ['dividend_events']
        if 'dividend' not in errors:
            facts = dividend_facts(dividends, cutoff)
            if len(dividends) >= 2000:
                facts['events_complete'] = False
                facts['ttm_cash_dividend_per_share'] = None
            earnings['dividend'] = {**earnings.get('dividend', {}), **facts}
            dividend_missing = [] if facts['events_complete'] else ['dividend_event_values_or_history']
        result['earnings'] = block(earnings, ['income', 'cashflow', 'daily_basic', 'dividend'],
                                   missing=missing + dividend_missing)

        def market_fetch(api, **params):
            """Market tables must not be passed through the single-company filter."""
            key = api + ':market:' + str(params.get('trade_date') or '')
            started = time.monotonic()
            try:
                remaining = deadline - started
                if remaining <= 0:
                    raise TimeoutError('stage exhausted')
                frame = self.query(api, request_timeout=min(remaining, 5), **params)
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    raise ValueError('empty market table is not confirmed absence')
                if len(frame) >= int(params.get('limit', 10000)):
                    raise ValueError('possibly truncated market table')
                traces[key] = {'provider': f'tushare:{api}', 'result': 'ok', 'record_count': len(frame),
                               'duration_ms': int((time.monotonic()-started)*1000),
                               'data_fetched_at': frame.attrs.get('data_fetched_at', fetched_at),
                               'cache_hit': bool(frame.attrs.get('cache_hit'))}
                return frame, key
            except Exception as exc:
                errors[key] = f'{type(exc).__name__}: market table unavailable'
                traces[key] = {'provider': f'tushare:{api}', 'result': 'failed', 'record_count': 0,
                               'duration_ms': int((time.monotonic()-started)*1000)}
                return pd.DataFrame(), key

        if trade_date:
            members, basic_key = market_fetch('stock_basic', list_status='L', limit=10000,
                                              fields='ts_code,industry')
            bars, daily_key = market_fetch('daily', trade_date=trade_date, limit=6000,
                                           fields='ts_code,trade_date,pct_chg')
            market_flows, flows_key = market_fetch('moneyflow', trade_date=trade_date, limit=6000,
                                                   fields='ts_code,trade_date,buy_lg_amount,buy_elg_amount,sell_lg_amount,sell_elg_amount')
            prices, funding = industry_facts(members, bars, market_flows, trade_date)
            if prices:
                related = next((r for r in prices.pop('all') if r['name'] == industry), None)
                prices['related_industry'] = related
                data = {**result['boards']['data'], 'sector_rankings': prices}
                result['boards'] = block(data, ['stock_basic', basic_key, daily_key],
                                          missing=[] if prices['coverage_complete'] else ['industry_constituent_coverage'])
            if funding:
                related = next((r for r in funding.pop('all') if r['name'] == industry), None)
                funding['related_industry'] = related
                data = {**result['capital_flow']['data'], 'sector_rankings': funding}
                flow_missing = [k for k in ('main_net_inflow', 'inflow_5d', 'inflow_10d') if flow.get(k) is None]
                if not funding['coverage_complete']:
                    flow_missing.append('industry_constituent_coverage')
                result['capital_flow'] = block(data, ['moneyflow', basic_key, daily_key, flows_key], missing=flow_missing)

            # Same-day leaderboard is normally published at night. Do not turn
            # pre-publication emptiness into a negative event finding.
            end = min(trade_date, today.strftime('%Y%m%d'))
            local_now = datetime.now().astimezone()
            if not as_of and end == today.strftime('%Y%m%d') and local_now.hour < 21:
                end = (today-timedelta(days=1)).strftime('%Y%m%d')
            calendar, cal_key = market_fetch('trade_cal', exchange='SSE', is_open='1',
                                             start_date=(today-timedelta(days=45)).strftime('%Y%m%d'), end_date=end,
                                             fields='cal_date,is_open', limit=10000)
            if not calendar.empty and {'cal_date', 'is_open'}.issubset(calendar.columns):
                sessions = sorted({str(row.cal_date) for row in calendar.itertuples()
                                   if str(row.is_open) == '1' and _date(row.cal_date) and str(row.cal_date) <= end}, reverse=True)[:5]
                tables, keys = {}, [cal_key]
                for day in sessions:
                    table, key = market_fetch('top_list', trade_date=day, limit=10000,
                                               fields='ts_code,trade_date,reason,net_amount')
                    tables[day] = table
                    keys.append(key)
                if sessions:
                    data = dragon_tiger_facts(ts_code, sessions, tables)
                    result['dragon_tiger'] = block(data, keys,
                        missing=[] if data['coverage_complete'] and len(sessions) == 5 else ['multi_session_history'])
        return result


def json_records(frame):
    """Convert nullable numeric dataframe values to JSON-safe Python values."""
    import json
    return json.loads(frame.to_json(orient='records', force_ascii=False))
