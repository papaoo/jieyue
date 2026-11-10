"""Reference chronology and interval coverage, entirely synthetic."""
from datetime import date
import pytest

from data_provider.historical_reference import (
    REFERENCE_VERSION, ReferenceBundle, closure_calendar, name_state, normalize_aliases, parse_capco_pages,
)


def bundle():
    return ReferenceBundle(dict(version=REFERENCE_VERSION, aliases=[], aliases_sha256='a' * 64,
        reports=[dict(period='2025H1', published_on='2025-09-30', inclusion_cutoff='2025-06-10',
            valid_through='2026-12-31', pdf_sha256='b' * 64, article_sha256='c' * 64,
            guideline_sha256='d' * 64, rows={'600001': dict(industry='银行', industry_code='J66')})],
        calendars=[dict(exchange=exchange, year=year, source_sha256='e' * 64,
            derivation='official_annual_closures_weekends_v1', closures=[])
            for exchange in ('SSE', 'SZSE', 'BSE') for year in (2025, 2026)]))


def test_wrapped_manufacturing_major_not_secondary():
    rows = parse_capco_pages(['000008神州高铁 C 制造业 C G 专用、通用及交通运输设备 37\n铁路、船舶、航空航天和其他\n运输设备制造业\n000009 中国宝安 J 金融业 69 其他金融业'])
    assert rows['000008']['industry_code'] == 'C37'
    assert rows['000009']['industry_code'] == 'J69'
    with pytest.raises(ValueError, match='duplicate'):
        parse_capco_pages(['000009 中国宝安 J 金融业 69 其他金融业'] * 2)


def test_publication_lag_and_ipo_cutoff_not_period_end():
    ref = bundle()
    with pytest.raises(ValueError, match='unavailable'):
        ref.classify('600001', '2000-01-01', '2025-09-30', 'SSE')
    assert ref.classify('600001', '2000-01-01', '2025-10-01', 'SSE')['industry_as_of'] == '2025-10-01'
    assert not ref.classify('600002', '2025-06-10', '2026-01-05', 'SSE')['eligible']
    with pytest.raises(ValueError, match='seasoned'):
        ref.classify('600002', '2025-06-09', '2026-01-05', 'SSE')


def test_unsupported_calendar_year_is_unknown_and_weekends_never_makeup():
    values = closure_calendar(2026, [('2026-02-15', '2026-02-23')])
    assert values['2026-02-14'] == 0 and values['2026-02-23'] == 0 and values['2026-02-24'] == 1
    with pytest.raises(ValueError, match='coverage'):
        bundle().calendar('BSE', date(2027, 1, 1), date(2027, 1, 2))


def test_official_alias_fields_and_conflicts():
    assert normalize_aliases([dict(o_code='430047.BJ', n_code='920047.BJ')]) == {'430047': '920047'}
    with pytest.raises(ValueError, match='conflict'):
        normalize_aliases([dict(o_code='430047', n_code='920047'), dict(o_code='430047', n_code='920048')])


def test_name_intervals_do_not_activate_future_effective_announcements():
    normal = dict(name='正常', start_date='20200101', end_date='20261022', ann_date='20191230')
    future = dict(name='萃华退', start_date='20261023', end_date=None, ann_date='20261016')
    assert name_state([normal, normal, future], '2026-10-16')['status'] == 'normal'
    assert name_state([normal, future], '2026-10-23')['status'] == 'delisting_arrangement'
    with pytest.raises(ValueError, match='missing'):
        name_state([], '2026-10-16')
    with pytest.raises(ValueError, match='publication'):
        name_state([dict(normal, ann_date=None)], '2026-10-16')
    with pytest.raises(ValueError, match='conflict'):
        name_state([normal, dict(normal, name='*ST测试')], '2026-10-16')


def test_shared_eligibility_distinguishes_explicit_pe_null_from_download_failure():
    from src.services.screening.eligibility import scoring_eligibility
    args = dict(reference=dict(eligible=True), name_status='normal', suspension='none', evidence_id='fixture',
        daily=dict(close=10., pct_chg=0., amount=1e6),
        basic=dict(turnover_rate=1., volume_ratio=1., pe_ttm=None, pb=1., total_mv=1e6))
    assert scoring_eligibility(**args)['reason'] == 'ineligible_pe_not_reported'
    with pytest.raises(ValueError, match='row_missing'):
        scoring_eligibility(**dict(args, basic=None))
    with pytest.raises(ValueError, match='column_missing'):
        scoring_eligibility(**dict(args, basic={}))
    with pytest.raises(ValueError, match='value_invalid'):
        scoring_eligibility(**dict(args, basic=dict(args['basic'], pe_ttm=10., pb=None)))
    assert scoring_eligibility(**dict(args, basic=dict(args['basic'], pe_ttm=-10.)))['eligible']
    assert not scoring_eligibility(**dict(args, reference=dict(eligible=False, reason='ipo'), basic=None))['eligible']


def test_documented_suspend_d_null_and_intraday_and_conflicts():
    from src.services.screening.eligibility import suspension_state
    assert suspension_state([dict(suspend_type='S', suspend_timing=None)])['status'] == 'full_day'
    partial = suspension_state([dict(suspend_type='S', suspend_timing='09:30-10:00')])
    assert partial == dict(status='partial', suspended_open=True, suspended_close=False)
    assert suspension_state([dict(suspend_type='S', suspend_timing='14:00-15:00')])['suspended_close']
    assert suspension_state([dict(suspend_type='R', suspend_timing=None)])['status'] == 'none'
    with pytest.raises(ValueError, match='schema'):
        suspension_state([dict(suspend_type='S')])
    with pytest.raises(ValueError, match='conflict'):
        suspension_state([dict(suspend_type='S', suspend_timing=None), dict(suspend_type='R', suspend_timing=None)])


