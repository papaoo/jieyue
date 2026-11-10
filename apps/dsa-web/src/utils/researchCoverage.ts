import type { DataCapabilityOverview, DataProviderCapability, DatasetQualityStatus } from '../types/dataCapability';

export type ResearchAsset = 'cn' | 'us' | 'gold' | 'crypto';
type CoverageState = 'candidate' | 'partial' | 'unknown' | 'unconfigured' | 'unavailable' | 'not-integrated';
export type ResearchProvider = { provider: DataProviderCapability; datasets: string[]; usable: boolean };
export type ResearchEvidence = {
  dataset: string; status: DatasetQualityStatus; source: string | null;
  lastSuccess: string | null; observed: boolean; freshness: 'stale' | 'not-stale' | 'unknown';
  lastError: string | null; warnings: string[]; fallbackFrom: string[];
};
export type ResearchCoverage = {
  state: CoverageState; providers: ResearchProvider[]; evidence: ResearchEvidence[]; observedCount: number;
};

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}
function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}
function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}
function timestamp(value: unknown): string | null {
  const candidate = text(value);
  return candidate && /^\d{4}-\d{2}-\d{2}T/.test(candidate) && Number.isFinite(Date.parse(candidate)) ? candidate : null;
}
function quality(value: unknown): DatasetQualityStatus {
  const statuses: DatasetQualityStatus[] = ['ok', 'degraded', 'partial', 'unconfigured', 'unavailable', 'unknown', 'stale'];
  return statuses.includes(value as DatasetQualityStatus) ? value as DatasetQualityStatus : 'unknown';
}

/** Configuration is a routing candidate, never evidence of a live market observation. */
export function deriveResearchCoverage(overview: DataCapabilityOverview | null, asset: ResearchAsset): ResearchCoverage {
  const empty: ResearchCoverage = { state: 'not-integrated', providers: [], evidence: [], observedCount: 0 };
  // Equity coverage does not establish direct spot/futures gold or crypto capability.
  if (!overview || asset === 'gold' || asset === 'crypto') return empty;
  const providers = overview.providers.flatMap((provider) => {
    const datasets = Object.entries(provider.datasetMarkets).filter(([, markets]) =>
      markets.some((market) => market.toLowerCase() === asset),
    ).map(([dataset]) => dataset);
    return datasets.length ? [{
      provider, datasets,
      usable: provider.enabled && provider.configured && !provider.cooldown && (provider.status === 'ok' || provider.status === 'partial'),
    }] : [];
  });
  const usable = providers.filter((item) => item.usable);
  const state: CoverageState = usable.some((item) => item.provider.status === 'ok') ? 'candidate'
    : usable.length ? 'partial'
      : providers.some((item) => item.provider.enabled && item.provider.configured && item.provider.status === 'unknown') ? 'unknown'
        : providers.some((item) => item.provider.enabled && item.provider.configured) ? 'unavailable'
          : providers.length ? 'unconfigured' : 'not-integrated';
  const names = new Set(providers.map((item) => item.provider.name));
  const declaredDatasets = new Set(providers.flatMap((item) => item.datasets));
  const evidence: ResearchEvidence[] = overview.datasets.flatMap((dataset) => {
    const marketMap = record(dataset.coverage?.markets);
    const marketEntry = marketMap
      ? record(Object.entries(marketMap).find(([market]) => market.toLowerCase() === asset)?.[1]) : null;
    if (marketMap && !marketEntry) return [];
    if (!marketEntry && !declaredDatasets.has(dataset.dataset) && !names.has(dataset.source ?? '')) return [];
    const sourceProvider = providers.find((item) => item.provider.name === dataset.source)?.provider;
    const sourceMarkets = sourceProvider?.datasetMarkets[dataset.dataset]?.map((market) => market.toLowerCase()) ?? [];
    const attributable = Boolean(marketEntry) || (sourceMarkets.length > 0 && sourceMarkets.every((market) => market === asset));
    const source = text(marketEntry ? marketEntry.source : attributable ? dataset.source : null);
    // A top-level lastSuccess may belong to another market. Never borrow it for an aggregate entry.
    const lastSuccess = timestamp(marketEntry ? marketEntry.last_success : attributable ? dataset.lastSuccess : null);
    const stale = marketEntry ? marketEntry.stale : attributable ? dataset.stale : null;
    return [{
      dataset: dataset.dataset,
      status: quality(marketEntry ? marketEntry.status : attributable ? dataset.status : 'unknown'), source, lastSuccess,
      observed: Boolean(source && lastSuccess),
      freshness: stale === true ? 'stale' : stale === false ? 'not-stale' : 'unknown',
      lastError: text(marketEntry ? marketEntry.last_error : attributable ? dataset.lastError : null),
      warnings: [...new Set([...dataset.warnings, ...strings(marketEntry?.warnings), ...(attributable ? [] : ['market-attribution-unverified'])])],
      fallbackFrom: marketEntry ? strings(marketEntry.fallback_from) : attributable ? dataset.fallbackFrom : [],
    }];
  });
  return { state, providers, evidence, observedCount: evidence.filter((item) => item.observed).length };
}
