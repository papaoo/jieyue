import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react';
import { Badge, Button } from '../../common';
import { researchDeliveryApi as api } from '../../../api/researchDelivery';
import type { AutomationJob, AutomationStatus, DeliveryCategory, DeliveryDestination, DeliverySubscription } from '../../../types/researchDelivery';
import { automationLabel, displayTime, safeDeliveryError } from './shared';

interface Props {
  groups: DeliveryDestination[]; categories: DeliveryCategory[]; subscriptions: DeliverySubscription[];
  disabled: boolean; visible: boolean; mutationLock: MutableRefObject<boolean>;
  onSavingChange: (busy: boolean) => void; onStatusChange: (status: AutomationStatus | null) => void;
  onControlChanged: () => Promise<void>;
}
const businessDay = () => new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' });
// Retain the day's identity even if Settings unmounts this pane; never persist credentials or payloads.
let sessionRunRequest: { day: string; id: string } | null = null;
const statuses: Record<string, string> = { pending: '等待处理', collecting: '采集中', retry_wait: '等待数据重试', ready: '数据就绪', preparing: '生成报告中', prepared: '报告已准备', sending: '发送中', sent: '飞书已接收', blocked: '当天数据未通过', skipped: '已跳过', rejected: '飞书明确拒绝', delivery_unknown: '送达未知', suppressed: '已停止' };
const stages: Record<string, string> = { queued: '已排队', collecting: '采集', collection: '数据验收', collection_pending: '分批采集待续', collected: '采集完成', preparing: '报告准备', prepared: '报告已准备', delivery: '投递', guard: '安全校验', authorization: '授权检查', deadline: '截止时窗' };
const issues: Record<string, string> = {
  collection_pending: '当前批次已保存，等待继续采集，不占用源错误重试次数',
  checkpoint_invalid: '采集断点证据校验未通过', listing_calendar_invalid: '上市交易日历证据无效',
  market_response_coverage_below_95pct: '行情覆盖率不足 95%', non_trading_day: '非交易日，不发布', category_unsupported: '此类别尚未接入',
  outside_publication_window: '不在当日发布窗口', authorization_changed: '授权已变化，旧任务已停止', configuration_changed: '配置已变化，请核对',
  source_timeout: '数据源超时', source_rate_limited: '数据源限流', source_request_failed: '数据源请求失败',
  source_permission_denied: '数据权限不足', source_quota_exceeded: '数据配额不足', source_unconfigured: '数据源未配置',
  calendar_unverified: '交易日历未核实', calendar_invalid: '交易日历无效', data_not_ready: '数据尚未就绪',
  abandoned_sending: '发送回执未确认，禁止自动重发', llm_daily_limit: '模型当日预算已用尽', llm_unavailable: '模型不可用',
  prompt_fallback: '模型解释已回退原始事实', destination_disabled: '目标群已暂停', subscription_disabled: '订阅已暂停',
  tushare_credentials_missing: 'Tushare 凭据未配置', collector_outside_collection_window: '不在当日采集窗口', calendar_evidence_invalid: '交易日历证据无效',
  etf_asset_data_unavailable: 'ETF 资产数据不完整', etf_history_date_invalid: 'ETF 历史日期未通过', etf_warmup_incomplete: 'ETF 技术预热不足',
  radar_current_verified_facts_unavailable: '雷达缺少当日已核实事实', announcement_sources_unavailable: '公告来源暂不可用',
  payload_too_large: '报告超出内部 16 KiB 消息预算', batch_expired: '数据批次已过期', coverage_invalid: '数据覆盖校验未通过', evidence_invalid: '来源证据未通过',
};
function issueText(code: string | null): string {
  if (!code) return '无';
  return code.split(';').map(c => `${issues[c] ?? '未通过安全或数据校验'}（${c}）`).join('；');
}
function coverageText(job: AutomationJob, category: string): string {
  const c = job.coverage, values: string[] = [];
  const n = (key: string) => typeof c[key] === 'number' ? c[key] as number : undefined;
  if (category === 'announcement_risk') {
    if (n('scanned_count') !== undefined) values.push(`公告范围已扫描 ${n('scanned_count')}，未扫描 ${n('unscanned_count') ?? '未知'}`);
    if (n('verified_count') !== undefined) values.push(`已核实 ${n('verified_count')}`);
  } else if (category === 'etf_rotation') {
    if (n('valid_asset_count') !== undefined || n('asset_count') !== undefined) values.push(`有效资产 ${n('valid_asset_count') ?? '未知'}/${n('asset_count') ?? '未知'}`);
    else values.push('有效 / 总资产数暂未提供');
    if (n('history_years') !== undefined) values.push(`历史 ${n('history_years')} 年，仅参考信号`);
  } else {
    if (n('daily_response_coverage') !== undefined) values.push(`行情覆盖率 ${(n('daily_response_coverage')! * 100).toFixed(1)}%`);
    if (n('eligible_count') !== undefined) values.push(`有效样本 ${n('eligible_count')} 只`);
    if (n('independent_universe_count') !== undefined) values.push(`独立股票池 ${n('independent_universe_count')} 只`);
  }
  if (n('required_count') !== undefined) values.push(`必需字段单元 ${n('valid_count') ?? '未知'}/${n('required_count')}`);
  if (c.scope) values.push(c.scope === 'restricted_complete_core_metric_eligible_universe' ? '完整指标受限样本，非全市场排名' : c.scope === 'verified_theme_subset' ? '已核实主题子集' : c.scope === 'bounded_public_announcement_metadata' ? '有限范围公告元数据' : '观察快照，非全市场');
  if (n('technical_limit') !== undefined) values.push(`${c.contract_version === 2 ? '每批采集预算' : '技术计算上限'} ${n('technical_limit')} 只`);
  if (c.contract_version === 2) values.push(`全范围评估 ${n('evaluated_count') ?? 0}/${n('evaluation_scope_count') ?? '未知'}，待采 ${n('pending_count') ?? 0}，失败 ${n('failed_count') ?? 0}`);
  if (n('technical_not_evaluated_count') !== undefined) values.push(`未评估 ${n('technical_not_evaluated_count')} 只`);
  return values.join('；') || '暂无可发布批次覆盖信息';
}

