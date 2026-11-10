"""Explicit synthetic canonical references for existing offline collector tests."""
from data_provider.historical_reference import ReferenceBundle, REFERENCE_VERSION


def canonical_frame(frame, signal='2026-10-09'):
    frame = frame.copy()
    frame['industry'] = '合成行业'
    frame['industry_taxonomy'] = 'capco_major_v1'
    frame['industry_code'] = 'J66'
    frame['industry_as_of'] = '2025-10-01'
    frame['industry_source'] = 'synthetic_published_CAPCO'
    frame['industry_evidence_id'] = 'synthetic-reference'
    frame.attrs['signal_date'] = signal
    return frame


def reference_bundle():
    codes = [str(600001 + i) for i in range(1000)] + ['920001', '000001', '300001', '688001']
    return ReferenceBundle(dict(version=REFERENCE_VERSION, aliases=[], aliases_sha256='a' * 64,
        reports=[dict(period='2025H1', published_on='2025-09-30', inclusion_cutoff='2025-06-10',
            valid_through='2026-12-31', pdf_sha256='b' * 64, article_sha256='c' * 64,
            guideline_sha256='d' * 64,
            rows={code: dict(industry='合成行业', industry_code='J66') for code in codes})],
        calendars=[dict(exchange=exchange, year=year, source_sha256='e' * 64,
            derivation='official_annual_closures_weekends_v1', closures=[])
            for exchange in ('SSE', 'SZSE', 'BSE') for year in (2025, 2026)]))
