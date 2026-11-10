import { useEffect, useState } from 'react';
import { researchDeliveryApi as api } from '../../../api/researchDelivery';
import { Button, Select, Badge } from '../../common';
import type { DeliveryCapabilities, DeliveryCategory, DeliveryDestination, DeliveryRecord, DeliverySource, DeliverySubscription, ResearchPublication } from '../../../types/researchDelivery';
import { displayTime, reportPresentation, safeDeliveryError } from './shared';

interface Props {
  subscriptions: DeliverySubscription[]; groups: DeliveryDestination[]; categories: DeliveryCategory[];
  capabilities: DeliveryCapabilities; publications: ResearchPublication[]; records: DeliveryRecord[]; disabled: boolean;
  onPreview: (subscriptionId: string, runId: string, useLlm: boolean) => Promise<ResearchPublication | null>;
}
export function ResearchPreview({ subscriptions, groups, categories, capabilities, publications, records, disabled, onPreview }: Props) {
  const [subId, setSubId] = useState('');
  const [sourceId, setSourceId] = useState('');
  const [sources, setSources] = useState<DeliverySource[]>([]);
  const [loadedStrategy, setLoadedStrategy] = useState('');
  const [error, setError] = useState('');
  const [useLlm, setUseLlm] = useState(false);
  const [selectedReport, setSelectedReport] = useState<ResearchPublication | null>(null);
  const subscription = subscriptions.find(s => s.id === subId);
  const category = categories.find(c => c.category_id === subscription?.strategy_id);
  const strategyId = category?.preview_available ? category.category_id : '';
  useEffect(() => {
    if (!strategyId) return;
    let cancelled = false;
    api.sources(strategyId).then(items => {
      if (!cancelled) { setSources(items); setLoadedStrategy(strategyId); }
    }).catch(e => { if (!cancelled) { setError(safeDeliveryError(e)); setLoadedStrategy(strategyId); } });
    return () => { cancelled = true; };
  }, [strategyId, subId]);
  const canModel = capabilities.llm_available && subscription?.llm_mode === 'explain_only';
  const report = selectedReport;
  const presentation = report ? reportPresentation(report, records) : null;
  const dataDate = report?.brief_input?.business_date || report?.envelope?.business_date;
  const historicalData = dataDate && dataDate < new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' });
  const observations = [...new Set(report?.detail?.sources?.map(s => s.fetched_at).filter((s): s is string => Boolean(s)) ?? [])];
  const datedObservations = observations.filter(value => Number.isFinite(Date.parse(value))).sort((a, b) => Date.parse(a) - Date.parse(b));
  const observationRange = datedObservations.length ? `${displayTime(datedObservations[0])}${datedObservations.length > 1 ? ` 至 ${displayTime(datedObservations[datedObservations.length - 1])}` : ''}` : '时间未能解析';
  return <div className="rd-form">
    <div className="rd-notice"><strong>只读取已有运行，不重新选股</strong><p>研究草稿保留原始排序、日期缺口和风险。只有勾选 AI 解释时才可能调用现有模型；不发送消息。</p></div>
    <div className="rd-form-pair">
      <Select label="选择订阅" disabled={disabled} value={subId} options={subscriptions.map(s => ({ value: s.id, label: s.name }))} onChange={value => {
        setSubId(value); setSourceId(''); setSources([]); setLoadedStrategy(''); setError(''); setUseLlm(false); setSelectedReport(null);
      }} />
      <Select label="已保存的策略运行" disabled={disabled || !strategyId || loadedStrategy !== strategyId} value={sourceId} options={sources.map(s => ({ value: s.source_run_id, label: `${displayTime(s.created_at)} · ${s.candidate_count} 候选 · 数据日${s.business_date ?? '未知'}` }))} onChange={value => { setSourceId(value); setSelectedReport(null); }} />
    </div>
    {error ? <p role="alert" className="rd-error">{error}</p> : null}
    {category && !category.preview_available ? <p className="rd-muted">此分类没有历史运行预览；{category.producer_status === 'live_on_demand' ? '当日数据生产与发布由上方自动推送控制，仍须通过门禁。' : '当日内容生产尚未接入。'}</p> : null}
    {strategyId && loadedStrategy === strategyId && !sources.length && !error ? <p className="rd-muted">这个策略还没有已保存的运行。请先到选股页面运行并保存结果，再回到此处。</p> : null}
    <label className="rd-check"><input type="checkbox" checked={useLlm} disabled={disabled || !canModel} onChange={e => setUseLlm(e.target.checked)} /><span>本次使用 AI 解释（可能计费，每日最多 {capabilities.llm_daily_attempt_limit} 次；不改排名）{!canModel ? ' · 当前订阅关闭或模型路由不受支持' : ''}</span></label>
    <div><Button variant="settings-primary" disabled={disabled || !sourceId || !subscription || loadedStrategy !== strategyId} onClick={async () => {
      if (!subscription) return;
      const result = await onPreview(subscription.id, sourceId, useLlm && canModel);
      if (result) setSelectedReport(result);
    }}>生成研究草稿</Button></div>
    {report && presentation ? <article className="rd-report" aria-label="冻结研究报告">
      <div className="rd-inline"><Badge>{presentation.label}</Badge><Badge variant={presentation.accepted ? 'success' : presentation.kind === 'unknown' ? 'default' : 'warning'}>{presentation.deliveryLabel}</Badge><Badge>AI：{report.llm.status === 'succeeded' ? '解释已生成' : report.llm.status === 'off' || report.llm.status === 'not_requested' ? '未调用' : '已降级'}</Badge></div>
      <p className="rd-muted">目标：{groups.find(g => g.id === report.destination_id)?.alias ?? '历史目标'} · 原始运行 {report.source_run_id}</p>
      {report.title ? <h3>{report.title}</h3> : null}
      <p className="rd-muted">数据日期：{dataDate || '未知（旧版记录未提供）'}{historicalData ? ' · 历史数据，非当日行情' : ''} · 报告生成：{displayTime(report.created_at)}</p>
      {observations.length ? <p className="rd-muted">观测时间（北京时间）：{observationRange} · {observations.length} 个时间点{datedObservations.length < observations.length ? '（部分时间未知）' : ''}；逐条来源见核验信息。</p> : null}
      <pre className={`rd-report-content${report.format_version === 'research-brief-v1' ? ' rd-brief-content' : ''}`}>{report.rendered_content}</pre>
      {report.envelope?.evidence.length ? <details><summary>打开原始证据链接</summary><ul className="rd-list">{report.envelope.evidence.map(e => <li key={e.id} className="py-2">
        {e.url && /^https?:\/\//.test(e.url) ? <a className="underline underline-offset-4" href={e.url} target="_blank" rel="noopener noreferrer">{e.title || e.id}</a> : <span>{e.title || e.id} · 无可用公开链接</span>}
        <p className="rd-muted">原始发布时间：{e.published_at ?? '未知'} · 主体匹配：{e.issuer_status === 'exact_stored_symbol' ? '存储代码精确匹配（非独立核实）' : '未核实'}</p>
      </li>)}</ul></details> : null}
      <details><summary>核验信息与用量</summary><p className="rd-muted">记录的阻断原因：{report.blockers.join('、') || '无记录（不代表已发送）'}。AI 状态：{report.llm.status}；{report.llm.reason ?? '无额外原因'}。</p><pre className="rd-report-content">{JSON.stringify({ usage: report.llm.usage ?? {}, audit: report.detail?.audit ?? {}, observations: report.detail?.sources ?? [] }, null, 2)}</pre><p className="rd-muted">未知费用不等于免费。内容哈希：{report.content_hash}</p></details>
      {presentation.kind === 'historical' ? <Button variant="settings-secondary" disabled>历史草稿不可正式发布</Button>
        : <p className="rd-muted">{presentation.kind === 'formal' || presentation.kind === 'revision' ? '仅展示冻结报告与关联投递记录；本页不重新发送。' : '报告身份或投递关联未确认，不能据此批准或发送。'}</p>}
    </article> : null}
    <div className="rd-separator"><h3>已保存的研究报告与草稿</h3>{!publications.length ? <p className="rd-muted">还没有保存记录。可选订阅和已有运行生成历史预览；正式报告由自动推送流程生成。</p> : <ul className="rd-list">{publications.map(p => {
      const state = reportPresentation(p, records);
      return <li key={p.id}><button type="button" disabled={disabled} className="rd-list-button" onClick={() => setSelectedReport(p)}><span>{subscriptions.find(s => s.id === p.subscription_id)?.name ?? '历史订阅'}</span><span className="rd-muted">{displayTime(p.created_at)} · {state.label} · {state.deliveryLabel}</span></button></li>;
    })}</ul>}</div>
  </div>;
}