export function AutomationPanel({ groups, categories, subscriptions, disabled, visible, mutationLock, onSavingChange, onStatusChange, onControlChanged }: Props) {
  const [status, setStatus] = useState<AutomationStatus | null>(null);
  // This initial selection is deliberately not reset when a new group is saved.
  const [selected, setSelected] = useState(() => subscriptions.filter(s => groups.some(g => g.id === s.destination_id && g.credential_configured) && categories.some(c => c.category_id === s.strategy_id && c.producer_status === 'live_on_demand')).map(s => s.id));
  const [reading, setReading] = useState(false), [busy, setBusy] = useState(false);
  const [readError, setReadError] = useState(''), [actionError, setActionError] = useState(''), [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false), [submittedDay, setSubmittedDay] = useState<string | null>(null);
  const inFlight = useRef(false), mounted = useRef(true);
  const currentStatus = useRef(status); currentStatus.current = status;
  const readEpoch = useRef(0), previousSubscriptions = useRef(subscriptions);
  const statusCallback = useRef(onStatusChange); statusCallback.current = onStatusChange;
  const load = useCallback(async (explicit = false) => {
    if (inFlight.current) return;
    const epoch = readEpoch.current;
    inFlight.current = true; if (mounted.current) setReading(true);
    try {
      const next = await api.automation();
      if (mounted.current && epoch === readEpoch.current) {
        setStatus(next); setReadError(''); statusCallback.current(next);
        if (explicit) { setConflict(false); setActionError(previous => previous.startsWith('自动化授权版本') ? '' : previous); }
      }
    } catch (e) {
      if (mounted.current && epoch === readEpoch.current) { setReadError(`自动化状态读取失败：${safeDeliveryError(e)}`); statusCallback.current(null); }
    } finally { inFlight.current = false; if (mounted.current) setReading(false); }
  }, []);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (previousSubscriptions.current !== subscriptions) { previousSubscriptions.current = subscriptions; if (visible) void load(); }
  }, [subscriptions, visible, load]);
  useEffect(() => {
    if (!visible) return;
    void load();
    const refresh = () => { if (document.visibilityState !== 'hidden') void load(); };
    document.addEventListener('visibilitychange', refresh);
    const timer = window.setInterval(() => { if (document.visibilityState !== 'hidden' && currentStatus.current?.enabled) void load(); }, 5000);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [visible, load]);
  const act = async (action: () => Promise<void>, versioned = false) => {
    if (disabled || mutationLock.current) return;
    mutationLock.current = true; readEpoch.current++; setBusy(true); onSavingChange(true); setActionError(''); setNotice('');
    try { await action(); }
    catch (e) {
      if (versioned && (e as { response?: { status?: number } })?.response?.status === 409) { setConflict(true); setActionError('自动化授权版本已变化。请先“刷新运行状态”核对，不会自动覆盖或重试。'); }
      else setActionError(safeDeliveryError(e));
    } finally { mutationLock.current = false; if (mounted.current) { setBusy(false); onSavingChange(false); } }
  };
  const control = (enabled: boolean) => void act(async () => {
    if (!status) return;
    const next = await api.updateAutomation({ version: status.version, enabled, subscription_ids: enabled ? selected : [] });
    setStatus(next); onStatusChange(next);
    setNotice(enabled ? '已启用所选群，其他群状态不变；按各群计划和数据门禁执行。' : '已暂停全部自动推送；已提交的在途发送无法撤回。');
    await onControlChanged();
  }, true);
  const run = () => void act(async () => {
    const day = businessDay();
    if (sessionRunRequest?.day !== day) sessionRunRequest = { day, id: crypto.randomUUID() };
    const requestId = sessionRunRequest.id; setSubmittedDay(day);
    try {
      const receipt = await api.runAutomation({ request_id: requestId });
      setNotice(`今天的运行已受理（${receipt.business_date}），不代表已发送。请求编号：${receipt.request_id}`);
    } catch (e) {
      const httpStatus = (e as { response?: { status?: number } })?.response?.status;
      if (httpStatus && httpStatus < 500) { setSubmittedDay(null); throw e; }
      setActionError(`运行请求结果未知，可能已排队。只刷新运行状态并核对各群，不重复提交。请求编号：${requestId}`);
    }
    await load();
  });
  const locked = disabled || busy || !status || !!readError || conflict;
  return <section className="rd-editor rd-automation" aria-label="自动推送控制" aria-busy={busy}>
    <div className="rd-heading"><h3>自动推送总开关</h3><Badge variant={status?.enabled && status.worker_alive && !readError ? 'success' : 'warning'}>{readError ? '自动化状态未确认' : automationLabel(status)}</Badge></div>
    <p className="rd-muted">启用和运行今天会执行真实采集、模型解释（按订阅设置）与飞书发送，不是连接测试。仅处理当天，不补发历史。localhost 本机服务必须保持运行。</p>
    <p className="rd-muted">启用后的计划运行：北京时间 {status?.collection_time ?? '15:30'} 开始跑策略并整合数据，随后按固定顺序一个一个做研判并推送、完成即发（不按固定时段等待）；某群数据或模型未就绪会顺延且不阻塞后续，{status?.cutoff_time ?? '22:30'} 截止。</p>
    <p className="rd-muted">运行今天：立即排队当前已启用订阅，不受已保存计划时间限制；仍仅限北京时间 {status?.collection_time ?? '15:30'}—{status?.cutoff_time ?? '22:30'}，并校验交易日、数据、版本和凭据。受理不代表送达，同日去重仍生效。</p>
    <p className="rd-muted">工作进程：{readError || !status ? '未确认' : status.worker_alive ? '在线' : '离线'} · 最近心跳（北京时间）：{displayTime(status?.heartbeat_at ?? undefined)}</p>
    {readError ? <p role="alert" className="rd-error">{readError}</p> : null}
    {actionError ? <p role="alert" className="rd-error">{actionError}</p> : null}
    {notice ? <p role="status" className="rd-success">{notice}</p> : null}
    {status?.last_error ? <p className="rd-error">最近运行问题：{issueText(status.last_error)}</p> : null}
    <fieldset disabled={disabled || busy} className="rd-form"><legend className="rd-muted">启用所选群，其他群状态不变；取消勾选不会暂停已启用的群。单群暂停请使用下方“暂停该订阅”。新保存的群不会自动入选。</legend>
      <div className="rd-activation-list">{subscriptions.map(s => {
        const group = groups.find(g => g.id === s.destination_id), category = categories.find(c => c.category_id === s.strategy_id);
        const eligible = !!group?.credential_configured && category?.producer_status === 'live_on_demand';
        return <label key={s.id} className="rd-check"><input type="checkbox" aria-label={`启用选择 ${s.name} → ${group?.alias ?? s.destination_id}`} disabled={!eligible} checked={selected.includes(s.id)} onChange={e => setSelected(ids => e.target.checked ? [...ids, s.id] : ids.filter(id => id !== s.id))} /><span>{s.name} → {group?.alias ?? s.destination_id} · {category?.code ?? s.strategy_id} · 北京时间 {s.planned_time ?? '—'}{!eligible ? ' · 未接入或缺群连接' : !group?.enabled ? ' · 目标暂停，启用订阅后仍不投递' : ''}</span></label>;
      })}</div>
      {!subscriptions.length ? <p className="rd-muted">尚无订阅，请先独立保存群订阅，再明确选择启用。</p> : null}
      <div className="rd-inline"><Button variant="settings-primary" disabled={locked || !selected.length} onClick={() => control(true)}>启用所选群</Button><Button variant="settings-secondary" disabled={locked || !status?.enabled} onClick={() => control(false)}>暂停全部自动推送</Button><Button variant="settings-secondary" disabled={locked || !status?.enabled || !status.worker_alive || submittedDay === businessDay()} onClick={run}>运行今天</Button></div>
    </fieldset>
    <div className="rd-inline"><Button variant="ghost" disabled={reading || busy} onClick={() => void load(true)}>刷新运行状态</Button><span className="rd-muted">只读刷新，不覆盖未保存表单；启用且页面可见时每 5 秒更新。</span></div>
    <div className="rd-table-scroll" role="region" aria-label="分群自动推送计划与状态" tabIndex={0}><table className="rd-jobs"><thead><tr><th>类别 / 群</th><th>计划时间 / 订阅</th><th>业务日 / 阶段 / 状态</th><th>数据覆盖 / 最近问题</th><th>平台回执</th></tr></thead><tbody>{status?.subscriptions.map(s => {
      const category = categories.find(c => c.category_id === s.category_id), group = groups.find(g => g.id === s.destination_id), job = s.job;
      return <tr key={s.subscription_id}><td>{category?.code} {category?.display_name ?? s.category_id}<br /><strong>{group?.alias ?? s.destination_id}</strong><br />{s.name}</td><td>北京时间 {s.planned_time}<br />{s.enabled ? '订阅启用' : '订阅暂停'}{group && !group.enabled ? ' · 目标暂停' : ''}</td><td>{job ? <>{job.business_date}<br />{stages[job.stage] ?? '处理中'} · {statuses[job.status] ?? '待核对'}<br />尝试 {job.attempts} 次<br />更新 {displayTime(job.updated_at)}</> : '当天尚无任务'}</td><td>{job ? <>{coverageText(job, s.category_id)}<br />最近问题：{issueText(job.error_code)}</> : category?.producer_status === 'not_connected' ? '内容尚未接入' : '等待授权、计划或当天数据'}</td><td>{job?.status === 'sent' ? '飞书已接收（不等于已读）' : job?.status === 'delivery_unknown' ? '送达未知，不自动重发' : job?.status === 'rejected' ? '飞书明确拒绝' : '尚无接收确认'}</td></tr>;
    })}</tbody></table></div>
  </section>;
}
