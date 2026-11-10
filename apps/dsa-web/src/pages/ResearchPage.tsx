import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowRight, Bitcoin, Database, Gem, Globe2, Landmark, RefreshCw, Save, ShieldCheck, TriangleAlert } from 'lucide-react';
import { motion } from 'motion/react';
import { Link, useBlocker } from 'react-router-dom';
import type { BlockerFunction } from 'react-router-dom';
import { dataCapabilityApi } from '../api/dataCapability';
import { ConfirmDialog } from '../components/common';
import ResearchHistoryPanel from '../components/research/ResearchHistoryPanel';
import { BRAND } from '../config/brand';
import { useMotionPreference } from '../contexts/MotionPreferenceContext';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { DataCapabilityOverview } from '../types/dataCapability';
import { deriveResearchCoverage, type ResearchAsset } from '../utils/researchCoverage';
import './ResearchPage.css';

const STORAGE_PREFIX = 'guanlan.research-notebook.v1';
const FIELD_LIMIT = 4000;
const assets: ResearchAsset[] = ['cn', 'us', 'gold', 'crypto'];
const assetIcons = { cn: Landmark, us: Globe2, gold: Gem, crypto: Bitcoin };
const copy = {
  zh: {
    title: '跨资产研究', intro: '先确认来源，再形成判断。把宏观驱动写成可验证的问题，而不是行情结论。',
    selector: '选择研究资产', labels: { cn: 'A股', us: '美股', gold: '黄金', crypto: 'BTC' },
    subtitles: { cn: '盈利 · 政策 · 流动性', us: '盈利 · 利率 · 估值', gold: '实际利率 · 美元', crypto: '流动性 · 市场结构' },
    lens: '研究视角', mechanism: '关系假设图', mechanismNote: '连接表示待检验的研究关系，不代表当前方向、强度或因果结论。',
    drivers: ['增长与盈利', '实际利率', '美元与流动性', '风险偏好'], questionLabel: '下一步要验证的问题',
    questions: {
      cn: ['盈利改善是否能由财报和公告验证，而不只是政策叙事？', '成交与市场宽度能否支持行业扩散，而不是少数标的上涨？', '外部利率与汇率变化，通过哪一条具体路径影响目标行业？'],
      us: ['估值变化来自盈利预期还是贴现率？两者的证据是否分开？', '财报发布日期与市场已知时点是否匹配研究区间？', '指数表现是否掩盖集中度和行业间的差异？'],
      gold: ['研究对象是现货、期货还是黄金 ETF？币种和交易时段是否一致？', '实际利率与美元的关系，在选定区间是否仍成立？', 'ETF 费用、跟踪误差和汇率是否解释了与金价的偏离？'],
      crypto: ['所用价格来自哪个交易所、现货还是永续合约？', '流动性变化是否与资金费率、杠杆和清算风险相互印证？', '24 小时市场如何与股票交易日对齐，避免提前使用信息？'],
    },
    sourceTitle: '来源证据', refresh: '刷新来源概览', refreshing: '正在读取概览', loading: '正在读取已接入的数据能力…',
    failed: '来源概览暂不可用', failedDetail: '本次读取失败，旧快照已撤下。未对行情或数据源执行新的探测。', retry: '重试',
    snapshot: '概览生成时间', configNotEvidence: '配置可用 ≠ 数据已成功观测；概览时间 ≠ 行情时间。',
    coverage: { candidate: '有配置候选', partial: '候选能力受限', unknown: '能力未验证', unconfigured: '尚未配置', unavailable: '候选暂不可用', 'not-integrated': '直接数据未接入' },
    goldBoundary: '黄金直接行情尚未接入', goldNote: '下方可按需读取黄金 ETF 代理，需按具体标的独立请求与核验；不能等同于现货或期货金价。',
    cryptoBoundary: 'BTC 行情适配器尚未接入', cryptoNote: '可记录研究假设；此页不提供 BTC 报价，也不会将股票数据源当作加密行情。',
    candidateCount: '配置候选', observedCount: '带成功时点的记录', noProviders: '未找到该资产的数据集级候选。可到数据中心查看真实配置边界。',
    providerConfigured: '已配置', providerUnconfigured: '未配置', enabled: '启用', disabled: '停用', cooldown: '冷却中',
    quality: '返回的数据质量', noEvidence: '暂无可归属到该市场的观测记录。', noSource: '来源未知', noTime: '未返回成功时间',
    statuses: { ok: '接口状态 OK', partial: '部分可用', degraded: '降级', unconfigured: '未配置', unavailable: '不可用', unknown: '未验证', stale: '已过期' },
    freshness: { stale: '已标记过期', 'not-stale': '接口标记未过期', unknown: '新鲜度未知' },
    fallback: '降级来源', knownObservation: '有历史成功时点', notObservation: '未证实成功观测',
    notebook: '研究假设簿', notebookNote: '仅保存在当前浏览器，不同步服务器。共享设备上的本地存储并不加密；重要内容请导出备份。',
    thesis: '研究假设', evidence: '可观测证据', invalidation: '失效条件', date: '记录日期',
    placeholders: ['一个具体、可被证伪的判断', '来源链接、数据口径、发布日期与仍缺失的证据', '什么观察会让你修改或放弃这个判断'],
    save: '保存到本机', export: '导出 JSON', dirty: '有未保存修改', draft: '本地草稿', saved: '已保存到当前浏览器',
    leaveTitle: '有未保存的研究草稿', leaveMessage: '离开此页面会丢弃所有资产未保存的修改。你可以取消后逐一保存或导出；确认离开不会自动保存。',
    leaveConfirm: '放弃草稿并离开', leaveCancel: '留在研究页',
    readError: '本地存储无法读取，保存已暂停。草稿仍可编辑与导出。',
    invalidError: '本地记录格式异常，保存已暂停以避免覆盖原记录。可导出草稿，并在恢复原记录后重新读取。',
    writeError: '保存失败：浏览器阻止了写入或存储空间不足。草稿尚未保存，可导出备份。',
    retryStorage: '重新读取存储', exportError: '导出失败。请检查浏览器下载权限后重试。', invalidFields: '请填写有效的记录日期，并将每个文本字段控制在 4000 字以内。',
    existingTools: '继续使用已接入能力', tools: ['股票筛选', '数据中心', '持仓管理', '策略回测'],
    toolsNote: '打开工作区不会自动调用模型或启动分析。股票工具目前并不意味着全资产支持。',
    macro: '宏观证据边界', macroNote: '本页尚未接入宏观事件 API。上方仅为研究框架，不包含当前宏观数据或投资建议。',
  },
  en: {
    title: 'Cross-asset research', intro: 'Verify the source before forming a view. Turn macro drivers into testable questions, not market conclusions.',
    selector: 'Choose a research asset', labels: { cn: 'A-shares', us: 'US equities', gold: 'Gold', crypto: 'BTC' },
    subtitles: { cn: 'Earnings · Policy · Liquidity', us: 'Earnings · Rates · Valuation', gold: 'Real yields · USD', crypto: 'Liquidity · Market structure' },
    lens: 'Research lens', mechanism: 'Relationship hypotheses', mechanismNote: 'Connections are research hypotheses, not current direction, strength or established causation.',
    drivers: ['Growth & earnings', 'Real yields', 'USD & liquidity', 'Risk appetite'], questionLabel: 'Questions to verify next',
    questions: {
      cn: ['Do filings and announcements verify improving earnings, beyond the policy narrative?', 'Do turnover and market breadth support sector diffusion beyond a few leaders?', 'Through which specific channel do external rates and FX affect the target industry?'],
      us: ['Does the valuation change come from earnings expectations or discount rates? Separate the evidence.', 'Are filing publication dates and information-known times aligned with the study window?', 'Do index returns hide concentration and differences between sectors?'],
      gold: ['Is the instrument spot gold, futures or a gold ETF? Align currency and trading hours.', 'Does the real-yield and USD relationship hold in the chosen window?', 'Can ETF fees, tracking error and FX explain divergence from the gold price?'],
      crypto: ['Which exchange supplies the price, and is it spot or a perpetual contract?', 'Do funding rates, leverage and liquidation risk corroborate liquidity changes?', 'How will a 24/7 market align with equity sessions without using future information?'],
    },
    sourceTitle: 'Source evidence', refresh: 'Refresh source overview', refreshing: 'Reading overview', loading: 'Reading integrated data capabilities…',
    failed: 'Source overview unavailable', failedDetail: 'This request failed and the previous snapshot was removed. No new quote or provider probe was performed.', retry: 'Retry',
    snapshot: 'Overview generated', configNotEvidence: 'Configured ≠ successfully observed. Overview time ≠ market observation time.',
    coverage: { candidate: 'Configured candidates', partial: 'Limited candidates', unknown: 'Unverified capability', unconfigured: 'Not configured', unavailable: 'Candidates unavailable', 'not-integrated': 'Direct data not integrated' },
    goldBoundary: 'Direct gold data is not integrated', goldNote: 'The panel below can request a gold ETF proxy; verify each instrument independently. ETF data is not spot or futures gold.',
    cryptoBoundary: 'The BTC data adapter is not integrated', cryptoNote: 'You can record a thesis. This page supplies no BTC quote and does not relabel equity providers as crypto sources.',
    candidateCount: 'Configured candidates', observedCount: 'Records with a success time', noProviders: 'No dataset-level candidate was found for this asset. Check the actual configuration in Data Center.',
    providerConfigured: 'Configured', providerUnconfigured: 'Unconfigured', enabled: 'Enabled', disabled: 'Disabled', cooldown: 'Cooling down',
    quality: 'Returned data quality', noEvidence: 'No observation record can be attributed to this market.', noSource: 'Unknown source', noTime: 'No success time returned',
    statuses: { ok: 'API status OK', partial: 'Partial', degraded: 'Degraded', unconfigured: 'Unconfigured', unavailable: 'Unavailable', unknown: 'Unverified', stale: 'Stale' },
    freshness: { stale: 'Marked stale', 'not-stale': 'API reports not stale', unknown: 'Unknown freshness' },
    fallback: 'Fallback sources', knownObservation: 'Historical success time available', notObservation: 'Successful observation unverified',
    notebook: 'Hypothesis notebook', notebookNote: 'Stored only in this browser, not synced to the server. Local storage is not encrypted on shared devices; export important notes.',
    thesis: 'Research thesis', evidence: 'Observable evidence', invalidation: 'Invalidation condition', date: 'Record date',
    placeholders: ['A specific, falsifiable view', 'Source links, definitions, publication dates and missing evidence', 'What observation would make you revise or abandon this view?'],
    save: 'Save locally', export: 'Export JSON', dirty: 'Unsaved changes', draft: 'Local draft', saved: 'Saved in this browser',
    leaveTitle: 'Unsaved research drafts', leaveMessage: 'Leaving discards unsaved changes for all assets. Stay to save or export each draft. Leaving will not save automatically.',
    leaveConfirm: 'Discard drafts and leave', leaveCancel: 'Stay in research',
    readError: 'Local storage could not be read. Saving is paused; editing and exporting remain available.',
    invalidError: 'The stored record is invalid. Saving is paused to prevent overwriting it. Export your draft and retry after restoring the record.',
    writeError: 'Save failed: browser storage is blocked or full. The draft is not saved; export a backup.',
    retryStorage: 'Retry storage read', exportError: 'Export failed. Check browser download permissions and try again.', invalidFields: 'Use a valid record date and keep each text field within 4,000 characters.',
    existingTools: 'Continue with integrated tools', tools: ['Stock screener', 'Data Center', 'Portfolio', 'Backtest'],
    toolsNote: 'Opening a workspace does not invoke a model or start an analysis. Equity tools do not imply full multi-asset support.',
    macro: 'Macro evidence boundary', macroNote: 'No macro event API is connected to this page yet. The framework above contains no current macro data or investment advice.',
  },
};