@pytest.mark.parametrize('timings,blocked_close', [
    (['09:30-09:41,09:44-09:54'], False),
    (['09:30-09:41', '09:44-09:54', '09:30-09:41'], False),
    (['09:30-09:41', '14:00-15:00'], True),
])
def test_resumption_and_intraday_suspension_are_events_not_conflicting_states(timings, blocked_close):
    from src.services.screening.eligibility import suspension_state, validate_history_sessions
    rows = [dict(suspend_type='R', suspend_timing=None)] + [
        dict(suspend_type='S', suspend_timing=timing) for timing in timings]
    assert suspension_state(rows) == dict(status='partial', suspended_open=True, suspended_close=blocked_close)
    assert suspension_state(list(reversed(rows))) == suspension_state(rows)
    assert suspension_state(rows[1:]) == suspension_state(rows)  # Multiple S rows union identically.
    with pytest.raises(ValueError, match='unexplained_historical_bar_gap'):
        validate_history_sessions([date(2025, 4, 28)], [], lambda stamp: rows)


@pytest.mark.parametrize('rows', [
    [dict(suspend_type='R', suspend_timing='09:30-09:41')],
    [dict(suspend_type='R', suspend_timing='unknown'), dict(suspend_type='S', suspend_timing='09:30-09:41')],
    [dict(suspend_type='R', suspend_timing=None), dict(suspend_type='S', suspend_timing=None)],
    [dict(suspend_type='S', suspend_timing=None), dict(suspend_type='S', suspend_timing='09:30-09:41')],
    [dict(suspend_type='S', suspend_timing='09:60-10:00')],
    [dict(suspend_type='S', suspend_timing='09:30-09:30')],
    [dict(suspend_type='S', suspend_timing='09:30-09:41,')],
    [dict(suspend_type='S', suspend_timing=[])], [dict(suspend_type='R')],
])
def test_unsupported_resumption_and_malformed_or_ambiguous_suspension_stay_unknown(rows):
    from src.services.screening.eligibility import suspension_state
    with pytest.raises(ValueError, match='suspension_'):
        suspension_state(rows)


def test_sqlite_batch_and_indexed_row_corruption_is_rejected(tmp_path):
    from src.services.screening.backtest_data import BatchStore, DataUnavailable
    store = BatchStore(tmp_path / 'batches.sqlite')
    params = dict(trade_date='20260105')
    store.put('daily', params, [dict(ts_code='600001.SH', trade_date='20260105', close=10)], '2026-01-06T00:00:00+00:00')
    assert store.batch('daily', params)[0][0]['close'] == 10
    store.db.execute("UPDATE rows SET raw='{}'")
    with pytest.raises(DataUnavailable, match='indexed_row_hash'):
        store.row('daily', '600001.SH', '2026-01-05')
    store.db.execute("UPDATE batches SET raw='{}'")
    with pytest.raises(DataUnavailable, match='batch_hash'):
        store.batch('daily', params)
    store.close()


def test_bse_visible_body_only_and_missing_holiday_blocks():
    from data_provider.historical_reference import parse_bse_annual
    with pytest.raises(ValueError, match='body'):
        parse_bse_annual('<html>share only</html>', 2026)
    body = '<div class="in_main clearfix">北京证券交易所2026年部分节假日休市安排 一、休市安排 （一）元旦：1月1日（星期四）休市 二、清算</div>'
    with pytest.raises(ValueError, match='holiday'):
        parse_bse_annual(body, 2026)


def seeded_dataset(tmp_path):
    import pandas as pd
    from src.services.screening.backtest_data import BatchStore, HistoricalDataset
    store = BatchStore(tmp_path / 'history.sqlite')
    store.db.execute('PRAGMA journal_mode=MEMORY')
    master = dict(ts_code='600001.SH', symbol='600001', name='现名不用于过去', list_date='20000101',
                  delist_date='20260202', list_status='D', exchange='SSE')
    for status in ('L', 'D', 'P'):
        store.put('stock_basic', dict(exchange='', list_status=status), [master] if status == 'D' else [], '2026-02-02T08:00:00+00:00')
    store.put('namechange', {}, [dict(ts_code='600001.SH', name='正常', start_date='20000101', end_date='20260109', ann_date='19991230'),
        dict(ts_code='600001.SH', name='*ST测试', start_date='20260112', end_date=None, ann_date='20260109')], '2026-02-02T08:00:00+00:00')
    for stamp in pd.bdate_range('2025-03-01', '2026-01-30').strftime('%Y%m%d'):
        identity = dict(ts_code='600001.SH', trade_date=stamp)
        rows = {
            'daily': [dict(identity, open=10., high=11., low=9., close=10., vol=10000., pct_chg=0., amount=1e6)],
            'daily_basic': [dict(identity, turnover_rate=2., volume_ratio=1.2, pe_ttm=8., pb=1., total_mv=1e6, circ_mv=8e5, dv_ttm=0.)],
            'adj_factor': [dict(identity, adj_factor=1.)], 'suspend_d': [],
            'stk_limit': [dict(identity, up_limit=11., down_limit=9.)],
            'index_daily': [dict(ts_code='000300.SH', trade_date=stamp, open=100., close=100.)],
        }
        for endpoint, records in rows.items():
            params = dict(trade_date=stamp)
            if endpoint == 'index_daily': params['ts_code'] = '000300.SH'
            store.put(endpoint, params, records, '2026-02-02T08:00:00+00:00')
        store.date_status(stamp, bundle().hash, 'complete')
    return HistoricalDataset(store, bundle())


def test_later_delisted_and_st_intervals_and_ordinary_nonfrozen_entry(tmp_path, monkeypatch):
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    from tests.test_screening_compute_only import forbid_remote
    dataset = seeded_dataset(tmp_path)
    forbid_remote(monkeypatch)
    snapshot, _ = dataset.snapshot(date(2026, 1, 5))
    assert snapshot.name.tolist() == ['正常']
    assert snapshot.dividend_yield_ttm_pct.tolist() == [0.]
    result = screen('dual_low', config=Config(), reference_dataset=dataset, closed_session=date(2026, 1, 5), use_llm=True)
    assert [p.code for p in result.picks] == ['600001'] and not result.llm_ranked
    assert result.picks[0].industry_taxonomy == 'capco_major_v1'
    assert dataset.snapshot(date(2026, 1, 12))[0].empty
    assert dataset.universe(date(2026, 2, 2)) == {}
    dataset.store.close()


