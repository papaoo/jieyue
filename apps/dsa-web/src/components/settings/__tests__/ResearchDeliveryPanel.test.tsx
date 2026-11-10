import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ResearchDeliveryPanel } from '../ResearchDeliveryPanel';
const api = vi.hoisted(() => Object.fromEntries(['automation', 'updateAutomation', 'runAutomation', 'setup', 'setupStatus', 'capabilities', 'categories', 'destinations', 'subscriptions', 'publications', 'deliveries', 'createDestination', 'updateDestination', 'credentials', 'createSubscription', 'updateSubscription', 'sources', 'preview', 'test'].map(k => [k, vi.fn()])));
vi.mock('../../../api/researchDelivery', () => ({ researchDeliveryApi: api }));
const group = { id: 'g1', alias: '突破群', kind: 'strategy', version: 2, enabled: true, credential_configured: true, signing_configured: true, keyword: '阶跃' };
const category = { category_id: 'volume_breakout', display_name: '放量突破', code: 'S01', destination_kind: 'strategy', producer_status: 'live_on_demand', preview_available: true, planned_time: '17:10', description: '规则筛选' };
const sub = { id: 's1', name: '突破日报', destination_id: 'g1', strategy_id: 'volume_breakout', top_n: 5, llm_mode: 'explain_only', planned_time: '17:10', enabled: false, version: 1 };
const caps = { llm_available: true, llm_daily_attempt_limit: 20, scheduler_active: false, manual_test_available: true, test_message: '阶跃 · 固定群连接测试', formal_publication_available: false };
describe('ResearchDeliveryPanel', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.automation.mockResolvedValue({ enabled: false, version: 1, worker_alive: true, running: false, heartbeat_at: null, collection_time: '15:30', cutoff_time: '22:30', subscriptions: [], last_error: null });
    api.capabilities.mockResolvedValue(caps); api.categories.mockResolvedValue([category]);
    api.destinations.mockResolvedValue([group]); api.subscriptions.mockResolvedValue([sub]);
    api.publications.mockResolvedValue([]); api.deliveries.mockResolvedValue([]); api.sources.mockResolvedValue([]);
  });
  it('binds an independent category to its own group without sending', async () => {
    api.subscriptions.mockResolvedValue([]); api.setup.mockResolvedValue({ request_id: 'setup', destination: group, subscription: sub });
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: /S01.*放量突破/ }));
    fireEvent.click(screen.getByLabelText('使用已保存的群连接'));
    fireEvent.change(screen.getByLabelText('已有群连接'), { target: { value: 'g1' } });
    fireEvent.click(screen.getByRole('button', { name: '保存群订阅' }));
    await waitFor(() => expect(api.setup).toHaveBeenCalledWith(expect.objectContaining({ destination_id: 'g1', strategy_id: 'volume_breakout', planned_time: '17:10' })));
    expect(await screen.findByText(/已保存：突破日报 → 突破群/)).toBeInTheDocument();
    expect(api.test).not.toHaveBeenCalled(); expect(api.preview).not.toHaveBeenCalled();
  });
  it('saves and clears only the selected group credentials', async () => {
    api.credentials.mockResolvedValue({ ...group, version: 3 });
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '群连接' }));
    fireEvent.click(screen.getByRole('button', { name: /编辑 突破群/ }));
    const input = screen.getByLabelText('该群 Webhook');
    expect(input).toHaveValue(''); expect(input).toHaveAttribute('type', 'password');
    fireEvent.change(input, { target: { value: 'https://open.feishu.cn/open-apis/bot/v2/hook/fixture' } });
    fireEvent.click(screen.getByRole('button', { name: '保存群连接' }));
    await waitFor(() => expect(api.credentials).toHaveBeenCalledWith('g1', expect.objectContaining({ version: 2 })));
    await waitFor(() => expect(screen.getByLabelText('该群 Webhook')).toHaveValue(''));
    expect(api.test).not.toHaveBeenCalled();
  });
  it('requires a named-target confirmation and never retries an unknown send', async () => {
    api.test.mockResolvedValue({ id: 'd1', destination_id: 'g1', status: 'delivery_unknown', attempt_count: 1, request_id: 'request', created_at: '2026-10-15' });
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '群连接' }));
    fireEvent.click(screen.getByRole('button', { name: '测试连接 突破群' }));
    expect(api.test).not.toHaveBeenCalled();
    const confirm = screen.getByRole('region', { name: '确认向此群发测试' });
    expect(within(confirm).getByText('突破群')).toBeInTheDocument();
    expect(within(confirm).getByText(caps.test_message)).toBeInTheDocument();
    fireEvent.click(within(confirm).getByRole('button', { name: '确认发送这一条' }));
    await screen.findByText(/可能已送达/);
    expect(api.test).toHaveBeenCalledTimes(1);
  });
  it('protects an unsaved connection keyword from a metadata save/remount', async () => {
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '群连接' }));
    fireEvent.click(screen.getByRole('button', { name: /编辑 突破群/ }));
    fireEvent.change(screen.getByLabelText('机器人关键词'), { target: { value: '新关键词' } });
    expect(screen.getByLabelText('群名称')).toBeDisabled();
    expect(screen.getByRole('button', { name: '保存群名称与用途' })).toBeDisabled();
    expect(screen.getByLabelText('机器人关键词')).toHaveValue('新关键词');
    expect(screen.getByLabelText('该群 Webhook')).not.toBeDisabled();
  });
  it('sequences metadata then connection editing with the new version, without a save deadlock', async () => {
    api.updateDestination.mockResolvedValue({ ...group, alias: '新群名称', version: 3 });
    api.credentials.mockResolvedValue({ ...group, alias: '新群名称', version: 4 });
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '群连接' }));
    fireEvent.click(screen.getByRole('button', { name: /编辑 突破群/ }));
    fireEvent.change(screen.getByLabelText('群名称'), { target: { value: '新群名称' } });
    expect(screen.getByLabelText('该群 Webhook')).toBeDisabled();
    expect(screen.getByLabelText('该群签名密钥（可选）')).toBeDisabled();
    expect(screen.getByLabelText('机器人关键词')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '保存群名称与用途' }));
    await waitFor(() => expect(screen.getByLabelText('该群 Webhook')).not.toBeDisabled());
    fireEvent.change(screen.getByLabelText('该群 Webhook'), { target: { value: 'https://open.feishu.cn/open-apis/bot/v2/hook/fixture' } });
    expect(screen.getByLabelText('群名称')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '保存群连接' }));
    await waitFor(() => expect(api.credentials).toHaveBeenCalledWith('g1', expect.objectContaining({ version: 3 })));
    await waitFor(() => expect(screen.getByLabelText('该群 Webhook')).toHaveValue(''));
  });
  it('uses saved sources and only invokes LLM with explicit consent', async () => {
    api.sources.mockResolvedValue([{ source_run_id: 'run1', created_at: '2026-10-14', candidate_count: 3, business_date: null }]);
    api.preview.mockResolvedValue({ id: 'p1', subscription_id: 's1', destination_id: 'g1', status: 'blocked', blockers: ['w1_unverified'], envelope: { w1_verified: false, evidence: [] }, rendered_content: '原始风险与证据', llm: { status: 'not_requested' } });
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '研究预览' }));
    fireEvent.change(screen.getByLabelText('选择订阅'), { target: { value: 's1' } });
    await waitFor(() => expect(api.sources).toHaveBeenCalledWith('volume_breakout'));
    fireEvent.change(screen.getByLabelText('已保存的策略运行'), { target: { value: 'run1' } });
    fireEvent.click(screen.getByRole('button', { name: '生成研究草稿' }));
    await waitFor(() => expect(api.preview).toHaveBeenCalledWith('s1', { source_run_id: 'run1', use_llm: false }));
    expect(await screen.findByText('原始风险与证据')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '历史草稿不可正式发布' })).toBeDisabled();
  });
  it('passes saved delivery identity to the report viewer, not publication status', async () => {
    api.publications.mockResolvedValue([{ id: 'p1', subscription_id: 's1', destination_id: 'g1', destination_version: 2,
      status: 'prepared', created_at: '2026-10-15T09:10:00', blockers: [], rendered_content: '正式冻结内容', llm: { status: 'off' } }]);
    api.deliveries.mockResolvedValue([{ id: 'd1', kind: 'formal', publication_id: 'p1', destination_id: 'g1', destination_version: 2, status: 'sent' }]);
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '研究预览' }));
    fireEvent.click(screen.getByRole('button', { name: /突破日报.*正式研究报告/ }));
    expect(within(screen.getByRole('article')).getByText('飞书已接收（不等于已读）')).toBeInTheDocument();
    expect(api.preview).not.toHaveBeenCalled(); expect(api.test).not.toHaveBeenCalled(); expect(api.runAutomation).not.toHaveBeenCalled();
  });
  it.each([true, false])('passes actual global model availability %s to both editors', async llmAvailable => {
    api.capabilities.mockResolvedValue({ ...caps, llm_available: llmAvailable });
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: /S01.*放量突破/ }));
    expect(screen.getByLabelText('大模型作用').matches(':disabled')).toBe(!llmAvailable);
    fireEvent.click(screen.getByRole('button', { name: '取消编辑' }));
    fireEvent.click(screen.getByRole('button', { name: '新增群订阅' }));
    fireEvent.change(screen.getByLabelText('内容类别'), { target: { value: 'volume_breakout' } });
    expect(screen.getByLabelText('大模型作用').matches(':disabled')).toBe(!llmAvailable);
    expect(api.preview).not.toHaveBeenCalled(); expect(api.test).not.toHaveBeenCalled();
  });
  it.each([401, 403, 409, 422, 503])('shows safe errors for HTTP %s without secrets', async (status) => {
    api.capabilities.mockRejectedValue({ response: { status, data: { message: 'PRIVATE-HOOK-URL' } } });
    render(<ResearchDeliveryPanel />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText(/PRIVATE-HOOK/)).not.toBeInTheDocument();
  });
  it('reports local dirty state and resets drafts explicitly', async () => {
    const onDirtyChange = vi.fn();
    const result = render(<ResearchDeliveryPanel onDirtyChange={onDirtyChange} draftResetToken={0} />);
    fireEvent.click(await screen.findByRole('button', { name: '群连接' }));
    fireEvent.click(screen.getByRole('button', { name: '新增群订阅' }));
    fireEvent.change(screen.getByLabelText('飞书群名称'), { target: { value: '新群草稿' } });
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));
    result.rerender(<ResearchDeliveryPanel onDirtyChange={onDirtyChange} draftResetToken={1} />);
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));
  });
  it('refreshes runtime status without replacing a dirty subscription or reporting a save', async () => {
    const onDirtyChange = vi.fn(), onSavingChange = vi.fn();
    render(<ResearchDeliveryPanel onDirtyChange={onDirtyChange} onSavingChange={onSavingChange} />);
    fireEvent.click(await screen.findByRole('button', { name: /S01.*放量突破/ }));
    fireEvent.change(screen.getByLabelText('订阅名称'), { target: { value: '保留订阅草稿' } });
    fireEvent.click(screen.getByRole('button', { name: '刷新运行状态' }));
    await waitFor(() => expect(api.automation).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText('订阅名称')).toHaveValue('保留订阅草稿');
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    expect(onSavingChange).not.toHaveBeenCalledWith(true);
    expect(api.subscriptions).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: '启用所选群' })).toBeDisabled();
  });
  it('refreshes runtime status without touching dirty group credentials', async () => {
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '群连接' }));
    fireEvent.click(screen.getByRole('button', { name: /编辑 突破群/ }));
    fireEvent.change(screen.getByLabelText('机器人关键词'), { target: { value: '保留关键词' } });
    fireEvent.click(screen.getByRole('button', { name: '刷新运行状态' }));
    await waitFor(() => expect(api.automation).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText('机器人关键词')).toHaveValue('保留关键词');
    expect(api.destinations).toHaveBeenCalledTimes(1);
  });
  it('keeps the independent setup draft when refreshing runtime', async () => {
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '新增群订阅' }));
    fireEvent.change(screen.getByLabelText('飞书群名称'), { target: { value: '新群未保存' } });
    fireEvent.click(screen.getByRole('button', { name: '刷新运行状态' }));
    await waitFor(() => expect(api.automation).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText('飞书群名称')).toHaveValue('新群未保存');
    expect(api.setup).not.toHaveBeenCalled();
  });
  it('reports actual global state after an individual subscription toggle', async () => {
    api.automation.mockResolvedValue({ enabled: true, version: 2, worker_alive: false, subscriptions: [], last_error: null });
    api.updateSubscription.mockResolvedValue({ ...sub, enabled: true, version: 2 });
    render(<ResearchDeliveryPanel />);
    fireEvent.click(await screen.findByRole('button', { name: /S01.*放量突破/ }));
    fireEvent.click(screen.getByRole('button', { name: '启用该订阅' }));
    expect(await screen.findByRole('status')).toHaveTextContent('工作进程离线');
    expect(screen.queryByText(/自动调度仍未启用/)).not.toBeInTheDocument();
  });
});