type NotebookFields = { thesis: string; evidence: string; invalidation: string; date: string };
type StorageError = 'read' | 'invalid' | 'write' | null;
type Notebook = { fields: NotebookFields; dirty: boolean; savedAt: string | null; storageError: StorageError; notice: 'saved' | 'export-error' | 'invalid-fields' | null };

function localDate(): string {
  const date = new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}
function validDate(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}
function validFields(value: unknown): value is NotebookFields {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const fields = value as Record<string, unknown>;
  return Object.keys(fields).length === 4 && validDate(fields.date)
    && ['thesis', 'evidence', 'invalidation'].every((key) => typeof fields[key] === 'string' && (fields[key] as string).length <= FIELD_LIMIT);
}
function emptyNotebook(): Notebook {
  return { fields: { thesis: '', evidence: '', invalidation: '', date: localDate() }, dirty: false, savedAt: null, storageError: null, notice: null };
}
function readNotebook(asset: ResearchAsset): Notebook {
  const empty = emptyNotebook();
  let raw: string | null;
  try { raw = localStorage.getItem(`${STORAGE_PREFIX}:${asset}`); }
  catch { return { ...empty, storageError: 'read' }; }
  if (raw === null) return empty;
  try {
    if (raw.length > 30000) throw new Error('Oversized record');
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid record');
    const stored = value as Record<string, unknown>;
    if (stored.version !== 1 || stored.asset !== asset || !validFields(stored.fields)
      || typeof stored.savedAt !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(stored.savedAt) || !Number.isFinite(Date.parse(stored.savedAt))) throw new Error('Invalid fields');
    return { ...empty, fields: stored.fields, savedAt: stored.savedAt };
  } catch { return { ...empty, storageError: 'invalid' }; }
}

