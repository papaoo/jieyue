import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ChartNoAxesCombined, Download, TriangleAlert } from 'lucide-react';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { stocksApi } from '../../api/stocks';
import type { ResearchHistory } from '../../types/researchHistory';
import type { ResearchAsset } from '../../utils/researchCoverage';
import './ResearchHistoryPanel.css';

const copy = {
  zh: {
    title: '按需读取日线', subtitle: '真实历史收盘价，独立于来源概览；只在你点击时读取。', ticker: '股票或 ETF 代码', days: '回溯天数', read: '读取日线', loading: '正在读取日线…',
    initial: '尚未请求行情', initialNote: '选择标的与区间，再主动读取。不会轮询或调用模型。', gold: '518880：黄金 ETF 代理，不是现货或期货金价。',
    crypto: 'BTC 尚未接入；禁止借用股票历史接口。', us: '美股请求依赖现有数据源；配置存在不保证标的可返回。',
    failure: '日线读取失败', failureNote: '未保留上一份图表。请检查网络、标的及来源后重试。', empty: '未返回可用日线', unavailable: '历史数据源暂不可用',
    source: '实际返回来源', unknownSource: '来源未知', bar: '最新返回 bar', completed: '请求完成时间（非行情时间）', unknownTime: '未返回完成时间',
    status: { available: '已返回历史数据', partial: '部分数据可用', empty: '空结果', unavailable: '来源不可用', unknown: '数据状态未验证' },
    adjustment: '复权口径未知', freshness: '新鲜度未验证 · 日线非实时', chartNote: '收盘价 · 纵轴为自动范围，不是收益率。横轴为返回交易日记录，非连续日历；币种与时区未由此接口返回。缺失记录未补值；无报价动画。',
    close: '收盘价', warnings: '数据边界与警告', validation: '请输入六位 A 股代码或有效美股股票代码。',
  },
  en: {
    title: 'Daily history on demand', subtitle: 'Actual historical closes, separate from the source overview. Read only on your click.', ticker: 'Stock or ETF ticker', days: 'Lookback days', read: 'Read daily history', loading: 'Reading daily history…',
    initial: 'No market data requested', initialNote: 'Choose an instrument and window, then request it. No polling or model call.', gold: '518880: a gold ETF proxy, not a spot or futures gold price.',
    crypto: 'BTC is not integrated; the equity history endpoint is not used.', us: 'US requests depend on existing providers. Configuration does not guarantee instrument availability.',
    failure: 'Daily history request failed', failureNote: 'The previous chart was removed. Check the network, instrument and source before retrying.', empty: 'No usable daily bars returned', unavailable: 'History provider unavailable',
    source: 'Actual returned source', unknownSource: 'Unknown source', bar: 'Latest returned bar', completed: 'Request completed (not market time)', unknownTime: 'No completion time returned',
    status: { available: 'Historical data returned', partial: 'Partial data', empty: 'Empty result', unavailable: 'Source unavailable', unknown: 'Unverified data status' },
    adjustment: 'Adjustment basis unknown', freshness: 'Freshness unverified · daily, not realtime', chartNote: 'Close price; auto-scaled axis, not returns. The x-axis shows returned trading-day records, not a continuous calendar. Currency and timezone are not returned. Missing records are not filled; no quote animation.',
    close: 'Close', warnings: 'Data boundaries & warnings', validation: 'Enter a six-digit A-share code or a valid US equity ticker.',
  },
};
type Props = { asset: ResearchAsset; language: 'zh' | 'en' };
const defaults: Record<ResearchAsset, string> = { cn: '510300', us: 'AAPL', gold: '518880', crypto: '' };

