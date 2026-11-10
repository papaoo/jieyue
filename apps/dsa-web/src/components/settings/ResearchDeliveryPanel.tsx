import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Layers3, Plus, RefreshCw } from 'lucide-react';
import { researchDeliveryApi as api } from '../../api/researchDelivery';
import type { AutomationStatus, DeliveryCapabilities, DeliveryCategory, DeliveryDestination, DeliveryRecord, DeliverySubscription, ResearchPublication } from '../../types/researchDelivery';
import { Badge, Button } from '../common';
import { SettingsSectionCard } from './SettingsSectionCard';
import { GroupEditor } from './research-delivery/GroupEditor';
import { SetupEditor } from './research-delivery/SetupEditor';
import { SubscriptionEditor } from './research-delivery/SubscriptionEditor';
import { ResearchPreview } from './research-delivery/ResearchPreview';
import { automationLabel, groupKinds, deliveryLabels, displayTime, safeDeliveryError } from './research-delivery/shared';
import { DeliveryNotice } from './research-delivery/DeliveryNotice';
import { AutomationPanel } from './research-delivery/AutomationPanel';
import './research-delivery/research-delivery.css';

interface Props { disabled?: boolean; visible?: boolean; onDirtyChange?: (dirty: boolean) => void; onSavingChange?: (busy: boolean) => void; draftResetToken?: number }
type View = 'subscriptions' | 'groups' | 'preview' | 'deliveries';
const views: { id: View; label: string }[] = [{ id: 'subscriptions', label: '分类订阅' }, { id: 'groups', label: '群连接' }, { id: 'preview', label: '研究预览' }, { id: 'deliveries', label: '投递记录' }];
const replace = <T extends { id: string }>(rows: T[], next: T) => rows.some(r => r.id === next.id) ? rows.map(r => r.id === next.id ? next : r) : [...rows, next];

