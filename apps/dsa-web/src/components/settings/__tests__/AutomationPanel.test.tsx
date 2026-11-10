import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AutomationPanel } from '../research-delivery/AutomationPanel';
import type { AutomationStatus, DeliveryCategory, DeliveryDestination, DeliverySubscription } from '../../../types/researchDelivery';

const api = vi.hoisted(() => ({ automation: vi.fn(), updateAutomation: vi.fn(), runAutomation: vi.fn() }));
vi.mock('../../../api/researchDelivery', () => ({ researchDeliveryApi: api }));
const groups = [{ id: 'g1', alias: '突破群', enabled: true, credential_configured: true }, { id: 'g2', alias: '回踩群', enabled: true, credential_configured: true }] as DeliveryDestination[];
const categories = [{ category_id: 'volume_breakout', code: 'S01', display_name: '放量突破', producer_status: 'live_on_demand' }] as DeliveryCategory[];
const subscriptions = [{ id: 's1', name: '突破日报', destination_id: 'g1', strategy_id: 'volume_breakout', enabled: false }, { id: 's2', name: '回踩日报', destination_id: 'g2', strategy_id: 'volume_breakout', enabled: true }] as DeliverySubscription[];
const status = (overrides: Partial<AutomationStatus> = {}): AutomationStatus => ({ enabled: false, version: 2, worker_alive: true, heartbeat_at: '2026-10-15T09:05:00', timezone: 'Asia/Shanghai', collection_time: '15:30', cutoff_time: '22:30', running: false, subscriptions: subscriptions.map(s => ({ subscription_id: s.id, name: s.name, category_id: s.strategy_id, destination_id: s.destination_id, planned_time: '17:10', enabled: s.enabled, job: null })), last_error: null, ...overrides });
function mount(extra: Partial<Parameters<typeof AutomationPanel>[0]> = {}) {
  const props = { groups, categories, subscriptions, disabled: false, visible: true, mutationLock: { current: false }, onSavingChange: vi.fn(), onStatusChange: vi.fn(), onControlChanged: vi.fn().mockResolvedValue(undefined), ...extra };
  return { ...render(<AutomationPanel {...props} />), props };
}
async function flush() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
describe('AutomationPanel', () => {
  beforeEach(() => { vi.resetAllMocks(); api.automation.mockResolvedValue(status()); api.updateAutomation.mockResolvedValue(status({ enabled: true })); });
  afterEach(() => { vi.useRealTimers(); });
  it('loads separate global/worker status and enables only named selected subscriptions without assuming a new epoch', async () => {
    mount();
    expect(await screen.findByText('自动推送已暂停')).toBeInTheDocument();
    expect(screen.getAllByText(/15:30.*22:30/)).toHaveLength(2);
    expect(screen.getByText(/2026\/10\/15 17:05:00/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/启用选择.*回踩日报/));
    fireEvent.click(screen.getByRole('button', { name: '启用所选群' }));
    await waitFor(() => expect(api.updateAutomation).toHaveBeenCalledWith({ version: 2, enabled: true, subscription_ids: ['s1'] }));
    expect(screen.getByRole('status')).toHaveTextContent('其他群状态不变');
    fireEvent.click(await screen.findByRole('button', { name: '暂停全部自动推送' }));
    await waitFor(() => expect(api.updateAutomation).toHaveBeenLastCalledWith({ version: 2, enabled: false, subscription_ids: [] }));
  });
  it('shows worker offline, blocked coverage and platform acceptance with correct units', async () => {
    const job = { id: 'j1', business_date: '2026-10-15', status: 'blocked', stage: 'collection', attempts: 1, error_code: 'market_response_coverage_below_95pct', coverage: { daily_response_coverage: .9, eligible_count: 3927, required_count: 35343, valid_count: 35000, technical_limit: 100, scope: 'restricted_complete_core_metric_eligible_universe' }, updated_at: '2026-10-15T09:05:00' };
    const base = status({ enabled: true, worker_alive: false }); base.subscriptions[0].job = job;
    api.automation.mockResolvedValue(base); mount();
    expect(await screen.findByText('自动推送已启用 · 工作进程离线')).toBeInTheDocument();
    expect(screen.getByText(/行情覆盖率 90.0%/)).toBeInTheDocument();
    expect(screen.getByText(/必需字段单元 35000\/35343/)).toBeInTheDocument();
    expect(screen.getByText(/行情覆盖率不足/)).toBeInTheDocument();
    expect(screen.queryByText(/35343 只/)).not.toBeInTheDocument();
  });
  it('does not claim enabled when the status API fails and exposes a read-only retry', async () => {
    api.automation.mockRejectedValue(new Error('PRIVATE-URL')); mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('自动化状态读取失败');
    expect(screen.getByRole('button', { name: '启用所选群' })).toBeDisabled();
    expect(screen.queryByText(/PRIVATE-URL/)).not.toBeInTheDocument();
    api.automation.mockResolvedValue(status()); fireEvent.click(screen.getByRole('button', { name: '刷新运行状态' }));
    expect(await screen.findByText('自动推送已暂停')).toBeInTheDocument();
  });
  it('labels bounded data retries without treating them as unknown or sent', async () => {
    const base = status({ enabled: true });
    base.subscriptions[0].job = { id: 'retry1', business_date: '2026-10-15', status: 'retry_wait', stage: 'collection', attempts: 1,
      error_code: 'source_timeout', coverage: {}, updated_at: '2026-10-15T09:05:00' };
    api.automation.mockResolvedValue(base); mount();
    expect(await screen.findByText(/等待数据重试/)).toBeInTheDocument();
    expect(screen.queryByText(/待核对/)).not.toBeInTheDocument();
    expect(screen.queryByText(/飞书已接收/)).not.toBeInTheDocument();
    expect(api.runAutomation).not.toHaveBeenCalled();
  });
  it('labels resumable full-scope batches without a source retry or send claim', async () => {
    const base = status({ enabled: true });
    base.subscriptions[0].job = { id: 'pending1', business_date: '2026-10-15', status: 'retry_wait', stage: 'collection_pending', attempts: 0,
      error_code: 'collection_pending', coverage: { contract_version: 2, technical_limit: 100, evaluation_scope_count: 150, evaluated_count: 100, pending_count: 50, failed_count: 0 }, updated_at: '2026-10-15T09:05:00' };
    api.automation.mockResolvedValue(base); mount();
    expect(await screen.findByText(/分批采集待续/)).toBeInTheDocument();
    expect(screen.getByText(/不占用源错误重试次数/)).toBeInTheDocument();
    expect(screen.getByText(/全范围评估 100\/150，待采 50，失败 0/)).toBeInTheDocument();
    expect(screen.queryByText(/飞书已接收/)).not.toBeInTheDocument();
  });
  it('freezes the run request ID after a lost response and only queries status', async () => {
    api.automation.mockResolvedValue(status({ enabled: true })); api.runAutomation.mockRejectedValue(new Error('lost'));
    mount(); fireEvent.click(await screen.findByRole('button', { name: '运行今天' }));
    expect(await screen.findByText(/运行请求结果未知/)).toBeInTheDocument();
    const requestId = api.runAutomation.mock.calls[0][0].request_id;
    expect(requestId).toBeTruthy(); expect(screen.getByText(new RegExp(requestId))).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '刷新运行状态' }));
    await waitFor(() => expect(api.automation).toHaveBeenCalledTimes(3));
    expect(api.runAutomation).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: '运行今天' })).toBeDisabled();
  });
  it('shows accepted as a queued receipt, refreshes jobs, and prevents duplicate actions while busy', async () => {
    api.automation.mockResolvedValue(status({ enabled: true }));
    let resolve!: (value: unknown) => void;
    api.runAutomation.mockImplementation(() => new Promise(r => { resolve = r; }));
    const { props } = mount();
    fireEvent.click(await screen.findByRole('button', { name: '运行今天' }));
    fireEvent.click(screen.getByRole('button', { name: '运行今天' }));
    expect(api.runAutomation).toHaveBeenCalledTimes(1); expect(props.mutationLock.current).toBe(true);
    await act(async () => resolve({ request_id: 'r1', business_date: '2026-10-15', authorization_version: 2, status: 'accepted' }));
    expect(screen.getByText(/已受理.*不代表已发送/)).toBeInTheDocument();
    expect(api.automation).toHaveBeenCalledTimes(2); expect(props.mutationLock.current).toBe(false);
  });
  it('requires explicit refresh after a version conflict and does not mutate selection', async () => {
    api.updateAutomation.mockRejectedValue({ response: { status: 409 } }); mount();
    const enableButton = screen.getByRole('button', { name: '启用所选群' });
    await waitFor(() => expect(enableButton).toBeEnabled());
    fireEvent.click(enableButton);
    await waitFor(() => expect(api.updateAutomation).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole('alert')).toHaveTextContent('版本');
    expect(api.automation).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: '启用所选群' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '刷新运行状态' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '启用所选群' })).not.toBeDisabled());
    expect(screen.getByLabelText(/启用选择.*突破日报/)).toBeChecked();
  });
  it('polls every five seconds only while visible and active, never overlaps reads, and stops on unmount', async () => {
    vi.useFakeTimers(); api.automation.mockResolvedValue(status({ enabled: true }));
    const { rerender, unmount, props } = mount(); await flush();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(api.automation).toHaveBeenCalledTimes(2);
    let finish!: (s: AutomationStatus) => void; api.automation.mockImplementation(() => new Promise(r => { finish = r; }));
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(api.automation).toHaveBeenCalledTimes(3);
    rerender(<AutomationPanel {...props} visible={false} />);
    await act(async () => { finish(status({ enabled: true })); await vi.advanceTimersByTimeAsync(10000); });
    expect(api.automation).toHaveBeenCalledTimes(3);
    api.automation.mockResolvedValue(status({ enabled: true })); rerender(<AutomationPanel {...props} visible />); await flush();
    expect(api.automation).toHaveBeenCalledTimes(4); unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); }); expect(api.automation).toHaveBeenCalledTimes(4);
  });
  it('does not silently select newly saved subscriptions', async () => {
    const { rerender, props } = mount(); await screen.findByText('自动推送已暂停');
    const extra = { ...subscriptions[0], id: 's3', name: '新保存的群' };
    rerender(<AutomationPanel {...props} subscriptions={[...subscriptions, extra]} />);
    expect(screen.getByLabelText(/启用选择.*新保存的群/)).not.toBeChecked();
    expect(within(screen.getByRole('region', { name: '自动推送控制' })).getByText(/真实采集.*模型.*发送/)).toBeInTheDocument();
  });
  it('keeps disabled runtime idle and pauses document-hidden polling until visible again', async () => {
    vi.useFakeTimers();
    const { unmount } = mount(); await flush();
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(api.automation).toHaveBeenCalledTimes(1); unmount();
    api.automation.mockResolvedValue(status({ enabled: true }));
    mount(); await flush();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    await act(async () => { fireEvent(document, new Event('visibilitychange')); await vi.advanceTimersByTimeAsync(10000); });
    expect(api.automation).toHaveBeenCalledTimes(2);
    visibility.mockReturnValue('visible');
    await act(async () => { fireEvent(document, new Event('visibilitychange')); });
    expect(api.automation).toHaveBeenCalledTimes(3); visibility.mockRestore();
  });
  it('renders category-specific announcement counts and accepted/unknown outcomes without claiming a read receipt', async () => {
    const base = status({ enabled: true });
    base.subscriptions[0] = { ...base.subscriptions[0], category_id: 'announcement_risk', job: { id: 'a', business_date: '2026-10-15', status: 'sent', stage: 'delivery', attempts: 1, error_code: null, coverage: { scanned_count: 20, unscanned_count: 80, verified_count: 2 }, updated_at: '2026-10-15T09:05:00' } };
    base.subscriptions[1].job = { ...base.subscriptions[0].job!, id: 'b', status: 'delivery_unknown', error_code: 'abandoned_sending', coverage: {} };
    api.automation.mockResolvedValue(base); mount();
    expect(await screen.findByText(/公告范围已扫描 20，未扫描 80/)).toBeInTheDocument();
    expect(screen.getByText('飞书已接收（不等于已读）')).toBeInTheDocument();
    expect(screen.getByText('送达未知，不自动重发')).toBeInTheDocument();
  });
  it('renders ETF asset counts when the status endpoint provides them, never as stock counts', async () => {
    const base = status();
    base.subscriptions[0] = { ...base.subscriptions[0], category_id: 'etf_rotation', job: { id: 'e', business_date: '2026-10-15', status: 'ready', stage: 'collected', attempts: 1, error_code: null, coverage: { valid_asset_count: 6, asset_count: 7, history_years: 1 }, updated_at: '2026-10-15T09:05:00' } };
    api.automation.mockResolvedValue(base); mount();
    expect(await screen.findByText(/有效资产 6\/7/)).toBeInTheDocument();
    expect(screen.queryByText(/有效样本 6 只/)).not.toBeInTheDocument();
  });
  it('retains the same daily run identity across a panel remount and queries before another explicit action', async () => {
    api.automation.mockResolvedValue(status({ enabled: true })); api.runAutomation.mockRejectedValue(new Error('lost'));
    const first = mount(); fireEvent.click(await screen.findByRole('button', { name: '运行今天' }));
    await screen.findByText(/运行请求结果未知/);
    const requestId = api.runAutomation.mock.calls[0][0].request_id; first.unmount();
    mount(); await screen.findByText('自动推送已启用 · 等待计划');
    expect(api.automation).toHaveBeenCalledTimes(3);
    fireEvent.click(screen.getByRole('button', { name: '运行今天' }));
    await screen.findByText(/运行请求结果未知/);
    expect(api.runAutomation).toHaveBeenLastCalledWith({ request_id: requestId });
  });
  it('does not mislabel an explicit paused run rejection as a version conflict or retry automatically', async () => {
    api.automation.mockResolvedValue(status({ enabled: true }));
    api.runAutomation.mockRejectedValue({ response: { status: 409, data: { error: 'automation_paused' } } });
    mount(); fireEvent.click(await screen.findByRole('button', { name: '运行今天' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('总开关已暂停');
    expect(screen.queryByText(/授权版本已变化/)).not.toBeInTheDocument();
    expect(api.runAutomation).toHaveBeenCalledTimes(1);
  });
  it('distinguishes scheduled delivery from run-today bypass while retaining all safety guards', async () => {
    mount(); await screen.findByText('自动推送已暂停');
    const panel = within(screen.getByRole('region', { name: '自动推送控制' }));
    expect(panel.getByText(/计划运行.*15:30.*开始跑策略并整合数据.*一个一个做研判并推送.*22:30/)).toBeInTheDocument();
    expect(panel.getByText(/运行今天.*立即排队.*不受已保存计划时间限制.*15:30.*22:30.*交易日.*数据.*版本.*凭据/)).toBeInTheDocument();
    expect(panel.getByText(/真实采集.*模型.*发送.*不是连接测试/)).toBeInTheDocument();
    expect(panel.getByText(/仅处理当天.*不补发历史.*localhost.*保持运行/)).toBeInTheDocument();
  });
});