def test_future_prices_factors_references_cannot_change_selection(tmp_path, monkeypatch):
    import json
    from data_provider.historical_reference import digest
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    from tests.test_screening_compute_only import forbid_remote
    dataset = seeded_dataset(tmp_path)
    forbid_remote(monkeypatch)
    def selection():
        value = screen('dual_low', config=Config(), reference_dataset=dataset, closed_session=date(2026, 1, 5))
        return [(p.code, p.final_score, p.industry_code) for p in value.picks]
    before = selection()
    # Valid future mutation in indexed input; only future rows are changed.
    for rowid, raw in dataset.store.db.execute("SELECT rowid,raw FROM rows WHERE endpoint IN ('adj_factor','daily') AND stamp>'20260105'").fetchall():
        record = json.loads(raw)
        if 'adj_factor' in record:
            record['adj_factor'] = 99.
        else:
            for field in ('open', 'high', 'low', 'close'):
                record[field] *= 10
        dataset.store.db.execute('UPDATE rows SET raw=?,row_sha=? WHERE rowid=?', (json.dumps(record), digest(record), rowid))
    future = dict(dataset.references.reports[0], period='2026H1', published_on='2026-09-30',
                  rows={'600001': dict(industry='未来行业', industry_code='K70')})
    dataset.references.reports.append(future)
    dataset.references._report_rows['2026H1'] = future['rows']
    assert selection() == before
    dataset.store.close()


def test_signed_pb_supplement_is_narrow_and_cross_checked():
    from data_provider.historical_fundamentals import supplement_pb
    primary = dict(ts_code='300157.SZ', trade_date='20260105', pe_ttm=159.1315, pb=None)
    quote = dict(ts_code='300157.SZ', trade_date='20260105', close=4.64)
    record = dict(code='sz.300157', date='2026-01-05', close='4.6400', peTTM='159.131502', pbMRQ='-17.568612')
    updated, metadata = supplement_pb(primary, quote, record, evidence_id='fixture')
    assert updated['pb'] == -17.568612 and primary['pb'] is None
    assert metadata['source'] == 'baostock.pbMRQ'
    assert supplement_pb(dict(primary, pb=2.), quote, record, evidence_id='fixture')[0]['pb'] == 2.
    assert supplement_pb(None, quote, record, evidence_id='fixture') == (None, None)
    for change in (dict(close='4.65'), dict(peTTM='160.'), dict(date='2026-01-06'), dict(code='sz.300158')):
        with pytest.raises(ValueError):
            supplement_pb(primary, quote, dict(record, **change), evidence_id='fixture')


def test_history_gap_requires_explicit_full_day_evidence():
    from src.services.screening.eligibility import validate_history_sessions
    expected = [date(2026, 1, 5), date(2026, 1, 6)]
    with pytest.raises(ValueError, match='unexplained'):
        validate_history_sessions(expected, expected[:1], lambda stamp: [])
    with pytest.raises(ValueError, match='unexplained'):
        validate_history_sessions(expected, expected[:1], lambda stamp: [dict(suspend_type='S', suspend_timing='09:30-10:00')])
    validate_history_sessions(expected, expected[:1], lambda stamp: [dict(suspend_type='S', suspend_timing=None)])


def test_incremental_ordinary_refresh_keeps_existing_batches(tmp_path, monkeypatch):
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    from tests.test_screening_compute_only import forbid_remote
    dataset = seeded_dataset(tmp_path)
    forbid_remote(monkeypatch)
    calls = []
    def refresh(current, start, end):
        calls.append((start, end))
        for endpoint in ('daily', 'daily_basic', 'adj_factor'):
            current.store.put(endpoint, dict(trade_date='20260202'), [], '2026-02-02T08:00:00+00:00')
    # Empty batches are not evidence of a latest available market day.
    from src.services.screening.backtest_data import DataUnavailable
    with pytest.raises(DataUnavailable, match='stale'):
        screen('dual_low', config=Config(), reference_dataset=dataset, closed_session=date(2026, 2, 2), reference_refresh=refresh)
    assert calls == [(date(2026, 1, 31), date(2026, 2, 2))]
    dataset.store.close()


def test_stale_cache_can_incrementally_refresh_to_closed_session(tmp_path, monkeypatch):
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    from tests.test_screening_compute_only import forbid_remote
    dataset = seeded_dataset(tmp_path)
    forbid_remote(monkeypatch)
    # Hide only final date batches, retaining the exact earlier evidence bytes.
    with dataset.store.db:
        dataset.store.db.execute("DELETE FROM rows WHERE stamp='20260130'")
        dataset.store.db.execute("DELETE FROM batches WHERE stamp='20260130'")
    before = dataset.store.batch('daily', dict(trade_date='20260105'))
    calls = []
    def refresh(current, start, end):
        calls.append((start, end))
        for endpoint in ('daily', 'daily_basic', 'adj_factor', 'stk_limit', 'suspend_d'):
            rows = current.store.batch(endpoint, dict(trade_date='20260129'))[0]
            current.store.put(endpoint, dict(trade_date='20260130'),
                [dict(row, trade_date='20260130') for row in rows], '2026-01-30T08:30:00+00:00')
        current.store.date_status(end, current.references.hash, 'complete')
    result = screen('dual_low', config=Config(), reference_dataset=dataset,
        closed_session=date(2026, 1, 30), reference_refresh=refresh)
    assert calls == [(date(2026, 1, 30), date(2026, 1, 30))]
    assert result.picks == []  # Explicit ST interval, not missing acquisition.
    assert dataset.store.batch('daily', dict(trade_date='20260105')) == before
    dataset.store.close()


@pytest.mark.parametrize('missing_endpoint', ['daily_basic', 'adj_factor', 'stk_limit', 'suspend_d'])
def test_partially_acquired_date_is_not_a_freshness_watermark(tmp_path, missing_endpoint):
    dataset = seeded_dataset(tmp_path)
    with dataset.store.db:
        dataset.store.db.execute('DELETE FROM batches WHERE endpoint=? AND stamp=?', (missing_endpoint, '20260130'))
    assert dataset.last_available_date == date(2026, 1, 29)
    dataset.store.close()