export function ResearchDeliveryPanel({ draftResetToken = 0, ...props }: Props) {
  // A page-level discard changes this key; secrets never survive the explicit reset.
  return <ResearchDeliveryWorkspace key={draftResetToken} {...props} />;
}
function ResearchDeliveryWorkspace({ disabled = false, visible = true, onDirtyChange, onSavingChange }: Props) {
  const [caps, setCaps] = useState<DeliveryCapabilities | null>(null);
  const [categories, setCategories] = useState<DeliveryCategory[]>([]);
  const [groups, setGroups] = useState<DeliveryDestination[]>([]);
  const [subscriptions, setSubscriptions] = useState<DeliverySubscription[]>([]);
  const [publications, setPublications] = useState<ResearchPublication[]>([]);
  const [records, setRecords] = useState<DeliveryRecord[]>([]);
  const [view, setView] = useState<View>('subscriptions');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [automationBusy, setAutomationBusy] = useState(false);
  const [automationStatus, setAutomationStatus] = useState<AutomationStatus | null>(null);
  const lock = useRef(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [groupDirty, setGroupDirty] = useState(false);
  const [subDirty, setSubDirty] = useState(false);
  const [groupEditor, setGroupEditor] = useState<{ target?: DeliveryDestination } | null>(null);
  const [categoryId, setCategoryId] = useState('');
  const [subId, setSubId] = useState('');
  const [setupCategory, setSetupCategory] = useState<string | null>(null);
  const [test, setTest] = useState<{ target: DeliveryDestination; requestId: string; result?: DeliveryRecord; uncertain?: boolean } | null>(null);
  const confirmationRef = useRef<HTMLDivElement>(null);
  const navigationRef = useRef<HTMLElement>(null);
  const testTrigger = useRef<HTMLButtonElement | null>(null);
  const [uncertainTests, setUncertainTests] = useState<Record<string, { requestId: string; version: number }>>({});
  const dirty = groupDirty || subDirty;
  const blocked = disabled || busy || automationBusy || loading;
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onSavingChange?.(busy || automationBusy); }, [busy, automationBusy, onSavingChange]);
  useEffect(() => () => { onDirtyChange?.(false); onSavingChange?.(false); }, [onDirtyChange, onSavingChange]);
  useEffect(() => {
    if (test) confirmationRef.current?.focus();
    else if (testTrigger.current) {
      if (testTrigger.current.disabled) navigationRef.current?.querySelector<HTMLButtonElement>('[aria-current]')?.focus();
      else testTrigger.current.focus();
    }
  }, [test]);

  const read = useCallback(async () => {
    const [capabilities, topics, destinations, subs, pubs, deliveries] = await Promise.all([
      api.capabilities(), api.categories(), api.destinations(), api.subscriptions(), api.publications(), api.deliveries(),
    ]);
    setCaps(capabilities); setCategories(topics); setGroups(destinations); setSubscriptions(subs); setPublications(pubs); setRecords(deliveries);
  }, []);
  useEffect(() => {
    let active = true;
    void read().catch(e => { if (active) setError(safeDeliveryError(e)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [read]);
  const perform = async (action: () => Promise<void>, success = '') => {
    if (lock.current || disabled) return false;
    lock.current = true; setBusy(true); setError(''); setNotice('');
    try { await action(); setNotice(success); return true; }
    catch (e) { setError(safeDeliveryError(e)); return false; }
    finally { lock.current = false; setBusy(false); }
  };
  const updateGroup = (g: DeliveryDestination) => { setGroups(rows => replace(rows, g)); if (groupEditor?.target?.id === g.id) setGroupEditor({ target: g }); };
  const chooseCategory = (category: DeliveryCategory, subscriptionId?: string) => {
    if (dirty) { setError('请先保存或取消当前编辑，再切换分类。'); return; }
    const existing = subscriptionId ?? subscriptions.find(s => s.strategy_id === category.category_id)?.id;
    setCategoryId(category.category_id); setSubId(existing ?? ''); setSetupCategory(existing ? null : category.category_id); setError('');
  };
  const category = categories.find(c => c.category_id === categoryId);
  const subscription = subscriptions.find(s => s.id === subId);
  const configuredCount = categories.filter(c => subscriptions.some(s => s.strategy_id === c.category_id)).length;
  const closeTest = () => { setTest(null); };

  return <SettingsSectionCard className="research-delivery" title="多群研究推送" description="选内容、连接群、保存订阅。每类一个群，一次完成。" actions={<Button variant="settings-secondary" disabled={blocked || dirty || !!test} onClick={() => void perform(async () => { setGroupEditor(null); setSetupCategory(null); setCategoryId(''); await read(); }, '已从服务器重新读取保存记录')}><RefreshCw size={15} />重新读取</Button>}>
    <div className="rd-status-rail"><span><Layers3 size={16} /><strong>{configuredCount}/{categories.length || '—'}</strong> 分类已绑定</span><span>{groups.length} 个独立群</span><span>保存不发送</span><Badge variant={automationStatus?.enabled && automationStatus.worker_alive ? 'success' : 'warning'}>{automationLabel(automationStatus)}</Badge></div>
    <p className="rd-muted">使用表单内的“保存群订阅”一次保存连接和内容绑定；旧版通知在另一个入口，不会覆盖这里。保存成功不代表自动推送已运行。</p>
    {error ? <div role="alert" className="rd-error">{error}</div> : null}
    {notice ? <p role="status" className="rd-success">{notice}</p> : null}
    {loading ? <div className="rd-skeleton" aria-label="正在读取多群配置"><span /><span /><span /></div> : caps ? <>
      <AutomationPanel groups={groups} categories={categories} subscriptions={subscriptions} visible={visible} disabled={disabled || busy || dirty || !!test} mutationLock={lock} onSavingChange={setAutomationBusy} onStatusChange={setAutomationStatus} onControlChanged={async () => { setSubscriptions(await api.subscriptions()); }} />
      <div className="rd-heading"><nav ref={navigationRef} aria-label="多群推送工作区" className="rd-tabs">{views.map(v => <button type="button" key={v.id} aria-current={view === v.id ? 'page' : undefined} disabled={blocked || dirty || !!test} onClick={() => { setView(v.id); setError(''); }}>{v.label}</button>)}</nav><Button variant="settings-primary" disabled={blocked || dirty || !!test} onClick={() => { setView('subscriptions'); setCategoryId(''); setSubId(''); setSetupCategory(''); setGroupEditor(null); setNotice(''); }}><Plus size={16} />新增群订阅</Button></div>
      {view === 'subscriptions' ? <div className="rd-workspace">
        <div><div className="rd-heading"><h3>一类一群</h3><span className="rd-muted">点击分类配置</span></div><ul className="rd-category-list">{categories.map(c => {
          const bindings = subscriptions.filter(s => s.strategy_id === c.category_id);
          return <li key={c.category_id}><button type="button" className={`rd-category ${categoryId === c.category_id ? 'is-selected' : ''}`} disabled={blocked} onClick={() => chooseCategory(c)} aria-label={`${c.code} ${c.display_name}`}>
            <span className="rd-code">{c.code}</span><span className="min-w-0"><strong>{c.display_name}</strong><small>{bindings.length ? bindings.map(s => groups.find(g => g.id === s.destination_id)?.alias ?? '目标不存在').join('、') : '未配置 · 点击一次完成连接与订阅'}</small>{bindings.length ? <small>内容已绑定 · 保存于 {displayTime(bindings[0].updated_at)} · {c.producer_status === 'live_on_demand' ? bindings.every(s => !s.enabled) ? '订阅暂停' : automationLabel(automationStatus) : c.producer_status === 'not_connected' ? '内容尚未接入' : '仅历史草稿'}</small> : null}</span><ArrowUpRight size={15} /></button>
            {bindings.length > 1 ? <div className="rd-inline">{bindings.map(s => <button className="rd-text-button" type="button" key={s.id} disabled={blocked || dirty} onClick={() => chooseCategory(c, s.id)}>编辑 {s.name}</button>)}</div> : null}
          </li>;
        })}</ul></div>
        {setupCategory !== null ? <SetupEditor key={setupCategory} initialCategoryId={setupCategory} categories={categories} groups={groups} llmAvailable={caps.llm_available} disabled={disabled || loading || automationBusy} onDirtyChange={setSubDirty} onSavingChange={setBusy}
          onClose={() => { setSetupCategory(null); setSubDirty(false); setCategoryId(''); }}
          onSaved={saved => { setGroups(rows => replace(rows, saved.destination)); setSubscriptions(rows => replace(rows, saved.subscription)); setSetupCategory(null); setSubDirty(false); setCategoryId(saved.subscription.strategy_id); setSubId(saved.subscription.id); setNotice(`已保存：${saved.subscription.name} → ${saved.destination.alias}。连接与内容绑定均已保存；新订阅暂停，需另行选择启用。`); }} />
          : category ? <SubscriptionEditor key={`${category.category_id}:${subscription?.id ?? 'new'}:${subscription?.version ?? 0}`} category={category} subscription={subscription} groups={groups} llmAvailable={caps.llm_available} disabled={blocked} onDirtyChange={setSubDirty}
          onCancel={() => { setCategoryId(''); setSubId(''); setSubDirty(false); }}
          onSave={async body => { await perform(async () => { const saved = subscription ? await api.updateSubscription(subscription.id, { ...body, version: subscription.version }) : await api.createSubscription(body); setSubscriptions(rows => replace(rows, saved)); setSubId(saved.id); }, '该类订阅已保存；保存不直接发送，后续受当前总开关与计划约束'); }}
          onToggle={async () => { if (subscription) await perform(async () => { const saved = await api.updateSubscription(subscription.id, { version: subscription.version, enabled: !subscription.enabled }); setSubscriptions(rows => replace(rows, saved)); }, `订阅${subscription.enabled ? '已暂停' : '已启用'}；${automationLabel(automationStatus)}。启用且工作进程在线时将按计划执行。`); }} />
          : <aside className="rd-empty"><Layers3 size={28} /><h3>从一个群订阅开始</h3><ol className="rd-steps"><li>在飞书建群，添加自定义机器人。</li><li>点击左侧内容类别，填入群名称和 Webhook。</li><li>点击“保存群订阅”，在列表核对群名与保存时间。</li></ol><p>不需要先登记空群。已有群可直接复用；测试连接仍须单独确认。</p></aside>}
      </div> : null}
      {view === 'groups' ? <div className="rd-form">
        <div className="rd-heading"><h3>已保存的群连接</h3></div>
        <p className="rd-muted">日常新增请用“新增群订阅”；这里用于维护连接、暂停目标与人工确认测试。连接已保存不代表已绑定内容。</p>
        {!groups.length ? <p className="rd-empty">暂无已保存群连接。点击“新增群订阅”一次完成配置。</p> : <ul className="rd-list">{groups.map(g => <li key={g.id} className="rd-group-row">
          <div className="min-w-0"><strong>{g.alias}</strong><div className="rd-inline"><span className="rd-muted">{groupKinds.find(k => k.value === g.kind)?.label} · v{g.version}</span><Badge variant={g.credential_configured ? 'success' : 'warning'}>{g.credential_configured ? '连接已保存' : '缺 Webhook'}</Badge><Badge>{g.enabled ? '目标启用' : '已暂停'}</Badge></div><p className="rd-muted">{subscriptions.filter(s => s.destination_id === g.id).length ? `已绑定 ${subscriptions.filter(s => s.destination_id === g.id).length} 类内容` : '尚未绑定内容'} · 保存于 {displayTime(g.updated_at)}</p></div>
          <div className="rd-inline"><Button variant="ghost" aria-label={`编辑 ${g.alias}`} disabled={blocked || dirty || !!test} onClick={() => setGroupEditor({ target: g })}>编辑</Button>
            <Button variant="settings-secondary" aria-label={`${g.enabled ? '暂停' : '启用'} ${g.alias}`} disabled={blocked || dirty || !!test || (!g.enabled && !g.credential_configured)} onClick={() => void perform(async () => updateGroup(await api.updateDestination(g.id, { version: g.version, enabled: !g.enabled })), '目标状态已保存；没有发送消息')}>{g.enabled ? '暂停' : '启用'}</Button>
            <Button variant="settings-secondary" aria-label={`测试连接 ${g.alias}`} disabled={blocked || dirty || !!test || !g.enabled || !g.credential_configured || !caps.manual_test_available || records.some(r => r.destination_id === g.id && ['sending', 'pending', 'delivery_unknown'].includes(r.status))} onClick={e => {
              testTrigger.current = e.currentTarget;
              const unresolved = uncertainTests[g.id];
              setTest({ target: g, requestId: unresolved?.requestId ?? crypto.randomUUID(), uncertain: !!unresolved });
            }}>测试连接</Button></div>
        </li>)}</ul>}
        <details className="rd-advanced"><summary>高级：只登记测试或备用群，不订阅内容</summary><Button variant="settings-secondary" disabled={blocked || dirty || !!test} onClick={() => { setGroupEditor({}); setError(''); }}>登记测试或备用群</Button></details>
        {groupEditor ? <GroupEditor key={`${groupEditor.target?.id ?? 'new'}:${groupEditor.target?.version ?? 0}`} target={groupEditor.target} disabled={blocked} onDirtyChange={setGroupDirty} onClose={() => { setGroupEditor(null); setGroupDirty(false); }}
          onSave={async body => { await perform(async () => { const saved = groupEditor.target ? await api.updateDestination(groupEditor.target.id, { ...body, version: groupEditor.target.version }) : await api.createDestination(body); updateGroup(saved); setGroupEditor({ target: saved }); }, '群记录已保存，接下来可录入该群连接'); }}
          onCredentials={body => perform(async () => { if (groupEditor.target) updateGroup(await api.credentials(groupEditor.target.id, body)); }, '群连接已保存；密钥已清空显示，没有发送消息')} /> : null}
        {test ? <div ref={confirmationRef} tabIndex={-1} role="region" aria-label="确认向此群发测试" className="rd-confirm">
          <h3>确认向此群发测试</h3><strong>{test.target.alias}</strong><p className="rd-muted">仅向此群发送下面这一条固定测试，不包含股票结论，不会连带其他群。</p><pre className="rd-report-content">{caps.test_message}</pre>
          {test.result ? <DeliveryNotice record={test.result} /> : test.uncertain ? <p role="alert" className="rd-error">请求结果未知，可能已送达。请核对群和投递记录，不要重复发送。请求编号：{test.requestId}</p> : null}
          <div className="rd-inline"><Button variant="settings-primary" disabled={blocked || !!test.result || test.uncertain} onClick={() => void perform(async () => {
            try { const result = await api.test(test.target.id, { version: test.target.version, confirm: true, request_id: test.requestId }); setTest({ ...test, result }); setRecords(rows => replace(rows, result)); }
            catch (e) { setUncertainTests(rows => ({ ...rows, [test.target.id]: { requestId: test.requestId, version: test.target.version } })); setTest({ ...test, uncertain: true }); throw e; }
          })}>确认发送这一条</Button><Button variant="ghost" disabled={busy} onClick={closeTest}>{test.result || test.uncertain ? '关闭并核对记录' : '取消，不发送'}</Button></div>
        </div> : null}
      </div> : null}
      {view === 'preview' ? <ResearchPreview subscriptions={subscriptions} groups={groups} categories={categories} capabilities={caps} publications={publications} records={records} disabled={blocked} onPreview={async (id, run, llm) => {
        let result: ResearchPublication | null = null;
        await perform(async () => { result = await api.preview(id, { source_run_id: run, use_llm: llm }); setPublications(rows => replace(rows, result!)); }, '研究草稿已保存；没有发送消息');
        return result;
      }} /> : null}
      {view === 'deliveries' ? <div className="rd-form"><p className="rd-muted">记录按独立目标保留。超时或送达未知不自动重试；飞书已接收不等于已读。</p>{!records.length ? <div className="rd-empty">暂无投递记录。保存群或订阅不会产生发送记录。</div> : <ul className="rd-list">{[...records].reverse().map(r => <li key={r.id} className="rd-delivery-row"><div className="rd-heading"><strong>{groups.find(g => g.id === r.destination_id)?.alias ?? '历史目标'}</strong><Badge variant={r.status === 'sent' ? 'success' : 'warning'}>{deliveryLabels[r.status]}</Badge></div><p className="rd-muted">{displayTime(r.created_at)} · 目标版本 {r.destination_version}</p><DeliveryNotice record={r} /></li>)}</ul>}</div> : null}
    </> : <p className="rd-muted">多群工作区尚未就绪。请检查管理员登录和后端版本，再点击“重新读取”。</p>}
  </SettingsSectionCard>;
}
