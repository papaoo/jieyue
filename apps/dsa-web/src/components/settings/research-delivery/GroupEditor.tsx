import { useEffect, useState } from 'react';
import { Button, Input, Select, Badge } from '../../common';
import type { CredentialInput, DeliveryDestination, DestinationKind } from '../../../types/researchDelivery';
import { groupKinds, type EditorStateProps } from './shared';
interface Props extends EditorStateProps {
  target?: DeliveryDestination;
  onSave: (body: { alias: string; kind: DestinationKind }) => Promise<void>;
  onCredentials: (body: CredentialInput) => Promise<boolean>;
  onClose: () => void;
}
export function GroupEditor({ target, disabled, onDirtyChange, onSave, onCredentials, onClose }: Props) {
  const [alias, setAlias] = useState(target?.alias ?? '');
  const [kind, setKind] = useState<DestinationKind>(target?.kind ?? 'strategy');
  const [webhook, setWebhook] = useState('');
  const [secret, setSecret] = useState('');
  const [keyword, setKeyword] = useState(target?.keyword ?? '阶跃');
  const metaDirty = alias !== (target?.alias ?? '') || kind !== (target?.kind ?? 'strategy');
  const credentialDirty = !!webhook || !!secret || keyword !== (target?.keyword ?? '阶跃');
  const dirty = metaDirty || credentialDirty;
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);
  return <section className="rd-editor" aria-label="群配置编辑器">
    <div className="rd-heading"><h3>{target ? '编辑独立群' : '新增独立群'}</h3><Button variant="ghost" onClick={onClose} disabled={disabled}>取消编辑</Button></div>
    <form onSubmit={e => { e.preventDefault(); void onSave({ alias: alias.trim(), kind }); }}>
      <fieldset disabled={disabled || credentialDirty} className="rd-form">
        <Input label="群名称" value={alias} maxLength={100} required placeholder="阶跃｜S01 放量突破" onChange={e => setAlias(e.target.value)} />
        <Select label="群用途" value={kind} options={groupKinds} onChange={value => setKind(value as DestinationKind)} />
        <Button type="submit" variant="settings-primary" disabled={!alias.trim() || (!!target && !metaDirty) || !!webhook || !!secret}>
          {target ? '保存群名称与用途' : '创建群记录'}
        </Button>
      </fieldset>
    </form>
    {target ? <form onSubmit={async e => {
      e.preventDefault();
      const ok = await onCredentials({ version: target.version, webhook_url: webhook.trim(), ...(secret.trim() ? { signing_secret: secret.trim() } : {}), keyword: keyword.trim() });
      if (ok) { setWebhook(''); setSecret(''); }
    }}>
      <fieldset disabled={disabled || metaDirty} className="rd-form rd-separator">
        <div className="rd-inline"><Badge variant={target.credential_configured ? 'success' : 'warning'}>{target.credential_configured ? '连接已保存' : '未配置连接'}</Badge><Badge>{target.signing_configured ? '已配置签名' : '未配置签名'}</Badge></div>
        <Input label="该群 Webhook" type="password" autoComplete="new-password" value={webhook} required maxLength={512} placeholder={target.credential_configured ? '已保存；更换时重新输入，不回显旧值' : '粘贴该群独立机器人的 HTTPS 地址'} onChange={e => setWebhook(e.target.value)} />
        <Input label="该群签名密钥（可选）" type="password" autoComplete="new-password" value={secret} maxLength={512} hint="同一地址留空保留旧签名；更换已签名地址须同时填写新密钥。" onChange={e => setSecret(e.target.value)} />
        <Input label="机器人关键词" value={keyword} maxLength={100} hint="建议飞书机器人和此处均设为“阶跃”。App ID / App Secret 无需填写。" onChange={e => setKeyword(e.target.value)} />
        <Button type="submit" variant="settings-primary" disabled={!webhook.trim() || metaDirty}>保存群连接</Button>
      </fieldset>
    </form> : <p className="rd-muted">先创建群记录，下一步录入该群独立的 Webhook 和签名密钥。新群默认暂停。</p>}
    {target ? <p className="rd-muted">名称/用途和连接分步保存：先保存当前部分，或取消编辑重新开始。修改签名或关键词时须重新填写当前 Webhook。</p> : null}
    <p className="rd-muted">连接仅写入后端受保护存储；不会回显、同步到浏览器存储或随保存发送消息。</p>
  </section>;
}