def test_paginated_acquisition_pacing_and_resume_preserves_vintage(tmp_path):
    import pandas as pd
    from src.services.screening.backtest_data import BatchStore, Acquirer, FIELDS, DataUnavailable
    store = BatchStore(tmp_path / 'paged.sqlite')
    calls, waits = [], []
    class Client:
        def query(self, endpoint, **params):
            calls.append(params['offset'])
            rows = [dict(ts_code='600001.SH', trade_date=params['trade_date'], **{k: 1. for k in
                ('open', 'high', 'low', 'close', 'pct_chg', 'amount', 'vol')})] if not params['offset'] else []
            return pd.DataFrame(rows, columns=FIELDS[endpoint].split(','))
    acquisition = Acquirer(store, Client(), page_size=1, clock=lambda: 0., sleep=waits.append)
    acquisition.batch('daily', trade_date='20260105')
    # Standalone acquisition cannot assert cross-endpoint universe coverage.
    # This synthetic one-row fixture explicitly accepts its validated attempt.
    params, records, fetched_at = acquisition.staged['daily']
    store.put('daily', params, records, fetched_at)
    original = store.batch('daily', dict(trade_date='20260105'))
    acquisition.batch('daily', trade_date='20260105')
    assert calls == [0, 1] and waits == [.4]
    assert store.batch('daily', dict(trade_date='20260105')) == original
    with pytest.raises(DataUnavailable, match='budget'):
        Acquirer(store, Client(), max_requests=1, page_size=1, sleep=waits.append).batch('daily', trade_date='20260106')
    store.close()


def test_hydration_reuses_original_vintages_and_rejects_corruption(tmp_path, monkeypatch):
    import json
    from hashlib import sha256
    from data_provider.historical_reference import hydrate_bundle
    raw = tmp_path / 'original'
    raw.mkdir()
    sources = {}
    vintage = '2026-10-17T08:00:00+00:00'
    values = {'report.pdf': b'offline PDF parser fixture', 'article.js': b'publication 2025-09-30',
              'guideline.js': b'cutoff 06-10 / 12-20', 'aliases.json': b'[]'}
    for name, value in values.items():
        (raw / name).write_bytes(value)
        sources[name] = dict(path=str(raw / name), fetched_at=vintage, url='https://official.example/' + name,
                             sha256=sha256(value).hexdigest())
    monkeypatch.setattr('data_provider.historical_reference.parse_capco_pdf', lambda path:
                        {'600001': dict(industry='银行', industry_code='J66')})
    plan = dict(sources=sources, guideline='guideline.js', aliases='aliases.json', bse_calendars=[], exchange_calendars=[],
        reports=[dict(period='2025H1', published_on='2025-09-30', inclusion_cutoff='2025-06-10',
            valid_through='2026-12-31', expected_rows=1, pdf='report.pdf', article='article.js')])
    destination = tmp_path / 'bundle'
    hydrated = hydrate_bundle(plan, destination)
    assert {v['fetched_at'] for v in hydrated.source_vintages.values()} == {vintage}
    assert hydrate_bundle(plan, destination).hash == hydrated.hash
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['plan_sha256']
    (destination / 'report.pdf').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='missing_or_invalid'):
        ReferenceBundle.load(destination)


def test_actual_offline_official_notice_parser_acceptance():
    from pathlib import Path
    from data_provider.historical_reference import parse_bse_annual
    root = Path(__file__).resolve().parents[1] / 'local/audits/strategy-integrity-20261017/preflight'
    if not (root / 'bse-2025-annual.html').exists():
        pytest.skip('optional audited local official raw sources not distributed')
    assert parse_bse_annual((root / 'bse-2025-annual.html').read_bytes(), 2025) == [
        ['2025-01-01', '2025-01-01'], ['2025-01-28', '2025-02-04'], ['2025-04-04', '2025-04-06'],
        ['2025-05-01', '2025-05-05'], ['2025-05-31', '2025-06-02'], ['2025-10-01', '2025-10-08']]
    assert parse_bse_annual((root / 'bse-2026-annual.html').read_bytes(), 2026)[1] == ['2026-02-15', '2026-02-23']


def test_explicit_acquisition_credentials_do_not_depend_on_or_mutate_environment(monkeypatch):
    import os
    from scripts.backtest_screening import acquisition_client
    monkeypatch.setenv('TUSHARE_API_URL', 'https://ambient.invalid')
    before = dict(os.environ)
    calls = []
    def factory(token, **kwargs):
        calls.append((token, kwargs))
        return object()
    acquisition_client(dict(TUSHARE_TOKEN='offline-sentinel', TUSHARE_HTTP_URL=' https://injected.invalid '), factory)
    assert calls == [('offline-sentinel', dict(timeout=15, api_url='https://injected.invalid'))]
    assert dict(os.environ) == before


def incremental_fixture(tmp_path):
    """Existing complete history through Jan8, provider responses for Jan9."""
    import copy
    import pandas as pd
    from src.services.screening.backtest_data import FIELDS
    dataset = seeded_dataset(tmp_path)
    responses = {}
    for endpoint in ('daily', 'daily_basic', 'adj_factor', 'stk_limit', 'suspend_d', 'index_daily'):
        params = dict(trade_date='20260109')
        if endpoint == 'index_daily': params['ts_code'] = '000300.SH'
        responses[endpoint] = dataset.store.batch(endpoint, params)[0]
    masters = {status: dataset.store.batch('stock_basic', dict(exchange='', list_status=status))[0]
               for status in ('L', 'D', 'P')}
    names = dataset.store.batch('namechange', {})[0]
    with dataset.store.db:
        dataset.store.db.execute("DELETE FROM rows WHERE stamp>='20260109'")
        dataset.store.db.execute("DELETE FROM batches WHERE stamp>='20260109'")
        dataset.store.db.execute("DELETE FROM date_state WHERE stamp>='20260109'")
    class Client:
        first_daily = None
        calls = []
        reference_masters = masters
        reference_names = names
        def query(self, endpoint, **params):
            self.calls.append(endpoint)
            rows = masters[params['list_status']] if endpoint == 'stock_basic' else names if endpoint == 'namechange' else responses[endpoint]
            if endpoint == 'daily' and self.first_daily is not None:
                rows, self.first_daily = self.first_daily, None
            return pd.DataFrame(copy.deepcopy(rows), columns=FIELDS[endpoint].split(','))
    return dataset, Client(), responses


