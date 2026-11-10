"""Source-backed listing-age eligibility shared by live and replay adapters."""
from datetime import date, timedelta
import math
import re

ELIGIBILITY_VERSION = 'published-capco-61-session-explicit-pe-null-v1'


def validate_history_sessions(expected, observed, suspension_rows, *, resolve=None):
    """A missing session is valid only with independently verified full-day S."""
    if set(observed) - set(expected):
        raise ValueError('history_non_session_bar')
    for stamp in sorted(set(expected) - set(observed)):
        state = resolve(stamp) if resolve else suspension_state(suspension_rows(stamp))
        if state['status'] != 'full_day':
            raise ValueError('unexplained_historical_bar_gap')


def suspension_proof_candidate(rows, quotes, *, code, stamp):
    """Two exact unresolved event forms; neither certifies suspension by itself."""
    from data_provider.historical_reference import day
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}\.(SH|SZ)', code):
        return False
    try:
        empty_timings = all('suspend_timing' in r and r['suspend_timing'] in (None, '') for r in rows)
        opening_zero = all('suspend_timing' in r and
            (r['suspend_timing'] is None if r.get('suspend_type') == 'R'
             else r['suspend_timing'] == '09:30-09:30') for r in rows)
        return (bool(rows) and {r.get('suspend_type') for r in rows} == {'S', 'R'}
            and (empty_timings or opening_zero)
            and all(r.get('ts_code') == code and day(r['trade_date']) == day(stamp) for r in rows)
            and not any(r.get('ts_code') == code and day(r['trade_date']) == day(stamp) for r in quotes))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def verified_suspension_proof(rows, quotes, records, *, code, stamp):
    """Pure positive-status corroboration. Placeholder prices are never returned."""
    from data_provider.historical_reference import day
    if (not suspension_proof_candidate(rows, quotes, code=code, stamp=stamp)
            or not isinstance(records, list) or len(records) != 1):
        raise ValueError('suspension_proof_not_applicable')
    row = records[0]
    if not isinstance(row, dict) or not {'code', 'date', 'tradestatus', 'volume', 'amount', 'open', 'high', 'low', 'close'} <= row.keys():
        raise ValueError('suspension_proof_schema_invalid')
    if (row.get('code') != code[-2:].lower() + '.' + code[:6] or day(row['date']) != day(stamp)
            or row.get('tradestatus') != '0' or row.get('volume') != '' or row.get('amount') != ''):
        raise ValueError('suspension_proof_identity_date_or_status_invalid')
    prices = [row.get(k) for k in ('open', 'high', 'low', 'close')]
    if any(isinstance(v, bool) or v is None for v in prices):
        raise ValueError('suspension_proof_placeholder_invalid')
    prices = [float(v) for v in prices]
    if any(not math.isfinite(v) or v <= 0 for v in prices) or len(set(prices)) != 1:
        raise ValueError('suspension_proof_placeholder_invalid')
    return dict(status='full_day', suspended_open=True, suspended_close=True)


def suspension_state(rows):
    """Tushare suspend_d rows are events: R/null can coexist with intraday S.

    Only call after successful complete date-batch/identity validation. An
    absent record then means no suspension, not a failed endpoint response.
    """
    if not rows:
        return dict(status='none', suspended_open=False, suspended_close=False)
    if any('suspend_timing' not in row or row.get('suspend_type') not in {'S', 'R'} for row in rows):
        raise ValueError('suspension_schema_unknown')
    if any(row['suspend_timing'] is not None and not isinstance(row['suspend_timing'], str) for row in rows):
        raise ValueError('suspension_timing_unknown')
    resumptions = [row for row in rows if row['suspend_type'] == 'R']
    if any(row['suspend_timing'] not in (None, '') for row in resumptions):
        raise ValueError('suspension_timing_unknown')
    timings = {row['suspend_timing'] for row in rows if row['suspend_type'] == 'S'}
    if not timings:
        return dict(status='none', suspended_open=False, suspended_close=False)
    if resumptions and (None in timings or '' in timings):
        raise ValueError('suspension_events_conflict')
    if timings == {None} or timings == {''}:
        return dict(status='full_day', suspended_open=True, suspended_close=True)
    if None in timings or '' in timings:
        raise ValueError('suspension_events_conflict')
    intervals, continuations = [], []
    for timing in timings:
        if not isinstance(timing, str):
            raise ValueError('suspension_timing_unknown')
        segments = timing.split(',')
        for segment in segments:
            match = re.fullmatch(r'([0-9]{1,2}):([0-5][0-9])-([0-9]{1,2}):([0-5][0-9])', segment.strip())
            if match is None or int(match[1]) > 23 or int(match[3]) > 23:
                raise ValueError('suspension_timing_unknown')
            start, end = int(match[1]) * 60 + int(match[2]), int(match[3]) * 60 + int(match[4])
            if 570 <= start < end <= 900:
                intervals.append((start, end))
            elif end == 570 and (570 < start < 690 or 780 <= start < 900) and len(segments) == 1:
                continuations.append((start, end))
            else:
                raise ValueError('suspension_timing_unknown')
    if continuations:
        if resumptions or intervals or len(set(continuations)) != 1:
            raise ValueError('suspension_timing_unknown')
        # The end token is an expected future opening, never evidence that a
        # later session actually resumed. Certify only this session's tail.
        return dict(status='partial', suspended_open=False, suspended_close=True)
    return dict(status='partial', suspended_open=any(a <= 570 < b for a, b in intervals),
                suspended_close=any(a < 900 <= b for a, b in intervals))


