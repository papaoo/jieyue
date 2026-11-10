export type ResearchHistoryBar = {
  date: string; open: number; high: number; low: number; close: number;
  volume: number | null; amount: number | null; changePercent: number | null;
};
export type ResearchHistory = {
  stockCode: string; stockName: string | null; period: 'daily';
  data: ResearchHistoryBar[]; source: string | null; fetchedAt: string | null;
  status: 'available' | 'partial' | 'empty' | 'unavailable' | 'unknown';
  warnings: string[]; adjustment: string | null;
};
