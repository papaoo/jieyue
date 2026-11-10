import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AnalysisReport, MarketReviewPayload } from '../../../types/analysis';
import { MarketReviewReportView } from '../MarketReviewReportView';

vi.mock('../../../api/history', () => ({
  historyApi: {
    getMarkdown: vi.fn(),
  },
}));

const englishMarketReviewReport: AnalysisReport = {
  meta: {
    queryId: 'market-review-q-1',
    stockCode: 'MARKET',
    stockName: 'Market Review',
    reportType: 'market_review',
    reportLanguage: 'en',
    createdAt: '2026-03-18T08:00:00Z',
  },
  summary: {
    analysisSummary: '',
    operationAdvice: '',
    trendPrediction: '',
    sentimentScore: undefined as unknown as number,
  },
};

const combinedMarketReviewPayload: MarketReviewPayload = {
  version: 1,
  kind: 'market_review',
  region: 'cn,hk',
  language: 'zh',
  rootTitle: '大盘复盘',
  markets: {
    cn: {
      title: 'A股市场',
      breadth: {
        upCount: 3120,
        downCount: 1420,
        limitUpCount: 72,
        limitDownCount: 4,
        totalAmount: 9600,
        turnoverUnit: '亿元',
      },
      indices: [{
        code: '000300',
        name: '沪深300',
        current: 3920.2,
        changePct: 1.2,
        high: 3940.5,
        low: 3860.1,
      }],
      sectors: {
        top: [{ name: '半导体', changePct: 2.35 }],
        bottom: [{ name: '煤炭', changePct: -1.1 }],
      },
      concepts: {
        top: [{ name: '机器人概念', changePct: 4.2 }],
        bottom: [{ name: '转基因', changePct: -2.05 }],
      },
    },
    hk: {
      title: '港股市场',
      breadth: {
        upCount: 680,
        downCount: 410,
        limitUpCount: 0,
        limitDownCount: 0,
        totalAmount: 1180,
        turnoverUnit: '亿港元',
      },
      indices: [{
        code: 'HSI',
        name: '恒生指数',
        current: 18920.4,
        changePct: -0.5,
        high: 19050.2,
        low: 18780.3,
      }],
    },
  },
};

const noBreadthMarketReviewPayload: MarketReviewPayload = {
  version: 1,
  kind: 'market_review',
  region: 'us',
  language: 'en',
  title: 'Market Review',
  rootTitle: 'Market Review',
  indices: [{
    code: 'SPX',
    name: 'S&P 500',
    current: 5200,
    changePct: 0.68,
    high: 5235.2,
    low: 5170.4,
  }],
  sectors: {
    top: [{ name: 'Technology', changePct: 1.9 }],
    bottom: [{ name: 'Energy', changePct: -0.8 }],
  },
  news: [],
  sections: [],
};

