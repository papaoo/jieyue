import { useEffect, useState } from 'react';
import { Button, Input, Select, Badge } from '../../common';
import type { DeliveryCategory, DeliveryDestination, DeliverySubscription, SubscriptionInput } from '../../../types/researchDelivery';
import type { EditorStateProps } from './shared';

interface Props extends EditorStateProps {
  category: DeliveryCategory; subscription?: DeliverySubscription; groups: DeliveryDestination[]; llmAvailable: boolean;
  onSave: (body: SubscriptionInput) => Promise<void>;
  onToggle: () => Promise<void>; onCancel: () => void;
}
export function SubscriptionEditor({ category, subscription, groups, llmAvailable, disabled, onDirtyChange, onSave, onToggle, onCancel }: Props) {
  const initial: SubscriptionInput = {
    name: subscription?.name ?? `${category.code} ${category.display_name}`,
    strategy_id: category.category_id, destination_id: subscription?.destination_id ?? '',
    top_n: subscription?.top_n ?? 5, llm_mode: subscription?.llm_mode ?? 'explain_only',
    planned_time: subscription?.planned_time ?? category.planned_time,
  };
  const [draft, setDraft] = useState(initial);
  const dirty = JSON.stringify(draft) !== JSON.stringify(initial);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);
  return <section className="rd-editor" aria-label="分类订阅编辑器">
    <div className="rd-heading"><div><span className="rd-kicker">{category.code} / 独立路由</span><h3>{category.display_name}</h3></div><Badge variant={category.producer_status === 'live_on_demand' ? 'info' : 'warning'}>{category.producer_status === 'live_on_demand' ? '已接入当日数据生产' : category.preview_available ? '可预览历史研究' : '仅配置，内容待接入'}</Badge></div>
    <p className="rd-muted">{category.description}</p>
    <form onSubmit={e => { e.preventDefault(); void onSave(draft); }}>
      <fieldset disabled={disabled} className="rd-form">
        <Input label="订阅名称" required maxLength={100} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} />
        <Select label="目标群" value={draft.destination_id} options={groups.map(g => ({ value: g.id, label: `${g.alias}${!g.credential_configured ? ' · 未配连接' : !g.enabled ? ' · 已暂停' : ''}` }))} onChange={value => setDraft({ ...draft, destination_id: value })} />
        {!groups.length ? <p className="rd-muted">请先在“群管理”创建一个群，再回到这里绑定。</p> : null}
        <div className="rd-form-pair">
          <Input label="Top N" type="number" min={1} max={20} required value={draft.top_n} disabled={!category.preview_available} onChange={e => setDraft({ ...draft, top_n: Number(e.target.value) })} />
          <Input label="计划时间（北京时间）" type="time" required value={draft.planned_time} onChange={e => setDraft({ ...draft, planned_time: e.target.value })} />
        </div>
        <Select label="大模型作用" disabled={!llmAvailable || !(category.producer_status === 'live_on_demand' || category.preview_available)} value={draft.llm_mode} options={[{ value: 'explain_only', label: '解释既有结果，不改名单与排序' }, { value: 'off', label: '关闭，仅展示原始事实与风险' }]} onChange={value => setDraft({ ...draft, llm_mode: value as SubscriptionInput['llm_mode'] })} />
        <p className="rd-muted">保存参数不发送。总开关、订阅和目标均启用且工作进程在线时，不早于北京时间计划执行真实采集和投递；数据或模型未就绪会顺延，22:30 截止。启用已有订阅可能使其进入到期处理。</p>
        <div className="rd-inline"><Button type="submit" variant="settings-primary" disabled={!draft.name.trim() || !draft.destination_id || (!!subscription && !dirty)}>保存该类订阅</Button><Button variant="ghost" onClick={onCancel}>取消编辑</Button></div>
      </fieldset>
    </form>
    {subscription ? <div className="rd-inline rd-separator"><Badge>{subscription.enabled ? '订阅已启用 · 受总开关与数据门禁约束' : '订阅已暂停'}</Badge><Button variant="settings-secondary" disabled={disabled || dirty} onClick={() => void onToggle()}>{subscription.enabled ? '暂停该订阅' : '启用该订阅'}</Button></div> : null}
  </section>;
}
