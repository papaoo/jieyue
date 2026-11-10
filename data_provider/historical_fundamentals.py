"""Narrow signed-PB supplement with dated identity/price/PE cross-checks."""
import math

from data_provider.historical_reference import day


def missing(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def supplement_pb(primary, quote, record, *, evidence_id):
    """Never replaces a primary value, absent row/column or another metric."""
    if primary is None or quote is None or 'pb' not in primary or not missing(primary['pb']):
        return primary, None
    code = primary['ts_code']
    if code[-2:] not in {'SH', 'SZ'} or missing(primary.get('pe_ttm')):
        return primary, None
    if (record.get('code') != code[-2:].lower() + '.' + code[:6]
            or day(record['date']) != day(primary['trade_date'])
            or day(quote['trade_date']) != day(primary['trade_date']) or quote['ts_code'] != code):
        raise ValueError('pb_supplement_identity_or_date_conflict')
    values = [float(record[k]) for k in ('close', 'peTTM', 'pbMRQ')]
    if not all(math.isfinite(v) for v in values) or values[0] <= 0:
        raise ValueError('pb_supplement_invalid')
    if abs(values[0] - float(quote['close'])) > .00500001:
        raise ValueError('pb_supplement_close_conflict')
    if not math.isclose(values[1], float(primary['pe_ttm']), rel_tol=0., abs_tol=1e-4):
        raise ValueError('pb_supplement_pe_conflict')
    if not evidence_id:
        raise ValueError('pb_supplement_evidence_missing')
    return dict(primary, pb=values[2]), dict(source='baostock.pbMRQ', evidence_id=evidence_id,
        effective_as_of=day(primary['trade_date']).isoformat(), original_primary_pb=None,
        basis='market_cap_over_latest_disclosed_parent_equity_less_other_equity_tools')


def fetch_suspension_status(ts_code, stamp):
    """Explicit exact-day anonymous status capture; never a price fallback."""
    return _fetch_range(ts_code, stamp, stamp, 'date,code,open,high,low,close,volume,amount,tradestatus', 1)


def fetch_pb_range(ts_code, start, end):
    """Explicit acquisition only. One code/range, unadjusted; never get_data()."""
    return _fetch_range(ts_code, start, end, 'date,code,close,peTTM,pbMRQ', 1500)


def _fetch_range(ts_code, start, end, fields, limit):
    if ts_code[-2:] not in {'SH', 'SZ'}:
        raise ValueError('pb_supplement_exchange_unsupported')
    import baostock as bs
    from src.services.screening.source_guard import call_with_timeout
    from src.services.screening.daily import _BAOSTOCK_LOCK
    def request():
        with _BAOSTOCK_LOCK:
            login = bs.login()
            if login.error_code != '0':
                raise ValueError('pb_supplement_login_failed')
            try:
                response = bs.query_history_k_data_plus(ts_code[-2:].lower() + '.' + ts_code[:6],
                    fields, start_date=day(start).isoformat(),
                    end_date=day(end).isoformat(), frequency='d', adjustflag='3')
                records = []
                while response.error_code == '0' and response.next():
                    records.append(dict(zip(response.fields, response.get_row_data())))
                    if len(records) > limit:
                        raise ValueError('pb_supplement_range_too_large')
                if response.error_code != '0':
                    raise ValueError('pb_supplement_request_failed')
                return records
            finally:
                bs.logout()
    return call_with_timeout(request, timeout_sec=20., label='historical bounded supplement')