function DriverMap({ assetLabel, drivers, title, note }: { assetLabel: string; drivers: string[]; title: string; note: string }) {
  return (
    <figure className="research-driver-map">
      <figcaption><span>{title}</span><span className="research-mono">HYPOTHESES / NOT SIGNALS</span></figcaption>
      <svg viewBox="0 0 660 212" role="img" aria-label={`${title}: ${drivers.join(', ')} → ${assetLabel}`}>
        <path className="research-map-orbit" d="M 162 55 C 280 55 250 106 330 106 S 380 55 498 55 M 162 157 C 280 157 250 106 330 106 S 380 157 498 157" />
        <ellipse className="research-map-ring" cx="330" cy="106" rx="76" ry="72" />
        <circle className="research-map-center" cx="330" cy="106" r="43" />
        <text className="research-map-asset" x="330" y="111" textAnchor="middle">{assetLabel}</text>
        {drivers.map((driver, index) => {
          const x = index % 2 === 0 ? 72 : 408;
          const y = index < 2 ? 35 : 137;
          return <g key={driver}><rect className="research-map-node" x={x} y={y} width="180" height="40" rx="7" /><circle className="research-map-dot" cx={x + 17} cy={y + 20} r="3" /><text x={x + 32} y={y + 25}>{driver}</text></g>;
        })}
      </svg>
      <p>{note}</p>
    </figure>
  );
}

