import { beforeEach, describe, expect, it, vi } from 'vitest';
import { stocksApi } from '../stocks';

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('../index', () => ({ default: { get } }));
const row = { date: '2026-10-08', open: 3, high: 4, low: 2, close: 3.5, volume: 0, amount: 0, change_percent: 0 };
beforeEach(() => vi.clearAllMocks());

describe('stocksApi.getHistory', () => {
  it('maps actual source metadata, preserving zero optional values', async () => {
    get.mockResolvedValue({ data: { stock_code: '510300', period: 'daily', data: [row], source: 'baostock', fetched_at: '2026-10-09T01:00:00Z', status: 'available', adjustment: null, warnings: ['freshness-not-verified'] } });
    const signal = new AbortController().signal;
    const result = await stocksApi.getHistory('510300', 90, signal);
    expect(get).toHaveBeenCalledWith('/api/v1/stocks/510300/history', expect.objectContaining({ params: { period: 'daily', days: 90 }, signal }));
    expect(result).toMatchObject({ source: 'baostock', fetchedAt: '2026-10-09T01:00:00Z', status: 'available', adjustment: null, data: [{ volume: 0, amount: 0, changePercent: 0 }] });
  });
  it('does not promote a legacy payload with missing metadata to healthy', async () => {
    get.mockResolvedValue({ data: { stock_code: 'AAPL', period: 'daily', data: [row] } });
    expect(await stocksApi.getHistory('AAPL', 30)).toMatchObject({ source: null, fetchedAt: null, status: 'unknown', adjustment: null });
  });
  it('rejects crypto symbols and preserves failures rather than an empty successful chart', async () => {
    await expect(stocksApi.getHistory('BTC-USD', 30)).rejects.toThrow();
    expect(get).not.toHaveBeenCalled();
    get.mockRejectedValueOnce(new Error('network failure'));
    await expect(stocksApi.getHistory('510300', 30)).rejects.toThrow('network failure');
  });
  it('rejects mismatched instruments and skips non-finite or invalid-date rows', async () => {
    get.mockResolvedValueOnce({ data: { stock_code: '600519', data: [row] } });
    await expect(stocksApi.getHistory('510300', 30)).rejects.toThrow();
    get.mockResolvedValueOnce({ data: { stock_code: '510300', period: 'daily', data: [row, { ...row, close: null }, { ...row, date: '2026-02-31' }], status: 'available', source: 'sample' } });
    const result = await stocksApi.getHistory('510300', 30);
    expect(result.data).toHaveLength(1);
    expect(result.status).toBe('partial');
    expect(result.warnings).toContain('client-invalid-bars-skipped:2');
  });
  it.each(['weekly', 'monthly', '', undefined])('rejects a non-daily or missing response period: %s', async (period) => {
    get.mockResolvedValueOnce({ data: { stock_code: '510300', period, data: [row], status: 'available', source: 'sample' } });
    await expect(stocksApi.getHistory('510300', 30)).rejects.toThrow(/period/i);
  });
  it.each([
    { open: 0 }, { open: -1 }, { high: 0 }, { high: -1 },
    { low: 0 }, { low: -1 }, { close: 0 }, { close: -1 },
    { high: 3 }, { low: 3.2 }, { high: 2, low: 4 },
  ])('filters non-positive prices and impossible OHLC envelopes: %j', async (invalidPrices) => {
    get.mockResolvedValueOnce({ data: { stock_code: '510300', period: 'daily',
      data: [row, { ...row, date: '2026-10-09', ...invalidPrices }], status: 'available', source: 'sample',
    } });
    const result = await stocksApi.getHistory('510300', 30);
    expect(result.data).toEqual([{ date: row.date, open: row.open, high: row.high, low: row.low, close: row.close, volume: 0, amount: 0, changePercent: 0 }]);
    expect(result.status).toBe('partial');
    expect(result.warnings).toContain('client-invalid-bars-skipped:1');
  });
  it('does not expose an available status when every price row is invalid', async () => {
    get.mockResolvedValueOnce({ data: { stock_code: '510300', period: 'daily',
      data: [{ ...row, open: 0 }, { ...row, close: -1 }], status: 'available', source: 'sample',
    } });
    const result = await stocksApi.getHistory('510300', 30);
    expect(result.data).toEqual([]);
    expect(result.status).toBe('partial');
    expect(result.warnings).toContain('client-invalid-bars-skipped:2');
  });
  it('treats an available but empty response as empty, not available', async () => {
    get.mockResolvedValueOnce({ data: { stock_code: '510300', period: 'daily', data: [], status: 'available', source: 'sample' } });
    expect(await stocksApi.getHistory('510300', 30)).toMatchObject({ status: 'empty', data: [] });
  });
});
