import { useEffect, useRef, useState } from 'react';
import { Sparkles, RefreshCw, Play, Power } from 'lucide-react';
import { freeModelsApi, type FreeModelStatus } from '../../api/freeModels';
import { systemConfigApi } from '../../api/systemConfig';
import { getParsedApiError } from '../../api/error';
import type { SystemConfigUpdateItem } from '../../types/systemConfig';
import { SettingsSectionCard } from './SettingsSectionCard';
import { Button } from '../common';

interface Props {
  configVersion: string;
  disabled?: boolean;
  onSavingChange: (busy: boolean) => void;
  onSaved: (keys: string[]) => Promise<void>;
}

export function FreeModelsPanel({ configVersion, disabled, onSavingChange, onSaved }: Props) {
  const [status, setStatus] = useState<FreeModelStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const mounted = useRef(false);
  useEffect(() => {
    let cancelled = false;
    mounted.current = true;
    freeModelsApi.status().then(value => { if (!cancelled) setStatus(value); })
      .catch(err => { if (!cancelled) setError(getParsedApiError(err).message); });
    return () => { cancelled = true; mounted.current = false; };
  }, [configVersion]);

  async function run(action: () => Promise<void>) {
    if (busy || disabled) return;
    setBusy(true); onSavingChange(true); setError(''); setMessage('');
    try { await action(); }
    catch (err) { if (mounted.current) setError(getParsedApiError(err).message); }
    finally { if (mounted.current) setBusy(false); onSavingChange(false); }
  }
  async function save(items: SystemConfigUpdateItem[]) {
    await systemConfigApi.update({ configVersion, items, reloadNow: true });
    await onSaved(items.map(item => item.key));
    const next = await freeModelsApi.status();
    if (mounted.current) { setStatus(next); setMessage('已保存，新任务立即使用当前配置。'); }
  }
  const models = status?.catalog?.models ?? [];
  const selectedListed = models.some(model => model.id === status?.model);
  const last = status?.recent_calls.find(call => call.model === status.model);

  return <SettingsSectionCard title="免费模型" description="OpenCode Zen · 默认 DeepSeek。统一用于分析、问股和推送研判，保留原有模型配置。"
    actions={<Sparkles size={20} className="text-cyan-400" />}>
    <div className="rounded-xl border border-cyan-400/20 bg-cyan-400/5 p-4 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><p className="font-medium text-foreground">{status?.enabled ? '免费通道已启用' : '免费通道未启用'}</p>
          <p className="text-sm text-muted-text mt-1">无需填写 API Key · 失败不会自动转付费</p></div>
        <Button disabled={busy || disabled || !status} onClick={() => void run(async () => {
          if (!status) return;
          if (!status.enabled) {
            const catalog = await freeModelsApi.refresh();
            if (!catalog.models.some(model => model.id === status.model)) {
              setStatus(previous => previous ? { ...previous, catalog } : previous);
              setError('所选模型当前未通过免费目录核验，请选择其他模型。');
              return;
            }
          }
          await save([{ key: 'FREE_MODEL_ENABLED', value: String(!status.enabled) }]);
        })}><Power size={15} />{status?.enabled ? '关闭并恢复常规模型' : '启用并设为默认'}</Button>
      </div>
      <label className="block text-sm text-muted-text">默认免费模型
        <select aria-label="默认免费模型" value={status?.model ?? ''} disabled={busy || disabled || !status}
          className="mt-2 block w-full rounded-lg border settings-border bg-card p-3 text-foreground"
          onChange={event => { const value = event.target.value; void run(() => save([{ key: 'FREE_MODEL_NAME', value }])); }}>
          {!selectedListed && status ? <option value={status.model}>{status.model}（待刷新目录核验）</option> : null}
          {models.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}
        </select>
      </label>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="secondary" disabled={busy || disabled} onClick={() => void run(async () => {
          const catalog = await freeModelsApi.refresh();
          setStatus(previous => previous ? { ...previous, catalog } : previous);
          setMessage(`已核验 ${catalog.models.length} 个免费模型；可调用性以连接测试为准。`);
        })}><RefreshCw size={15} />刷新目录</Button>
        <Button variant="secondary" disabled={busy || disabled || !status} onClick={() => void run(async () => {
          if (!status) return;
          const result = await freeModelsApi.test(status.model);
          setStatus(result.status);
          if (result.success) setMessage(`连接成功 · ${result.reply} · ${result.usage?.total_tokens ?? '未知'} tokens`);
          else setError(result.message ?? '测试回复不符合预期，请重试。');
        })}><Play size={15} />{busy ? '处理中…' : '测试连接'}</Button>
        {status ? <span className="text-sm text-muted-text">今日请求 {status.used_today} / {status.daily_limit}</span> : null}
      </div>
      {last ? <p className="text-xs text-muted-text">最近调用：{last.status === 'succeeded' ? '成功' : '失败'} · {(last.elapsed_ms / 1000).toFixed(1)} 秒 · {last.tokens ?? '未知'} tokens{last.code ? ` · ${last.code}` : ''}</p> : null}
      <p className="text-xs leading-5 text-muted-text">上游免费供应可能调整。阶跃独立计次，测试和失败请求也计入额度。启用期间下方常规渠道配置保留，关闭后恢复。研究推送自身的每日研判额度仍然生效。</p>
    </div>
    {message ? <p role="status" className="text-sm text-emerald-400">{message}</p> : null}
    {error ? <p role="alert" className="text-sm text-red-400">{error}</p> : null}
  </SettingsSectionCard>;
}