@pytest.mark.parametrize('first_kind', ['empty', 'partial'])
def test_ordinary_incomplete_attempt_can_retry_to_complete(tmp_path, monkeypatch, first_kind):
    from src.services.screening.backtest_data import Acquirer, acquire_market, DataUnavailable
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    from tests.test_screening_compute_only import forbid_remote
    dataset, client, responses = incremental_fixture(tmp_path)
    forbid_remote(monkeypatch)
    import copy
    payload = copy.deepcopy(dataset.references.payload)
    payload['reports'][0]['rows']['600002'] = dict(industry='银行', industry_code='J66')
    dataset.references = ReferenceBundle(payload)
    with dataset.store.db:
        dataset.store.db.execute('UPDATE date_state SET reference_hash=?', (dataset.references.hash,))
    client.reference_masters['D'].append(dict(client.reference_masters['D'][0], ts_code='600002.SH', symbol='600002'))
    client.reference_names.append(dict(client.reference_names[0], ts_code='600002.SH'))
    for endpoint in ('daily', 'daily_basic', 'adj_factor', 'stk_limit'):
        second = dict(responses[endpoint][0], ts_code='600002.SH')
        if endpoint == 'daily_basic': second['pe_ttm'] = None
        responses[endpoint].append(second)
    client.first_daily = [] if first_kind == 'empty' else responses['daily'][:1]
    def refresh(current, start, end):
        acquire_market(Acquirer(current.store, client, request_interval=0), current.references, start, end,
                       pb_fetcher=lambda *args: pytest.fail('unexpected PB'))
    kwargs = dict(config=Config(), reference_dataset=dataset, closed_session=date(2026, 1, 9), reference_refresh=refresh)
    with pytest.raises(ValueError):
        screen('dual_low', **kwargs)
    assert dataset.last_available_date == date(2026, 1, 8)
    with pytest.raises(DataUnavailable, match='batch_missing'):
        dataset.store.batch('daily', dict(trade_date='20260109'))
    original_attempts = dataset.store.db.execute('SELECT id,sha,raw,fetched_at FROM attempts').fetchall()
    result = screen('dual_low', **kwargs)
    assert result.picks and dataset.last_available_date == date(2026, 1, 9)
    assert dataset.store.db.execute('SELECT id,sha,raw,fetched_at FROM attempts ORDER BY id').fetchall()[:len(original_attempts)] == original_attempts
    assert client.calls.count('daily') == 2
    dataset.store.close()


def test_direct_empty_attempt_is_not_reused_as_success(tmp_path):
    import pandas as pd
    from src.services.screening.backtest_data import BatchStore, Acquirer, FIELDS
    store = BatchStore(tmp_path / 'attempts.sqlite')
    calls = []
    class Client:
        def query(self, endpoint, **params):
            calls.append(endpoint)
            rows = [] if len(calls) == 1 else [dict(ts_code='600001.SH', trade_date='20260105',
                open=10., high=11., low=9., close=10., vol=1., pct_chg=0., amount=1.)]
            return pd.DataFrame(rows, columns=FIELDS[endpoint].split(','))
    acquirer = Acquirer(store, Client(), request_interval=0)
    assert acquirer.batch('daily', trade_date='20260105') == []
    assert len(acquirer.batch('daily', trade_date='20260105')) == 1
    assert len(calls) == 2 and store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 2
    store.close()


def test_ordinary_retries_pending_pb_without_recapturing_primary(tmp_path, monkeypatch):
    from src.services.screening.backtest_data import Acquirer, acquire_market
    from src.services.screening.pipeline import screen
    from src.services.screening.config import Config
    from tests.test_screening_compute_only import forbid_remote
    dataset, client, responses = incremental_fixture(tmp_path)
    responses['daily_basic'][0]['pb'] = None
    forbid_remote(monkeypatch)
    count = []
    def pb(*args):
        count.append(args)
        if len(count) == 1:
            raise RuntimeError('injected interruption')
        return [dict(code='sh.600001', date='2026-01-09', close='10.', peTTM='8.', pbMRQ='1.')]
    def refresh(current, start, end):
        acquire_market(Acquirer(current.store, client, request_interval=0), current.references, start, end, pb_fetcher=pb)
    kwargs = dict(config=Config(), reference_dataset=dataset, closed_session=date(2026, 1, 9), reference_refresh=refresh)
    with pytest.raises(ValueError, match='pb_supplement_request_failed'):
        screen('dual_low', **kwargs)
    assert dataset.last_available_date == date(2026, 1, 8)
    original = {endpoint: dataset.store.batch(endpoint, dict(trade_date='20260109'))
                for endpoint in ('daily', 'daily_basic', 'adj_factor', 'stk_limit', 'suspend_d')}
    result = screen('dual_low', **kwargs)
    assert result.picks and len(count) == 2 and client.calls.count('daily') == 1
    assert dataset.last_available_date == date(2026, 1, 9)
    assert all(dataset.store.batch(endpoint, dict(trade_date='20260109')) == value for endpoint, value in original.items())
    dataset.store.close()


def test_warmup_acquisition_does_not_require_unpublished_classification(tmp_path, monkeypatch):
    from src.services.screening.backtest_data import Acquirer, acquire_market, BatchStore
    dataset, client, _ = incremental_fixture(tmp_path)
    references = dataset.references
    monkeypatch.setattr(references, 'classify', lambda *args: pytest.fail('warmup is not a signal'))
    query = client.query
    def dated(endpoint, **params):
        value = query(endpoint, **params)
        if 'trade_date' in value:
            value['trade_date'] = params['trade_date']
        return value
    client.query = dated
    store = BatchStore(tmp_path / 'warmup.sqlite')
    acquired = acquire_market(Acquirer(store, client, request_interval=0), references,
        date(2025, 3, 3), date(2025, 3, 3), pb_fetcher=lambda *args: pytest.fail('warmup PB not scored'))
    assert acquired.last_available_date == date(2025, 3, 3)
    assert store.batch('daily', dict(trade_date='20250303'))[0]
    store.close()
    dataset.store.close()