function HistoryReader({ asset, language }: Props) {
  const c = copy[language];
  const [ticker, setTicker] = useState(defaults[asset]);
  const [days, setDays] = useState(90);
  const [history, setHistory] = useState<ResearchHistory | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<'request' | 'validation' | null>(null);
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; controller.current?.abort(); }, []);

  const clear = () => { generation.current += 1; controller.current?.abort(); setHistory(null); setError(null); setLoading(false); };
  const read = async (event: FormEvent) => {
    event.preventDefault();
    if (asset === 'crypto') return;
    clear();
    const code = ticker.trim().toUpperCase();
    if ((asset === 'cn' && !/^\d{6}$/.test(code)) || (asset === 'us' && !/^[A-Z]{1,5}(?:\.[A-Z])?$/.test(code))) { setError('validation'); return; }
    const request = ++generation.current;
    const abort = new AbortController(); controller.current = abort; setLoading(true);
    try {
      const result = await stocksApi.getHistory(asset === 'gold' ? '518880' : code, days, abort.signal);
      if (request === generation.current && !abort.signal.aborted) setHistory(result);
    } catch { if (request === generation.current && !abort.signal.aborted) setError('request'); }
    finally { if (request === generation.current) setLoading(false); }
  };
  const canPlot = history && history.data.length > 0 && history.status !== 'unavailable' && history.status !== 'empty';

  return (
    <section className="research-history research-panel" aria-label={c.title}>
      <div className="research-history-heading"><div><span className="research-kicker">DAILY / MANUAL REQUEST</span><h2>{c.title}</h2><p>{c.subtitle}</p></div><ChartNoAxesCombined size={22} aria-hidden="true" /></div>
      <form className="research-history-controls" onSubmit={(event) => void read(event)}>
        <label htmlFor="research-history-ticker"><span>{c.ticker}</span><input id="research-history-ticker" value={ticker} placeholder={asset === 'crypto' ? '—' : defaults[asset]} disabled={loading || asset === 'crypto'} readOnly={asset === 'gold'} maxLength={10} autoComplete="off" onChange={(event) => { clear(); setTicker(event.target.value); }} /></label>
        <label htmlFor="research-history-days"><span>{c.days}</span><select id="research-history-days" value={days} disabled={loading || asset === 'crypto'} onChange={(event) => { clear(); setDays(Number(event.target.value)); }}>{[30, 90, 180, 365].map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        <button className="research-button" type="submit" disabled={loading || asset === 'crypto'}><Download size={15} aria-hidden="true" />{loading ? c.loading : c.read}</button>
      </form>
      {asset !== 'cn' ? <p className="research-history-boundary">{asset === 'gold' ? c.gold : asset === 'crypto' ? c.crypto : c.us}</p> : null}
      {loading ? <p role="status" className="research-history-idle">{c.loading}</p> : null}
      {error ? <div role="alert" className="research-warning"><strong>{error === 'validation' ? c.validation : c.failure}</strong>{error === 'request' ? <p>{c.failureNote}</p> : null}</div> : null}
      {!history && !loading && !error ? <div className="research-history-idle"><span>{c.initial}</span><p>{c.initialNote}</p></div> : null}
      {history ? <>
        <div className="research-history-result-title"><strong className="research-mono">{history.stockCode}{history.stockName ? ` / ${history.stockName}` : ''}</strong><span className="research-status" data-state={history.status === 'available' ? 'neutral' : 'attention'}>{c.status[history.status]}</span></div>
        {canPlot ? <div className="research-history-chart" role="img" aria-label={`${history.stockCode} ${c.close}: ${history.data[0].date} – ${history.data.at(-1)?.date}`}>
          <ResponsiveContainer width="100%" height={240}>
            <LineChart data={history.data} margin={{ top: 16, right: 12, left: 2, bottom: 4 }}>
              <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="3 6" vertical={false} />
              <XAxis dataKey="date" tick={{ fill: 'hsl(var(--muted-text))', fontSize: 10 }} minTickGap={45} tickLine={false} axisLine={false} />
              <YAxis domain={['auto', 'auto']} tick={{ fill: 'hsl(var(--muted-text))', fontSize: 10 }} width={56} tickLine={false} axisLine={false} />
              <Tooltip contentStyle={{ background: 'hsl(var(--card))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 11, color: 'hsl(var(--foreground))' }} isAnimationActive={false} />
              <Line type="linear" dataKey="close" name={c.close} stroke="hsl(var(--primary))" strokeWidth={1.6} dot={history.data.length === 1 ? { r: 3 } : false} activeDot={{ r: 3 }} isAnimationActive={false} connectNulls={false} />
            </LineChart>
          </ResponsiveContainer>
        </div> : <p className="research-history-idle">{history.status === 'unavailable' ? c.unavailable : c.empty}</p>}
        <p className="research-history-chart-note">{c.chartNote}</p>
        <dl className="research-history-provenance"><div><dt>{c.source}</dt><dd>{history.source || c.unknownSource}</dd></div><div><dt>{c.bar}</dt><dd>{history.data.at(-1)?.date || '—'}</dd></div><div><dt>{c.completed}</dt><dd>{history.fetchedAt || c.unknownTime}</dd></div></dl>
        <div className="research-history-limitations"><TriangleAlert size={13} aria-hidden="true" /><span>{history.adjustment || c.adjustment} · {c.freshness}</span></div>
        {history.warnings.length > 0 ? <details className="research-history-warnings"><summary>{c.warnings} ({history.warnings.length})</summary><ul>{history.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></details> : null}
      </> : null}
    </section>
  );
}

/** Remount per asset so old results and in-flight requests never cross research contexts. */
export default function ResearchHistoryPanel(props: Props) {
  return <HistoryReader key={props.asset} {...props} />;
}
