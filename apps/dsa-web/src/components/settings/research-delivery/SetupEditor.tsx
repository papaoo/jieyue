import { useEffect, useRef, useState } from 'react';
import { Button, Input, Select } from '../../common';
import { researchDeliveryApi as api } from '../../../api/researchDelivery';
import type { DeliveryCategory, DeliveryDestination, SetupInput, SetupResult } from '../../../types/researchDelivery';
import { safeDeliveryError, type EditorStateProps } from './shared';

interface Props extends EditorStateProps {
  categories: DeliveryCategory[]; groups: DeliveryDestination[]; initialCategoryId?: string; llmAvailable: boolean;
  onSavingChange: (busy: boolean) => void; onSaved: (saved: SetupResult) => void; onClose: () => void;
}
const statusOf = (error: unknown) => (error as { response?: { status?: number } })?.response?.status;

export function SetupEditor({ categories, groups, initialCategoryId = '', llmAvailable, disabled, onDirtyChange, onSavingChange, onSaved, onClose }: Props) {
  const initialCategory = categories.find(c => c.category_id === initialCategoryId);
  const [categoryId, setCategoryId] = useState(initialCategoryId);
  const [mode, setMode] = useState<'new' | 'existing'>('new');
  const [targetId, setTargetId] = useState('');
  const [alias, setAlias] = useState('');
  const [webhook, setWebhook] = useState('');
  const [secret, setSecret] = useState('');
  const [keyword, setKeyword] = useState('阶跃');
  const [topN, setTopN] = useState(5);
  const [llm, setLlm] = useState<'off' | 'explain_only'>('explain_only');
  const [time, setTime] = useState(initialCategory?.planned_time ?? '17:10');
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [pending, setPending] = useState<SetupInput | null>(null);
  const [checkedUnknown, setCheckedUnknown] = useState(false);
  const [error, setError] = useState('');
  const category = categories.find(c => c.category_id === categoryId);
  const target = groups.find(g => g.id === targetId);
  const dirty = !!(categoryId || alias || webhook || secret || targetId || pending || keyword !== '阶跃' || time !== '17:10' || topN !== 5 || llm !== 'explain_only');
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onSavingChange(busy); }, [busy, onSavingChange]);
  useEffect(() => () => { onDirtyChange(false); onSavingChange(false); }, [onDirtyChange, onSavingChange]);
  const finish = (saved: SetupResult) => { setWebhook(''); setSecret(''); setPending(null); onSaved(saved); };
  const submit = async (body: SetupInput) => {
    if (lock.current || disabled) return;
    lock.current = true; setBusy(true); setError(''); setCheckedUnknown(false);
    try { finish(await api.setup(body)); }
    catch (e) {
      // A network error/5xx/timeout can happen after commit: never assign a new ID.
      if (pending) {
        // A rejected retry says nothing about the first uncertain commit.
        // Keep its identity and frozen payload until receipt lookup succeeds.
        setPending(body);
        setError('本次重试未获成功确认；首次保存结果仍未确认。请保留原编号，恢复登录或服务后继续核对，不要另建一份订阅。');
      } else if ([400, 401, 403, 404, 409, 422].includes(statusOf(e) ?? 0)) {
        setPending(null); setError(`未保存：${safeDeliveryError(e)}`);
      } else {
        setPending(body); setError('保存结果尚未确认，可能已经成功。先核对结果，不要另建一份订阅。');
      }
    } finally { lock.current = false; setBusy(false); }
  };
  const verify = async () => {
    if (!pending || lock.current || disabled) return;
    lock.current = true; setBusy(true);
    try { finish(await api.setupStatus(pending.request_id)); }
    catch (e) {
      setCheckedUnknown(true);
      setError(statusOf(e) === 404 ? '暂未查到已提交记录；请求仍可能在处理。可稍后核对，或用原编号重试同一份内容。' : `暂时无法核对：${safeDeliveryError(e)}`);
    } finally { lock.current = false; setBusy(false); }
  };
  const canSave = !!category && (mode === 'new' ? !!alias.trim() && !!webhook.trim() : !!target?.credential_configured);
  return <section className="rd-editor rd-setup" aria-label="新增群订阅表单">
    <div className="rd-heading"><div><span className="rd-kicker">一次保存 · 不发送消息</span><h3>新增群订阅</h3></div><Button variant="ghost" disabled={busy} onClick={onClose}>取消编辑</Button></div>
    <p className="rd-muted">先在飞书建群并添加自定义机器人；这里连接该群，不会替你创建飞书群。</p>
    <form onSubmit={e => {
      e.preventDefault(); if (!category || !canSave || pending) return;
      void submit({ request_id: crypto.randomUUID(), name: `${category.code} ${category.display_name}`, strategy_id: category.category_id,
        top_n: topN, llm_mode: llm, planned_time: time,
        ...(mode === 'new' ? { new_destination: { alias: alias.trim(), webhook_url: webhook.trim(), signing_secret: secret.trim(), keyword: keyword.trim() } }
          : { destination_id: target!.id, destination_version: target!.version }) });
    }}>
      <fieldset className="rd-form" disabled={disabled || busy || !!pending}>
        <h4>1 · 选择要接收的内容</h4>
        <Select label="内容类别" value={categoryId} options={categories.map(c => ({ value: c.category_id, label: `${c.code} ${c.display_name}` }))} onChange={value => { setCategoryId(value); setTime(categories.find(c => c.category_id === value)!.planned_time); }} />
        {category && !category.preview_available ? <p className="rd-muted">{category.producer_status === 'live_on_demand' ? '此类已接入当日数据生产；本页没有历史运行预览。' : '此类内容尚未接入，目前只保存群订阅。'}</p> : null}
        <h4>2 · 连接目标飞书群</h4>
        <div className="rd-inline"><label className="rd-choice"><input type="radio" name="setup-target-mode" checked={mode === 'new'} onChange={() => setMode('new')} />连接一个新群</label><label className="rd-choice"><input type="radio" name="setup-target-mode" checked={mode === 'existing'} onChange={() => setMode('existing')} />使用已保存的群连接</label></div>
        {mode === 'new' ? <>
          <Input label="飞书群名称" required maxLength={100} value={alias} placeholder="例如：阶跃｜S01 放量突破" onChange={e => setAlias(e.target.value)} />
          <Input label="该群 Webhook" required type="password" autoComplete="new-password" maxLength={512} value={webhook} placeholder="粘贴该群自定义机器人的 HTTPS 地址" onChange={e => setWebhook(e.target.value)} />
          <Input label="该群签名密钥（可选）" type="password" autoComplete="new-password" maxLength={512} value={secret} hint="开启签名校验才需要填写；不需要 App ID / App Secret。" onChange={e => setSecret(e.target.value)} />
          <Input label="机器人关键词" value={keyword} maxLength={100} onChange={e => setKeyword(e.target.value)} />
        </> : <><Select label="已有群连接" value={targetId} options={groups.filter(g => g.credential_configured).map(g => ({ value: g.id, label: g.alias }))} onChange={setTargetId} /><p className="rd-muted">只列出连接已保存的群。复用不会更换其机器人或修改其他订阅。</p></>}
        <h4>3 · 设置内容参数</h4>
        <div className="rd-form-pair"><Input label="Top N" type="number" min={1} max={20} required value={topN} disabled={!category?.preview_available} onChange={e => setTopN(Number(e.target.value))} /><Input label="计划时间（北京时间）" type="time" value={time} required onChange={e => setTime(e.target.value)} /></div>
        <Select label="大模型作用" value={llm} disabled={!llmAvailable || !(category?.producer_status === 'live_on_demand' || category?.preview_available)} options={[{ value: 'explain_only', label: '解释已有结果，不改名单和排序' }, { value: 'off', label: '关闭，仅保留事实和风险' }]} onChange={value => setLlm(value as typeof llm)} />
        <p className="rd-muted">保存不会计算、调用模型或发送消息。新群订阅默认暂停，不因总开关已启用而自动加入；保存后须另行明确选择启用。</p>
        <Button type="submit" variant="settings-primary" disabled={!canSave} isLoading={busy}>保存群订阅</Button>
      </fieldset>
    </form>
    {error ? <div role="alert" className="rd-error">{error}</div> : null}
    {pending ? <div className="rd-confirm"><p className="rd-muted">保存编号：{pending.request_id}。表单已锁定，防止误改后重复创建；关闭后仍可在订阅列表核对。</p><div className="rd-inline"><Button disabled={busy || disabled} variant="settings-primary" onClick={() => void verify()}>核对是否已保存</Button>{checkedUnknown ? <Button disabled={busy || disabled} variant="settings-secondary" onClick={() => void submit(pending)}>用原编号重试保存</Button> : null}</div></div> : null}
  </section>;
}