export default function ResearchPage() {
  const { language } = useUiLanguage();
  const c = copy[language];
  const { motionEnabled } = useMotionPreference();
  const [asset, setAsset] = useState<ResearchAsset>('cn');
  const [overview, setOverview] = useState<DataCapabilityOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [notebooks, setNotebooks] = useState<Partial<Record<ResearchAsset, Notebook>>>(() => ({ cn: readNotebook('cn') }));
  const generation = useRef(0);
  const notebook = notebooks[asset]!;
  const hasUnsavedDrafts = Object.values(notebooks).some((item) => item?.dirty);
  const coverage = useMemo(() => deriveResearchCoverage(overview, asset), [overview, asset]);
  const departureBlocker = useBlocker(useCallback<BlockerFunction>(
    ({ currentLocation, nextLocation }) => hasUnsavedDrafts && nextLocation.pathname !== currentLocation.pathname,
    [hasUnsavedDrafts],
  ));

  useEffect(() => {
    if (!hasUnsavedDrafts) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = c.leaveMessage;
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [hasUnsavedDrafts, c.leaveMessage]);

  useEffect(() => { document.title = `${c.title} - ${language === 'zh' ? BRAND.name : BRAND.englishName}`; }, [c.title, language]);
  const load = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true); setFailed(false); setOverview(null);
    try {
      const next = await dataCapabilityApi.getOverview();
      if (request === generation.current) setOverview(next);
    } catch { if (request === generation.current) setFailed(true); }
    finally { if (request === generation.current) setLoading(false); }
  }, []);
  useEffect(() => {
    void load();
    return () => { generation.current += 1; };
  }, [load]);

  const patchNotebook = (patch: Partial<Notebook>) => setNotebooks((current) => ({ ...current, [asset]: { ...current[asset]!, ...patch } }));
  const selectAsset = (next: ResearchAsset) => {
    if (!notebooks[next]) {
      const loaded = readNotebook(next);
      setNotebooks((current) => ({ ...current, [next]: current[next] ?? loaded }));
    }
    setAsset(next);
  };
  const editField = (field: keyof NotebookFields, value: string) => patchNotebook({ fields: { ...notebook.fields, [field]: value }, dirty: true, notice: null });
  const save = () => {
    if (notebook.storageError === 'read' || notebook.storageError === 'invalid') return;
    if (!validFields(notebook.fields)) { patchNotebook({ notice: 'invalid-fields' }); return; }
    const savedAt = new Date().toISOString();
    try {
      localStorage.setItem(`${STORAGE_PREFIX}:${asset}`, JSON.stringify({ version: 1, asset, savedAt, fields: notebook.fields }));
      patchNotebook({ savedAt, dirty: false, storageError: null, notice: 'saved' });
    } catch { patchNotebook({ storageError: 'write', notice: null }); }
  };
  const retryStorage = () => {
    const loaded = readNotebook(asset);
    patchNotebook(notebook.dirty ? { storageError: loaded.storageError, notice: null } : loaded);
  };
  const exportNotebook = () => {
    let url: string | null = null;
    let anchor: HTMLAnchorElement | null = null;
    try {
      const payload = { version: 1, asset, exportedAt: new Date().toISOString(), savedAt: notebook.savedAt, fields: notebook.fields };
      url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' }));
      anchor = document.createElement('a'); anchor.href = url; anchor.download = `guanlan-research-${asset}-${localDate()}.json`;
      document.body.appendChild(anchor); anchor.click();
    } catch { patchNotebook({ notice: 'export-error' }); }
    finally { anchor?.remove(); if (url) URL.revokeObjectURL(url); }
  };
  const storageMessage = notebook.storageError === 'read' ? c.readError : notebook.storageError === 'invalid' ? c.invalidError : notebook.storageError === 'write' ? c.writeError : null;

  return (
    <div className="research-page" data-motion={motionEnabled ? 'on' : 'off'}>
      <header className="research-header">
        <div><div className="research-kicker"><span className="research-kicker-line" />{BRAND.englishName} / RESEARCH DESK</div><h1>{c.title}</h1><p>{c.intro}</p></div>
        <div className="research-header-seal" aria-hidden="true"><span>{BRAND.shortName}</span><small>EVIDENCE<br />BEFORE CONVICTION</small></div>
      </header>

      <div className="research-asset-rail" role="group" aria-label={c.selector}>
        {assets.map((item, index) => {
          const Icon = assetIcons[item];
          return <button key={item} type="button" aria-pressed={asset === item} data-selected={asset === item} onClick={() => selectAsset(item)}>
            <span className="research-asset-number">0{index + 1}</span><Icon size={19} aria-hidden="true" /><span className="research-asset-label"><strong>{c.labels[item]}</strong><small>{c.subtitles[item]}</small></span>
            {asset === item ? <motion.span className="research-asset-indicator" layoutId="research-asset-indicator" transition={{ duration: motionEnabled ? 0.18 : 0 }} /> : null}
          </button>;
        })}
      </div>

      <div className="research-workspace">
        <motion.section key={asset} className="research-lens research-panel" initial={motionEnabled ? { opacity: 0, y: 5 } : false} animate={{ opacity: 1, y: 0 }} transition={{ duration: motionEnabled ? 0.18 : 0 }}>
          <div className="research-section-heading"><span className="research-kicker">01 / {c.lens}</span><span className="research-status" data-state="neutral">{c.coverage[coverage.state]}</span></div>
          <div className="research-lens-title"><h2>{c.labels[asset]}</h2><p>{c.subtitles[asset]}</p></div>
          <DriverMap assetLabel={c.labels[asset]} drivers={c.drivers} title={c.mechanism} note={c.mechanismNote} />
          <h3 className="research-subheading">{c.questionLabel}</h3>
          <ol className="research-questions">{c.questions[asset].map((question, index) => <li key={question}><span className="research-mono">0{index + 1}</span><p>{question}</p></li>)}</ol>
          <div className="research-boundary"><ShieldCheck size={16} aria-hidden="true" /><div><strong>{c.macro}</strong><p>{c.macroNote}</p></div></div>
        </motion.section>

        <aside className="research-evidence research-panel" aria-label={c.sourceTitle}>
          <div className="research-section-heading"><h2>{c.sourceTitle}</h2><button className="research-icon-button" type="button" disabled={loading} aria-label={loading ? c.refreshing : c.refresh} onClick={() => void load()}><RefreshCw size={16} aria-hidden="true" /></button></div>
          <p className="research-evidence-rule">{c.configNotEvidence}</p>
          {loading ? <div role="status" className="research-empty"><Database size={22} aria-hidden="true" /><p>{c.loading}</p></div> : null}
          {failed ? <div role="alert" className="research-warning"><strong>{c.failed}</strong><p>{c.failedDetail}</p><button type="button" className="research-button" onClick={() => void load()}>{c.retry}</button></div> : null}
          {overview ? <>
            <div className="research-snapshot"><span>{c.snapshot}</span><time dateTime={overview.asOf}>{overview.asOf || '—'}</time></div>
            {overview.warnings.length > 0 ? <div className="research-warning"><TriangleAlert size={15} aria-hidden="true" /><p>{overview.warnings.join(' · ')}</p></div> : null}
            {asset === 'gold' || asset === 'crypto' ? <div className="research-empty"><strong>{asset === 'gold' ? c.goldBoundary : c.cryptoBoundary}</strong><p>{asset === 'gold' ? c.goldNote : c.cryptoNote}</p></div> : <>
              <div className="research-coverage-counts"><div><span className="research-mono">{coverage.providers.filter((item) => item.usable).length}</span><small>{c.candidateCount}</small></div><div><span className="research-mono">{coverage.observedCount}</span><small>{c.observedCount}</small></div></div>
              {coverage.providers.length === 0 ? <p className="research-empty">{c.noProviders}</p> : null}
              <div className="research-providers">{coverage.providers.map(({ provider, datasets }) => <div className="research-provider" key={provider.name}>
                <div className="research-provider-title"><strong>{provider.label || provider.name}</strong><span className="research-status" data-state={provider.status === 'unavailable' ? 'error' : provider.status === 'ok' ? 'neutral' : 'attention'}>{c.statuses[provider.status]}</span></div>
                <p className="research-provider-config">{provider.enabled ? c.enabled : c.disabled} · {provider.configured ? c.providerConfigured : c.providerUnconfigured}{provider.cooldown ? ` · ${c.cooldown}` : ''}</p>
                <p className="research-mono research-provider-datasets">{datasets.join(' / ')}</p>
                {provider.warnings.map((warning) => <p className="research-warning-text" key={warning}>{warning}</p>)}
                {provider.lastError ? <p className="research-warning-text">{provider.lastError}</p> : null}
              </div>)}</div>
              <h3 className="research-subheading">{c.quality}</h3>
              {coverage.evidence.length === 0 ? <p className="research-empty">{c.noEvidence}</p> : <div className="research-quality">{coverage.evidence.map((item) => <div key={item.dataset}>
                <div className="research-provider-title"><code>{item.dataset}</code><span className="research-status" data-state={item.status === 'unavailable' ? 'error' : item.status === 'ok' && item.observed && item.freshness === 'not-stale' ? 'neutral' : 'attention'}>{c.statuses[item.status]}</span></div>
                <p>{item.source || c.noSource} · {item.observed ? c.knownObservation : c.notObservation}</p>
                <time dateTime={item.lastSuccess || undefined}>{item.lastSuccess || c.noTime}</time>
                <p className={item.freshness !== 'not-stale' ? 'research-warning-text' : ''}>{c.freshness[item.freshness]}</p>
                {item.fallbackFrom.length ? <p>{c.fallback}: {item.fallbackFrom.join(' → ')}</p> : null}
                {item.warnings.map((warning) => <p className="research-warning-text" key={warning}>{warning}</p>)}
                {item.lastError ? <p className="research-warning-text">{item.lastError}</p> : null}
              </div>)}</div>}
            </>}
          </> : null}
        </aside>

        <ResearchHistoryPanel asset={asset} language={language} />

        <section className="research-notebook research-panel" aria-label={c.notebook}>
          <div className="research-notebook-heading"><div><span className="research-kicker">02 / HYPOTHESIS NOTEBOOK</span><h2>{c.notebook} <span>{c.labels[asset]}</span></h2></div><span className="research-status" data-state="neutral">{notebook.dirty ? c.dirty : c.draft}</span></div>
          <p className="research-notebook-privacy">{c.notebookNote}</p>
          <div className="research-notebook-fields">{(['thesis', 'evidence', 'invalidation'] as const).map((field, index) => <label key={field} htmlFor={`research-${field}`}><span id={`research-${field}-label`}>{c[field]}</span><textarea id={`research-${field}`} aria-labelledby={`research-${field}-label`} rows={4} maxLength={FIELD_LIMIT} placeholder={c.placeholders[index]} value={notebook.fields[field]} onChange={(event) => editField(field, event.target.value)} /></label>)}</div>
          <div className="research-notebook-footer"><label htmlFor="research-date"><span>{c.date}</span><input id="research-date" type="date" value={notebook.fields.date} onChange={(event) => editField('date', event.target.value)} /></label><div className="research-notebook-actions"><button type="button" className="research-button" onClick={exportNotebook}><ArrowDownToLine size={15} aria-hidden="true" />{c.export}</button><button type="button" className="research-button research-button-primary" disabled={notebook.storageError === 'read' || notebook.storageError === 'invalid'} onClick={save}><Save size={15} aria-hidden="true" />{c.save}</button></div></div>
          {storageMessage ? <div className="research-warning" role="alert"><p>{storageMessage}</p><button type="button" className="research-text-button" onClick={retryStorage}>{c.retryStorage}</button></div> : null}
          {notebook.notice ? <p role={notebook.notice === 'saved' ? 'status' : 'alert'} className={notebook.notice === 'saved' ? 'research-save-note' : 'research-warning-text'}>{notebook.notice === 'saved' ? c.saved : notebook.notice === 'export-error' ? c.exportError : c.invalidFields}</p> : null}
        </section>
      </div>

      <footer className="research-tool-dock"><div><span className="research-kicker">03 / CONNECTED WORKSPACES</span><h2>{c.existingTools}</h2><p>{c.toolsNote}</p></div><nav aria-label={c.existingTools}>{['/screening', '/data', '/portfolio', '/backtest'].map((path, index) => <Link to={path} key={path}>{c.tools[index]}<ArrowRight size={14} aria-hidden="true" /></Link>)}</nav></footer>
      <ConfirmDialog
        isOpen={departureBlocker.state === 'blocked'}
        title={c.leaveTitle}
        message={c.leaveMessage}
        confirmText={c.leaveConfirm}
        cancelText={c.leaveCancel}
        isDanger
        onConfirm={() => departureBlocker.proceed?.()}
        onCancel={() => departureBlocker.reset?.()}
      />
    </div>
  );
}
