import { describe, expect, it } from 'vitest';
import type { DataCapabilityOverview, DataProviderCapability } from '../../types/dataCapability';
import { deriveResearchCoverage } from '../researchCoverage';

const provider: DataProviderCapability = {
  name: 'sample', label: 'Sample', enabled: true, configured: true, status: 'ok',
  markets: ['cn', 'us'], datasets: ['kline.daily'],
  datasetMarkets: { 'kline.daily': ['CN', 'US'] }, warnings: [],
};
const overview = (providers = [provider]): DataCapabilityOverview => ({
  asOf: '2026-10-09T09:00:00Z', providers, datasets: [], priorities: [], warnings: [],
});

describe('deriveResearchCoverage', () => {
  it('uses dataset-level markets, not broad provider markets', () => {
    const result = deriveResearchCoverage(overview([{ ...provider, datasetMarkets: { 'kline.daily': ['US'] } }]), 'cn');
    expect(result.providers).toHaveLength(0);
    expect(result.state).toBe('not-integrated');
  });
  it('normalizes market identifiers and distinguishes candidates from observed data', () => {
    const result = deriveResearchCoverage(overview(), 'cn');
    expect(result.state).toBe('candidate');
    expect(result.providers[0].datasets).toEqual(['kline.daily']);
    expect(result.observedCount).toBe(0);
  });
  it('does not treat unconfigured, disabled or unknown providers as usable candidates', () => {
    const result = deriveResearchCoverage(overview([
      { ...provider, name: 'disabled', enabled: false },
      { ...provider, name: 'no-key', configured: false },
      { ...provider, name: 'unknown', status: 'unknown' },
    ]), 'us');
    expect(result.providers.every((item) => !item.usable)).toBe(true);
    expect(result.state).toBe('unknown');
  });
  it('never infers direct gold or crypto coverage from equity providers', () => {
    for (const asset of ['gold', 'crypto'] as const) {
      const result = deriveResearchCoverage(overview(), asset);
      expect(result.state).toBe('not-integrated');
      expect(result.providers).toEqual([]);
      expect(result.evidence).toEqual([]);
    }
  });
  it('does not inherit aggregate source or success timestamps across markets', () => {
    const data = overview();
    data.datasets = [{ dataset: 'kline.daily', status: 'ok', source: 'sample', stale: false,
      lastSuccess: '2026-10-09T08:00:00Z', fallbackFrom: [], warnings: [],
      coverage: { markets: { cn: { status: 'partial', source: 'sample', warnings: ['probe_unknown'] } } },
    }];
    const result = deriveResearchCoverage(data, 'cn');
    expect(result.evidence[0]).toMatchObject({ status: 'partial', lastSuccess: null, observed: false, freshness: 'unknown' });
    expect(result.observedCount).toBe(0);
    expect(result.evidence[0].warnings).toContain('probe_unknown');
  });
  it('separates an old successful observation from stale freshness and current failure', () => {
    const data = overview([{ ...provider, datasetMarkets: { 'kline.daily': ['us'] } }]);
    data.datasets = [{ dataset: 'kline.daily', status: 'unavailable', source: 'sample', stale: true,
      lastSuccess: '2026-10-08T08:00:00Z', lastError: 'timeout', fallbackFrom: [], warnings: [], coverage: null,
    }];
    expect(deriveResearchCoverage(data, 'us').evidence[0]).toMatchObject({
      status: 'unavailable', observed: true, freshness: 'stale', lastError: 'timeout',
    });
  });
  it('does not borrow a CN-only source observation for a US provider sharing its dataset ID', () => {
    const data = overview([
      { ...provider, name: 'cn-source', datasetMarkets: { 'kline.daily': ['cn'] } },
      { ...provider, name: 'us-source', datasetMarkets: { 'kline.daily': ['us'] } },
    ]);
    data.datasets = [{ dataset: 'kline.daily', status: 'ok', source: 'cn-source', stale: false,
      lastSuccess: '2026-10-09T08:00:00Z', fallbackFrom: [], warnings: [], coverage: null,
    }];
    expect(deriveResearchCoverage(data, 'us').evidence[0]).toMatchObject({ source: null, lastSuccess: null, observed: false, status: 'unknown', freshness: 'unknown' });
  });
  it('keeps a multi-market provider timestamp unattributed without market-level evidence', () => {
    const data = overview();
    data.datasets = [{ dataset: 'kline.daily', status: 'ok', source: 'sample', stale: false,
      lastSuccess: '2026-10-09T08:00:00Z', fallbackFrom: [], warnings: [], coverage: null,
    }];
    expect(deriveResearchCoverage(data, 'us').observedCount).toBe(0);
    expect(deriveResearchCoverage(data, 'cn').evidence[0].warnings).toContain('market-attribution-unverified');
  });
});