LEGACY_MASTER = dict(ts_code='T600018.SH', symbol='T600018', name='上港集箱(退)',
                     list_date='20000719', delist_date='20061020', list_status='D', exchange='SSE')


@pytest.mark.parametrize('count', [1, 30])
def test_reference_vintage_lookup_is_constant_per_rebuild_and_refreshes(tmp_path, count):
    from src.services.screening.backtest_data import BatchStore, HistoricalDataset
    store = BatchStore(tmp_path / 'vintage-count.sqlite')
    dataset = HistoricalDataset(store, bundle())
    queries = []
    store.db.set_trace_callback(queries.append)
    for vintage, label in [('2026-01-05', '旧名'), ('2026-01-06', '新名')]:
        masters = [dict(ts_code=f'{600001 + i}.SH', symbol=str(600001 + i), name=label,
                        list_date='20000101', delist_date=None, list_status='L', exchange='SSE')
                   for i in range(count)]
        for status in ('L', 'D', 'P'):
            store.put('stock_basic', dict(exchange='', list_status=status, reference_vintage=vintage),
                      masters if status == 'L' else [], vintage + 'T08:00:00+00:00')
        names = [dict(ts_code=row['ts_code'], name=label, start_date='20000101',
                      end_date=None, ann_date='19991230') for row in masters]
        store.put('namechange', dict(reference_vintage=vintage), names, vintage + 'T08:00:00+00:00')
        # Exercise both uncached reconstructions; no lifetime-level vintage cache.
        dataset._masters = dataset._names = None
        queries.clear()
        assert len(dataset.masters()) == count
        assert sum('SELECT MAX(json_extract' in query for query in queries) == 1
        queries.clear()
        assert len(dataset.names()) == count
        assert {row['name'] for rows in dataset.names().values() for row in rows} == {label}
        assert sum('SELECT MAX(json_extract' in query for query in queries) == 1
    store.close()


class LegacyListingClient:
    """Leaf responses only: actual acquisition, universe and name audit run."""
    def __init__(self, legacy):
        self.legacy, self.calls = legacy, []

    def query(self, endpoint, **params):
        import pandas as pd
        from src.services.screening.backtest_data import FIELDS
        self.calls.append((endpoint, params))
        identity = dict(ts_code='600001.SH', trade_date=params.get('trade_date'))
        rows = {
            'daily': [dict(identity, open=10., high=11., low=9., close=10., vol=10000., pct_chg=0., amount=1e6)],
            'daily_basic': [dict(identity, turnover_rate=2., volume_ratio=1.2, pe_ttm=8., pb=1., total_mv=1e6, circ_mv=8e5, dv_ttm=0.)],
            'adj_factor': [dict(identity, adj_factor=1.)], 'suspend_d': [],
            'stk_limit': [dict(identity, up_limit=11., down_limit=9.)],
            'index_daily': [dict(ts_code='000300.SH', trade_date=params.get('trade_date'), open=100., close=100.)],
            'namechange': [dict(ts_code='600001.SH', name='正常', start_date='20000101',
                                end_date=None, ann_date='19991230', change_reason='上市')],
            'stock_basic': [self.legacy, dict(ts_code='600001.SH', symbol='600001', name='后续退市',
                list_date='20000101', delist_date='20260130', list_status='D', exchange='SSE')]
                if params.get('list_status') == 'D' else [],
        }[endpoint]
        return pd.DataFrame(rows, columns=FIELDS[endpoint].split(','))


@pytest.mark.parametrize('mode', ['complete', 'missing_bar', 'full_day_conflict'])
def test_acquire_mixed_resumption_intraday_events_preserves_price_and_execution_guards(tmp_path, mode):
    import pandas as pd
    from src.services.screening.backtest_data import Acquirer, acquire_market, BatchStore, FIELDS
    class Client(LegacyListingClient):
        def query(self, endpoint, **params):
            table = super().query(endpoint, **params)
            if endpoint == 'suspend_d':
                identity = dict(ts_code='600001.SH', trade_date=params['trade_date'])
                return pd.DataFrame([dict(identity, suspend_type='R', suspend_timing=None),
                    dict(identity, suspend_type='S', suspend_timing=None if mode == 'full_day_conflict'
                         else '09:30-09:41,09:44-09:54')], columns=FIELDS[endpoint].split(','))
            return table.iloc[:0] if mode == 'missing_bar' and endpoint == 'daily' else table
    store = BatchStore(tmp_path / 'mixed-events.sqlite')
    acquirer = Acquirer(store, Client(dict(LEGACY_MASTER)), request_interval=0)
    target = date(2026, 1, 5) if mode == 'full_day_conflict' else date(2025, 4, 28)
    if mode != 'complete':
        reason = 'suspension_events_conflict' if mode == 'full_day_conflict' else 'primary_date_coverage_incomplete_retry'
        with pytest.raises(ValueError, match=reason):
            acquire_market(acquirer, bundle(), target, target)
        assert store.date_status(target, bundle().hash) is None
    else:
        dataset = acquire_market(acquirer, bundle(), date(2025, 4, 28), date(2025, 4, 28))
        assert store.date_status('20250428', bundle().hash) == 'complete'
        assert len(store.batch('suspend_d', dict(trade_date='20250428'))[0]) == 2
        execution = dataset.execution('600001', date(2025, 4, 28))
        assert execution['suspended'] is False
        assert execution['suspended_open'] is True and execution['suspended_close'] is False
        assert execution['open'] == execution['close'] == 10.
    store.close()