describe('MarketReviewReportView', () => {
  it.each(['2026-10-12Tnot-a-time', '2026-02-30', '2026-10-12T25:00:00Z'])('rejects the entire malformed industry date %s', (asOf) => {
    const payload = { ...noBreadthMarketReviewPayload, sectors: { top: [{ name: '银行', asOf }] } };
    render(<MarketReviewReportView payload={payload} content="# 大盘复盘" reportLanguage="zh" />);
    expect(screen.getByText(/数据日期未知/)).toBeInTheDocument();
    expect(screen.queryByText(/数据日期：/)).not.toBeInTheDocument();
  });
  it.each(['2026-10-12', '2026-10-12T09:30:00+08:00', '2026-10-12T01:30:00Z'])('accepts a full supported industry date %s', (asOf) => {
    const payload = { ...noBreadthMarketReviewPayload, sectors: { top: [{ name: '银行', asOf }] } };
    render(<MarketReviewReportView payload={payload} content="# 大盘复盘" reportLanguage="zh" />);
    expect(screen.getByText(/数据日期：2026-10-12/)).toBeInTheDocument();
  });
  it('uses historical row-level date/source/definition when rankings have no container metadata', () => {
    const row = { name: '银行', changePct: 1.2, asOf: '2026-10-12', source: 'computed_from_tushare',
      definition: 'equal_weight_return_of_observed_traded_constituents' };
    const payload = { ...noBreadthMarketReviewPayload, sectors: { top: [row], bottom: [{ ...row, name: '煤炭' }] } };
    render(<MarketReviewReportView payload={payload} content="# 大盘复盘" reportLanguage="zh" />);
    expect(screen.getAllByText(/数据日期：2026-10-12/)).toHaveLength(2);
    expect(screen.getAllByText(/computed_from_tushare/)).toHaveLength(2);
    expect(screen.getAllByText(/已观测成分等权均值/)).toHaveLength(2);
    expect(screen.queryByText(/数据日期未知/)).not.toBeInTheDocument();
  });
  it('honestly summarizes mixed and missing row dates without using report creation time', () => {
    const payload = { ...noBreadthMarketReviewPayload, sectors: { top: [
      { name: '银行', asOf: '2026-10-11', source: 'one' },
      { name: '煤炭', asOf: '2026-10-12', source: 'two' }, { name: '软件' },
    ] } };
    render(<MarketReviewReportView report={englishMarketReviewReport} payload={payload} content="# 大盘复盘" reportLanguage="zh" />);
    expect(screen.getByText(/2026-10-11 至 2026-10-12.*日期混合.*部分日期未知/)).toBeInTheDocument();
    expect(screen.queryByText(/数据日期：2026-03-18/)).not.toBeInTheDocument();
  });
  it('discloses the historical sector observation date and equal-weight scope', () => {
    const payload = { ...noBreadthMarketReviewPayload, sectors: {
      ...noBreadthMarketReviewPayload.sectors, asOf: '2026-10-12', source: 'computed_from_tushare',
      definition: 'equal_weight_return_of_observed_traded_constituents', coverage: { observedCount: 100 },
    } };
    render(<MarketReviewReportView payload={payload} content="# Market Review" reportLanguage="zh" />);
    expect(screen.getAllByText(/2026-10-12/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/历史数据/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/等权/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/历史数据.*未知/)).not.toBeInTheDocument();
  });
  it('uses localized summary card labels and fallbacks for English reports', () => {
    render(
      <MarketReviewReportView
        report={englishMarketReviewReport}
        content="# Market Review"
        reportLanguage="en"
      />,
    );

    expect(screen.getByText('Review Summary')).toBeInTheDocument();
    expect(screen.getByText('No review summary yet')).toBeInTheDocument();
    expect(screen.getByText('Market Sentiment')).toBeInTheDocument();
    expect(screen.getByText('No score yet')).toBeInTheDocument();
    expect(screen.getByText('Rotation & Funds')).toBeInTheDocument();
    expect(screen.getByText('No rotation view yet')).toBeInTheDocument();
    expect(screen.getByText('Risks & Watchlist')).toBeInTheDocument();
    expect(screen.getByText('No key observations yet')).toBeInTheDocument();
    expect(screen.queryByText('复盘摘要')).not.toBeInTheDocument();
    expect(screen.queryByText('暂无摘要')).not.toBeInTheDocument();
  });

  it('renders structured data for every market in a combined market review payload', () => {
    render(
      <MarketReviewReportView
        payload={combinedMarketReviewPayload}
        content="# 大盘复盘"
        reportLanguage="zh"
      />,
    );

    expect(screen.getByText('A股市场')).toBeInTheDocument();
    expect(screen.getByText('港股市场')).toBeInTheDocument();
    expect(screen.getByText('沪深300')).toBeInTheDocument();
    expect(screen.getByText('恒生指数')).toBeInTheDocument();
    expect(screen.getByText('3120')).toBeInTheDocument();
    expect(screen.getByText('680')).toBeInTheDocument();
  });

  it('renders industry and concept rankings from structured market review payloads', () => {
    render(
      <MarketReviewReportView
        payload={combinedMarketReviewPayload}
        content="# 大盘复盘"
        reportLanguage="zh"
      />,
    );

    expect(screen.getAllByText('行业板块')).toHaveLength(2);
    expect(screen.getAllByText('概念板块')).toHaveLength(2);
    expect(screen.getByText('半导体')).toBeInTheDocument();
    expect(screen.getByText('机器人概念')).toBeInTheDocument();
    expect(screen.getByText('+4.20%')).toBeInTheDocument();
    expect(screen.getByText('-2.05%')).toBeInTheDocument();
  });

  it('localizes structured market data labels for Chinese reports', () => {
    render(
      <MarketReviewReportView
        payload={combinedMarketReviewPayload}
        content="# 大盘复盘"
        reportLanguage="zh"
      />,
    );

    expect(screen.getByText('结构化大盘数据')).toBeInTheDocument();
    expect(screen.getAllByText('上涨家数')).toHaveLength(2);
    expect(screen.getAllByText('下跌家数')).toHaveLength(2);
    expect(screen.getAllByText('涨停/跌停')).toHaveLength(2);
    expect(screen.getAllByText('成交额')).toHaveLength(2);
    expect(screen.getAllByText('指数')).toHaveLength(2);
    expect(screen.getAllByText('最新')).toHaveLength(2);
    expect(screen.getAllByText('涨跌幅')).toHaveLength(2);
    expect(screen.getAllByText('高/低')).toHaveLength(2);
    expect(screen.queryByText('Structured Market Data')).not.toBeInTheDocument();
    expect(screen.queryByText('Advancers')).not.toBeInTheDocument();
    expect(screen.queryByText('Index')).not.toBeInTheDocument();
  });

  it('shows "No data" when breadth is not available for a market review payload', () => {
    render(
      <MarketReviewReportView
        payload={noBreadthMarketReviewPayload}
        content="# Market Review"
        reportLanguage="en"
      />,
    );

    expect(screen.getByText('Structured Market Data')).toBeInTheDocument();
    expect(screen.getByText('No data')).toBeInTheDocument();
    expect(screen.getByText('S&P 500')).toBeInTheDocument();
    expect(screen.getAllByText('Industry Sectors').length).toBeGreaterThan(0);
    expect(screen.getByText('Technology')).toBeInTheDocument();
    expect(screen.getByText('Energy')).toBeInTheDocument();
    expect(screen.queryByText('Advancers')).not.toBeInTheDocument();
    expect(screen.queryByText('Decliners')).not.toBeInTheDocument();
  });

  it('formats structured market numbers to two decimal places', () => {
    const payload: MarketReviewPayload = {
      version: 1,
      kind: 'market_review',
      region: 'cn',
      language: 'en',
      title: 'Market Review',
      rootTitle: 'Market Review',
      breadth: {
        upCount: 4327,
        downCount: 1145,
        limitUpCount: 222,
        limitDownCount: 12,
        totalAmount: 36822.49698199988,
        turnoverUnit: 'bn',
      },
      indices: [{
        code: '000001',
        name: 'Shanghai Composite',
        current: 4112.446,
        changePct: 0.44079750937683315,
        high: 4143.314,
        low: 4087.536,
      }],
    };

    render(
      <MarketReviewReportView
        payload={payload}
        content="# Market Review"
        reportLanguage="en"
      />,
    );

    expect(screen.getByText('36822.50 bn')).toBeInTheDocument();
    expect(screen.getByText('4112.45')).toBeInTheDocument();
    expect(screen.getByText('0.44%')).toBeInTheDocument();
    expect(screen.getByText('4143.31 / 4087.54')).toBeInTheDocument();
    expect(screen.queryByText(/36822\.496/)).not.toBeInTheDocument();
    expect(screen.queryByText(/0\.440797/)).not.toBeInTheDocument();
  });

  it('formats string-backed market numbers and hides missing high/low zeros', () => {
    const payload = {
      version: 1,
      kind: 'market_review',
      region: 'cn',
      language: 'en',
      title: 'Market Review',
      rootTitle: 'Market Review',
      breadth: {
        upCount: '4,327',
        downCount: '1,145',
        limitUpCount: '0',
        limitDownCount: '12',
        totalAmount: '36,822.49698199988',
        turnoverUnit: 'bn',
      },
      indices: [{
        code: '000001',
        name: 'Shanghai Composite',
        current: '4,112.446',
        changePct: '0.44079750937683315%',
        high: 0,
        low: '0',
      }],
    } as unknown as MarketReviewPayload;

    render(
      <MarketReviewReportView
        payload={payload}
        content="# Market Review"
        reportLanguage="en"
      />,
    );

    expect(screen.getByText('4327')).toBeInTheDocument();
    expect(screen.getByText('36822.50 bn')).toBeInTheDocument();
    expect(screen.getByText('4112.45')).toBeInTheDocument();
    expect(screen.getByText('0.44%')).toBeInTheDocument();
    expect(screen.queryByText('0.00 / 0.00')).not.toBeInTheDocument();
    expect(screen.queryByText(/0\.440797/)).not.toBeInTheDocument();
  });

  it('opens run flow for historical market review records', () => {
    const onOpenRunFlow = vi.fn();

    render(
      <MarketReviewReportView
        payload={combinedMarketReviewPayload}
        content="# 大盘复盘"
        recordId={7}
        reportLanguage="zh"
        onOpenRunFlow={onOpenRunFlow}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '查看历史记录 7 运行流' }));

    expect(onOpenRunFlow).toHaveBeenCalledWith(7);
  });
});
