import apiClient from './index';
import type { ResearchHistory, ResearchHistoryBar } from '../types/researchHistory';

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}
function finiteOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}
function validBarDate(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

export type ExtractItem = {
  code?: string | null;
  name?: string | null;
  confidence: string;
};

export type ExtractFromImageResponse = {
  codes: string[];
  items?: ExtractItem[];
  rawText?: string;
};

export const stocksApi = {
  async getHistory(stockCode: string, days = 90, signal?: AbortSignal): Promise<ResearchHistory> {
    const code = stockCode.trim().toUpperCase();
    // The existing endpoint is an equity/ETF contract, not a generic Yahoo symbol proxy.
    if (!/^(?:\d{6}|[A-Z]{1,5}(?:\.[A-Z])?)$/.test(code) || !Number.isInteger(days) || days < 1 || days > 365) throw new Error('Invalid daily-history request');
    const response = await apiClient.get(`/api/v1/stocks/${encodeURIComponent(code)}/history`, {
      params: { period: 'daily', days }, signal, timeout: 90000,
    });
    const raw: unknown = response.data;
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid history response');
    const payload = raw as Record<string, unknown>;
    if (typeof payload.stock_code !== 'string' || payload.stock_code.toUpperCase() !== code || !Array.isArray(payload.data)) throw new Error('History instrument or data mismatch');
    if (payload.period !== 'daily') throw new Error('History response period must be daily');
    const warnings = Array.isArray(payload.warnings) ? payload.warnings.filter((item): item is string => typeof item === 'string') : [];
    const parsed: ResearchHistoryBar[] = payload.data.flatMap((item: unknown) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
      const row = item as Record<string, unknown>;
      const open = finiteOrNull(row.open); const high = finiteOrNull(row.high);
      const low = finiteOrNull(row.low); const close = finiteOrNull(row.close);
      if (!validBarDate(row.date) || open === null || high === null || low === null || close === null) return [];
      if (open <= 0 || high <= 0 || low <= 0 || close <= 0
        || high < Math.max(open, close, low) || low > Math.min(open, close, high)) return [];
      return [{ date: row.date, open, high, low, close, volume: finiteOrNull(row.volume), amount: finiteOrNull(row.amount), changePercent: finiteOrNull(row.change_percent) }];
    });
    const data = [...new Map(parsed.map((item) => [item.date, item])).values()].sort((left, right) => left.date.localeCompare(right.date));
    if (parsed.length !== payload.data.length) warnings.push(`client-invalid-bars-skipped:${payload.data.length - parsed.length}`);
    if (data.length !== parsed.length) warnings.push(`client-duplicate-dates-skipped:${parsed.length - data.length}`);
    const source = stringOrNull(payload.source);
    const fetchedAtRaw = stringOrNull(payload.fetched_at);
    const fetchedAt = fetchedAtRaw && /^\d{4}-\d{2}-\d{2}T/.test(fetchedAtRaw) && Number.isFinite(Date.parse(fetchedAtRaw)) ? fetchedAtRaw : null;
    const adjustment = stringOrNull(payload.adjustment);
    const statuses: ResearchHistory['status'][] = ['available', 'partial', 'empty', 'unavailable'];
    let status: ResearchHistory['status'] = statuses.includes(payload.status as ResearchHistory['status']) ? payload.status as ResearchHistory['status'] : 'unknown';
    if (status === 'available') {
      if (payload.data.length === 0) status = 'empty';
      else if (data.length === 0 || parsed.length !== payload.data.length || data.length !== parsed.length || !source) status = 'partial';
    }
    if (!source) warnings.push('source-unknown');
    if (!fetchedAt) warnings.push('request-time-unknown');
    if (!adjustment) warnings.push('adjustment-unknown');
    warnings.push('freshness-not-verified');
    return { stockCode: code, stockName: stringOrNull(payload.stock_name), period: 'daily', data, source, fetchedAt, adjustment, status, warnings: [...new Set(warnings)] };
  },

  async extractFromImage(file: File): Promise<ExtractFromImageResponse> {
    const formData = new FormData();
    formData.append('file', file);

    const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
    const response = await apiClient.post(
      '/api/v1/stocks/extract-from-image',
      formData,
      {
        headers,
        timeout: 60000, // Vision API can be slow; 60s
      },
    );

    const data = response.data as { codes?: string[]; items?: ExtractItem[]; raw_text?: string };
    return {
      codes: data.codes ?? [],
      items: data.items,
      rawText: data.raw_text,
    };
  },

  async parseImport(file?: File, text?: string): Promise<ExtractFromImageResponse> {
    if (file) {
      const formData = new FormData();
      formData.append('file', file);
      const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
      const response = await apiClient.post('/api/v1/stocks/parse-import', formData, { headers });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    if (text) {
      const response = await apiClient.post('/api/v1/stocks/parse-import', { text });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    throw new Error('请提供文件或粘贴文本');
  },
};