@pytest.mark.parametrize('missing', [None, 'daily', 'daily_basic', 'adj_factor'])
def test_raw_warmup_present_bars_do_not_certify_ambiguous_event_timing(tmp_path, missing):
    import pandas as pd
    from data_provider.historical_reference import digest
    from src.services.screening.backtest_data import Acquirer, acquire_market, BatchStore
    from src.services.screening.eligibility import validate_history_sessions, suspension_state
    target = date(2025, 6, 10)
    events = [dict(ts_code='600001.SH', trade_date='20250610', suspend_type=kind, suspend_timing=None)
              for kind in ('R', 'S')]
    class Client(LegacyListingClient):
        def query(self, endpoint, **params):
            table = super().query(endpoint, **params)
            if endpoint == 'suspend_d':
                return pd.DataFrame(events)
            return table.iloc[:0] if endpoint == missing else table
    store = BatchStore(tmp_path / 'warmup-event-timing.sqlite')
    client = Client(dict(LEGACY_MASTER))
    acquirer = Acquirer(store, client, request_interval=0)
    if missing:
        reason = 'primary_factor_coverage_incomplete_retry' if missing == 'adj_factor' else 'suspension_events_conflict'
        if missing == 'daily':
            reason = 'suspension_proof_not_applicable'
        with pytest.raises(ValueError, match=reason):
            acquire_market(acquirer, bundle(), target, target, suspension_fetcher=lambda *args: [])
        assert store.date_status(target, bundle().hash) is None
        assert store.artifact('warmup_event_timing_v1:20250610') is None
    else:
        dataset = acquire_market(acquirer, bundle(), target, target)
        assert store.date_status(target, bundle().hash) == 'complete'
        raw, batch_hash, _ = store.batch('suspend_d', dict(trade_date='20250610'))
        assert raw == events
        assert store.artifact('warmup_event_timing_v1:20250610') == dict(
            scope='raw_warmup_only', business_date='2025-06-10', reference_hash=bundle().hash,
            suspension_batch_sha256=batch_hash, unresolved_events=[dict(code='600001',
                error='suspension_events_conflict', events=events, events_sha256=digest(events))])
        assert store.row('daily', '600001.SH', target)['close'] == 10.
        assert store.row('adj_factor', '600001.SH', target)['adj_factor'] == 1.
        with pytest.raises(ValueError, match='suspension_events_conflict'):
            dataset.execution('600001', target)
        with pytest.raises(ValueError, match='suspension_events_conflict'):
            suspension_state(raw)
        with pytest.raises(ValueError, match='suspension_events_conflict'):
            validate_history_sessions([target], [], lambda stamp: raw)
        # Complete raw-date resume reuses accepted evidence and the audit verbatim.
        receipt = store.artifact('warmup_event_timing_v1:20250610')
        calls = len(client.calls)
        acquire_market(Acquirer(store, client, request_interval=0), bundle(), target, target)
        assert len(client.calls) == calls
        assert store.artifact('warmup_event_timing_v1:20250610') == receipt
    store.close()


@pytest.mark.parametrize('explained', [True, False])
def test_acquire_delisting_effective_date_is_not_a_missing_listed_bar(tmp_path, explained):
    import pandas as pd
    from src.services.screening.backtest_data import Acquirer, acquire_market, BatchStore, DataUnavailable, FIELDS
    survivor = dict(ts_code='600001.SH', symbol='600001', name='正常', list_date='20000101',
                    delist_date=None, list_status='L', exchange='SSE')
    delisted = dict(ts_code='603963.SH', symbol='603963', name='后续退市', list_date='20170922',
                    delist_date='20250321', list_status='D', exchange='SSE')
    class Client(LegacyListingClient):
        def query(self, endpoint, **params):
            table = super().query(endpoint, **params)
            if endpoint == 'stock_basic':
                rows = {'L': [survivor], 'D': [delisted], 'P': []}[params['list_status']]
            elif endpoint == 'namechange':
                rows = [dict(ts_code=row['ts_code'], name='正常', start_date=row['list_date'],
                    end_date=None, ann_date=row['list_date'], change_reason='上市') for row in (survivor, delisted)]
            elif endpoint == 'suspend_d':
                rows = [dict(ts_code='603963.SH', trade_date='20250320', suspend_type='S', suspend_timing=None)] \
                    if explained and params['trade_date'] == '20250320' else []
            else:
                return table  # Only survivor has prices on either day.
            return pd.DataFrame(rows, columns=FIELDS[endpoint].split(','))
    store = BatchStore(tmp_path / 'delisting-boundary.sqlite')
    client = Client({})
    if not explained:
        with pytest.raises(DataUnavailable, match='primary_date_coverage_incomplete_retry'):
            acquire_market(Acquirer(store, client, request_interval=0), bundle(), date(2025, 3, 20), date(2025, 3, 21))
        assert store.date_status('20250320', bundle().hash) is None
    else:
        dataset = acquire_market(Acquirer(store, client, request_interval=0), bundle(), date(2025, 3, 20), date(2025, 3, 21))
        assert set(dataset.universe('2025-03-11')) == {'600001', '603963'}
        assert set(dataset.universe('2025-03-20')) == {'600001', '603963'}
        assert set(dataset.universe('2025-03-21')) == {'600001'}
        assert '603963' not in dataset.universe('2017-09-21')
        assert '603963' in dataset.universe('2017-09-22')
        assert store.date_status('20250321', bundle().hash) == 'complete'
        assert store.batch('suspend_d', dict(trade_date='20250321'))[0] == []
        assert dataset.execution('603963', date(2025, 3, 20)) == dict(suspended=True)
        # Membership removal is not an observed executable exit or a cash fill.
        with pytest.raises(DataUnavailable, match='execution_row_missing_or_conflicting:daily'):
            dataset.execution('603963', date(2025, 3, 21))
    store.close()


