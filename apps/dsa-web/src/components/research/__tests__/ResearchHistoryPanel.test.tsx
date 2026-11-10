import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ResearchHistoryPanel from '../ResearchHistoryPanel';
import type { ResearchHistory } from '../../../types/researchHistory';

const { getHistory, lineProps } = vi.hoisted(() => ({ getHistory: vi.fn(), lineProps: vi.fn() }));
vi.mock('../../../api/stocks', () => ({ stocksApi: { getHistory: (...args: unknown[]) => getHistory(...args) } }));
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div data-testid="history-chart">{children}</div>,
  Line: (props: Record<string, unknown>) => { lineProps(props); return null; },
  CartesianGrid: () => null, XAxis: () => null, YAxis: () => null, Tooltip: () => null,
}));
const history: ResearchHistory = {
  stockCode: '510300', stockName: null, period: 'daily', status: 'available', source: 'baostock',
  fetchedAt: '2026-10-09T01:00:00Z', adjustment: null, warnings: ['freshness-not-verified'],
  data: [{ date: '2026-10-08', open: 3, high: 4, low: 2, close: 3.5, volume: 0, amount: null, changePercent: 0 }],
};
beforeEach(() => { vi.clearAllMocks(); getHistory.mockResolvedValue(history); });

describe('ResearchHistoryPanel', () => {
  it('requests only on explicit click and displays provenance without animated lines', async () => {
    render(<ResearchHistoryPanel asset="cn" language="zh" />);
    expect(getHistory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '读取日线' }));
    expect(await screen.findByText('baostock')).toBeInTheDocument();
    expect(getHistory).toHaveBeenCalledWith('510300', 90, expect.any(AbortSignal));
    expect(screen.getByText('2026-10-08')).toBeInTheDocument();
    expect(screen.getByText('2026-10-09T01:00:00Z')).toBeInTheDocument();
    expect(screen.getByText(/复权口径未知/)).toBeInTheDocument();
    expect(lineProps).toHaveBeenCalledWith(expect.objectContaining({ isAnimationActive: false, connectNulls: false, type: 'linear' }));
  });
  it('labels gold as an ETF proxy and never routes BTC through the stock API', async () => {
    const { rerender } = render(<ResearchHistoryPanel asset="gold" language="zh" />);
    expect(screen.getByText(/黄金 ETF 代理，不是现货/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '读取日线' }));
    expect(getHistory).toHaveBeenCalledWith('518880', 90, expect.any(AbortSignal));
    rerender(<ResearchHistoryPanel asset="crypto" language="zh" />);
    expect(screen.getByRole('button', { name: '读取日线' })).toBeDisabled();
    expect(screen.queryByTestId('history-chart')).not.toBeInTheDocument();
    expect(getHistory).toHaveBeenCalledTimes(1);
  });
  it('clears old data on failure and shows unknown source explicitly', async () => {
    getHistory.mockResolvedValueOnce({ ...history, status: 'unknown', source: null, fetchedAt: null });
    render(<ResearchHistoryPanel asset="cn" language="zh" />);
    fireEvent.click(screen.getByRole('button', { name: '读取日线' }));
    expect(await screen.findByText('来源未知')).toBeInTheDocument();
    expect(screen.getByText('数据状态未验证')).toBeInTheDocument();
    getHistory.mockRejectedValueOnce(new Error('failed'));
    fireEvent.click(screen.getByRole('button', { name: '读取日线' }));
    expect(await screen.findByText('日线读取失败')).toBeInTheDocument();
    expect(screen.queryByTestId('history-chart')).not.toBeInTheDocument();
  });
  it('ignores a pending response after asset switch and does not auto-fetch the new asset', async () => {
    let resolve!: (value: ResearchHistory) => void;
    getHistory.mockReturnValueOnce(new Promise<ResearchHistory>((done) => { resolve = done; }));
    const { rerender } = render(<ResearchHistoryPanel asset="cn" language="zh" />);
    fireEvent.click(screen.getByRole('button', { name: '读取日线' }));
    const signal = getHistory.mock.calls[0][2] as AbortSignal;
    rerender(<ResearchHistoryPanel asset="us" language="zh" />);
    expect(signal.aborted).toBe(true);
    await act(async () => resolve(history));
    expect(screen.queryByTestId('history-chart')).not.toBeInTheDocument();
    expect(screen.getByLabelText('股票或 ETF 代码')).toHaveValue('AAPL');
    expect(getHistory).toHaveBeenCalledTimes(1);
  });
});
