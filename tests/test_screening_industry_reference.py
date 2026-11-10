from datetime import date
from src.services.research_delivery.evidence import validate_snapshot, CORE_FIELDS
from src.services.screening.pipeline import _df_to_picks
from src.services.screening.risk import apply_portfolio_overlay
from tests.test_research_delivery_preview import verified_snapshot, T


def test_canonical_industry_survives_validation_and_groups_without_aliases():
    frame = verified_snapshot(('600001', '600002', '600003'))
    frame['industry'] = ['银行', '银行', '银行']
    frame['industry_taxonomy'] = ['capco_major_v1', 'capco_major_v1', 'other_v1']
    frame['industry_code'] = 'J66'
    frame['industry_as_of'] = T.isoformat()
    frame['industry_source'] = 'official'
    frame['industry_evidence_id'] = 'reference'
    accepted, _, _ = validate_snapshot(frame, CORE_FIELDS, tuple(frame.code), T)
    picks = _df_to_picks(accepted)
    result, notes = apply_portfolio_overlay(picks, canonical_only=True, signal_date=T)
    assert result[0].industry_taxonomy == 'capco_major_v1'
    assert sum(p.portfolio_penalty > 0 for p in result) == 1


def test_unknown_and_future_industry_unassessed_no_llm_fallback():
    frame = verified_snapshot().drop(columns=['industry_taxonomy', 'industry_code', 'industry_as_of', 'industry_source', 'industry_evidence_id'])
    picks = _df_to_picks(frame)
    for pick in picks:
        pick.industry = pick.llm_sector = '银行'
    result, notes = apply_portfolio_overlay(picks, canonical_only=True, signal_date=T)
    assert all(p.portfolio_penalty == 0 for p in result)
    assert all('industry_unassessed' in p.portfolio_flags for p in result)
    assert notes
    for pick in picks:
        pick.industry_taxonomy, pick.industry_code = 'capco_major_v1', 'J66'
        pick.industry_as_of, pick.industry_source, pick.industry_evidence_id = '2027-01-01', 'official', 'reference'
    result, notes = apply_portfolio_overlay(picks, canonical_only=True, signal_date=T)
    assert all(p.portfolio_penalty == 0 for p in result)


def test_optional_dividend_requires_percent_unit_and_date():
    frame = verified_snapshot()
    frame['dividend_yield_ttm_pct'] = [3., None]
    frame.attrs['field_trade_date_columns']['dividend_yield_ttm_pct'] = 'business_date'
    frame.attrs['dividend_yield_ttm_unit'] = 'percent'
    accepted, receipts, blockers = validate_snapshot(frame, CORE_FIELDS, tuple(frame.code), T)
    assert not blockers and all(r.status == 'valid' for r in receipts)
    assert accepted['dividend_yield_ttm_pct'].iloc[0] == 3.
    assert accepted['dividend_yield_ttm_pct'].isna().iloc[1]
    assert _df_to_picks(accepted)[0].dividend_yield_ttm_as_of == T.isoformat()
    assert _df_to_picks(accepted)[0].dividend_yield_ttm_unit == 'percent'