@pytest.mark.parametrize('delisted_on', ['20061020', '20250311'])
def test_legacy_listing_is_audited_raw_and_never_aliases_or_removes_later_delisting(tmp_path, delisted_on):
    from data_provider.historical_reference import digest
    from src.services.screening.backtest_data import Acquirer, acquire_market, BatchStore, DataUnavailable, HistoricalDataset
    store = BatchStore(tmp_path / 'legacy.sqlite')
    legacy_master = dict(LEGACY_MASTER, delist_date=delisted_on)
    client = LegacyListingClient(legacy_master)
    query = client.query
    legacy_name = dict(ts_code='T600018.SH', name='上港集箱', start_date='20000719',
                       end_date=None, ann_date='20000718', change_reason='上市')
    def with_legacy_name(endpoint, **params):
        import pandas as pd
        table = query(endpoint, **params)
        return pd.concat([table, pd.DataFrame([legacy_name])], ignore_index=True) if endpoint == 'namechange' else table
    client.query = with_legacy_name
    dataset = acquire_market(Acquirer(store, client, request_interval=0), bundle(),
                             date(2025, 3, 11), date(2025, 3, 11))
    rows, batch_hash, _ = store.batch('stock_basic', dataset.reference_params(exchange='', list_status='D'))
    assert legacy_master in rows
    assert dataset.masters()['T600018.SH'] == legacy_master
    assert set(dataset.universe('2025-03-11')) == {'600001'}
    assert set(dataset.universe('2026-01-29')) == {'600001'}
    assert dataset.universe('2026-01-30') == {}  # Delisting effective date is exclusive.
    assert dataset.universe('2026-01-31') == {}
    assert '600018' not in dataset.masters()
    audit = store.artifact('legacy_listing_scope_v1')
    assert audit['coverage_start'] == '2025-03-11'
    assert audit['records'] == [dict(row=legacy_master, row_sha256=digest(legacy_master),
        batch_sha256=batch_hash, reason='legacy_listing_ended_before_or_at_coverage')]
    name_rows, name_hash, _ = store.batch('namechange', dataset.reference_params())
    assert legacy_name in name_rows
    assert dataset.names()['T600018.SH'] == [legacy_name]
    assert '600018' not in dataset.names()
    assert audit['name_records'] == [dict(row=legacy_name, row_sha256=digest(legacy_name),
        batch_sha256=name_hash, reason='bound_to_out_of_period_legacy_listing')]
    assert not any(endpoint == 'namechange' and params.get('ts_code') == 'T600018.SH'
                   for endpoint, params in client.calls)
    # Reading saved batches cannot silently use the legacy exclusion for older dates.
    reopened = HistoricalDataset(store, bundle())
    with pytest.raises(DataUnavailable, match='legacy_listing_overlaps_requested_date'):
        reopened.universe('2006-10-19')
    assert 'T600018.SH' not in reopened.universe(delisted_on)
    calls = len(client.calls)
    acquire_market(Acquirer(store, client, request_interval=0), bundle(), date(2025, 3, 11), date(2025, 3, 11))
    assert len(client.calls) == calls
    store.close()


def test_unjoined_name_identity_is_retained_but_not_accepted_as_security(tmp_path):
    import pandas as pd
    from src.services.screening.backtest_data import Acquirer, acquire_market, BatchStore, DataUnavailable
    store = BatchStore(tmp_path / 'legacy-name-invalid.sqlite')
    client = LegacyListingClient(dict(LEGACY_MASTER))
    query = client.query
    def unknown_name(endpoint, **params):
        table = query(endpoint, **params)
        if endpoint == 'namechange':
            return pd.concat([table, pd.DataFrame([dict(ts_code='X19363.SH', name='未知参考',
                start_date='20000101', end_date=None, ann_date='19991230', change_reason='上市')])], ignore_index=True)
        return table
    client.query = unknown_name
    dataset = acquire_market(Acquirer(store, client, request_interval=0), bundle(), date(2025, 3, 11), date(2025, 3, 11))
    assert set(dataset.names()) == {'600001'}
    assert set(dataset.universe('2025-03-11')) == {'600001'}
    audit = store.artifact('legacy_listing_scope_v1')['unmatched_name_records']
    assert len(audit) == 1 and audit[0]['row']['ts_code'] == 'X19363.SH'
    assert audit[0]['reason'] == 'not_in_validated_listing_master'
    store.close()


@pytest.mark.parametrize('repair', ['wrong_identity', 'empty'])
def test_unjoined_names_cannot_fill_real_master_name_gap(tmp_path, repair):
    from src.services.screening.backtest_data import Acquirer, acquire_market, BatchStore
    store = BatchStore(tmp_path / 'name-gap.sqlite')
    client = LegacyListingClient(dict(LEGACY_MASTER))
    query = client.query
    def wrong_name(endpoint, **params):
        table = query(endpoint, **params)
        if endpoint == 'namechange':
            table['ts_code'] = 'X600001.SH'
            if repair == 'empty' and 'ts_code' in params:
                return table.iloc[:0]
        return table
    client.query = wrong_name
    with pytest.raises(ValueError, match='historical_name_identity_conflict|name_state_unknown'):
        acquire_market(Acquirer(store, client, request_interval=0), bundle(), date(2026, 1, 5), date(2026, 1, 5))
    assert store.date_status('20260105', bundle().hash) is None
    store.close()


@pytest.mark.parametrize('change', [
    dict(delist_date='20250312'), dict(delist_date='20260130'), dict(delist_date=None),
    dict(list_date=None), dict(list_date='20060230'), dict(delist_date='2006bad'),
    dict(list_date='20071020'), dict(list_status='L'), dict(symbol='600018'),
    dict(ts_code='X600018.SH'), dict(exchange='SZSE'),
])
def test_legacy_listing_unknown_overlapping_or_malformed_fails_closed(tmp_path, change):
    from src.services.screening.backtest_data import Acquirer, acquire_market, BatchStore, DataUnavailable
    store = BatchStore(tmp_path / 'legacy-invalid.sqlite')
    with pytest.raises(DataUnavailable, match='source_identity_invalid'):
        acquire_market(Acquirer(store, LegacyListingClient(dict(LEGACY_MASTER, **change)), request_interval=0),
                       bundle(), date(2025, 3, 11), date(2025, 3, 11))
    assert store.artifact('legacy_listing_scope_v1') is None
    store.close()


def test_legacy_listing_earlier_acquisition_boundary_is_not_silently_skipped(tmp_path):
    from src.services.screening.backtest_data import Acquirer, acquire_market, BatchStore, DataUnavailable
    store = BatchStore(tmp_path / 'legacy-earlier.sqlite')
    with pytest.raises(DataUnavailable, match='legacy_listing_overlaps_coverage'):
        acquire_market(Acquirer(store, LegacyListingClient(dict(LEGACY_MASTER)), request_interval=0),
                       bundle(), date(2006, 10, 19), date(2006, 10, 19))
    store.close()