def scoring_eligibility(*, reference, name_status, daily, basic, suspension, evidence_id, allow_pending_pb=False):
    """Shared restricted scoring universe; unknown acquisition is never exclusion.

    Tushare documents PE-TTM null for loss-making observations. We only claim
    'not reported', not independently verified losses. No positive-PE threshold.
    ``suspension`` must be full_day/partial/none/unknown, from dated evidence.
    """
    result = dict(version=ELIGIBILITY_VERSION, evidence_id=evidence_id)
    if not reference['eligible']:
        return dict(result, **reference)
    if name_status in {'st', 'star_st', 'delisting_arrangement'}:
        return dict(result, eligible=False, reason='excluded_' + name_status)
    if name_status != 'normal':
        raise ValueError('name_state_unknown')
    if suspension == 'full_day':
        return dict(result, eligible=False, reason='verified_full_day_suspension')
    if suspension not in {'none', 'partial'}:
        raise ValueError('suspension_state_unknown')
    if daily is None or basic is None:
        raise ValueError('required_market_row_missing')
    fields = [('close', daily), ('pct_chg', daily), ('amount', daily),
              ('turnover_rate', basic), ('volume_ratio', basic), ('pe_ttm', basic), ('pb', basic), ('total_mv', basic)]
    if any(field not in row for field, row in fields):
        raise ValueError('required_market_column_missing')
    pe = basic['pe_ttm']
    if pe is None or (isinstance(pe, float) and math.isnan(pe)):
        return dict(result, eligible=False, reason='ineligible_pe_not_reported', raw_pe_ttm=None)
    for field, row in fields:
        value = row[field]
        if field == 'pb' and allow_pending_pb and (value is None or isinstance(value, float) and math.isnan(value)):
            continue  # Acquisition staging only; never a scoring substitution.
        if isinstance(value, bool) or value is None or not math.isfinite(float(value)):
            raise ValueError('required_market_value_invalid')
        if (field == 'close' and float(value) <= 0) or (field in {'amount', 'total_mv', 'volume_ratio', 'turnover_rate'} and float(value) < 0):
            raise ValueError('required_market_value_invalid')
    pending = allow_pending_pb and (basic['pb'] is None or isinstance(basic['pb'], float) and math.isnan(basic['pb']))
    return dict(result, eligible=True, reason='pending_pb' if pending else 'eligible', pending_pb=pending)


def listing_eligibility(listed, signal, calendar, *, exchange, listing_evidence_id, calendar_evidence_id):
    """Count verified exchange sessions, never bars or approximated calendar days.

    The calendar must cover every calendar date in its bounded interval through
    signal. Older listings are eligible once that interval proves 61 sessions.
    """
    if (not isinstance(listed, date) or not isinstance(signal, date) or listed > signal
            or not listing_evidence_id or not calendar_evidence_id or not calendar):
        raise ValueError('listing_eligibility_invalid')
    days = sorted(calendar)
    if (days[-1] != signal or any(right - left != timedelta(days=1) for left, right in zip(days, days[1:]))
            or any(type(calendar[d]) is not int or calendar[d] not in (0, 1) for d in days)):
        raise ValueError('listing_calendar_invalid')
    sessions = sum(calendar[d] for d in days if d >= listed)
    if sessions < 61 and days[0] > listed:
        raise ValueError('listing_calendar_insufficient')
    return dict(eligible=sessions >= 61, reason='eligible' if sessions >= 61 else 'ipo_fewer_than_61_sessions',
                exchange=exchange, listing_date=listed.isoformat(), signal_date=signal.isoformat(),
                observed_sessions=sessions, listing_evidence_id=listing_evidence_id, calendar_evidence_id=calendar_evidence_id)
