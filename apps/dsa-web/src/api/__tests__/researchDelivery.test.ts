import { beforeEach, describe, expect, it, vi } from 'vitest';
import { researchDeliveryApi as api } from '../researchDelivery';
const { get, post, put, patch } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn() }));
vi.mock('../index', () => ({ default: { get, post, put, patch } }));
describe('research delivery wire contract', () => {
  beforeEach(() => { vi.resetAllMocks(); });
  it('reads runtime separately and preserves exact optimistic subset control and run receipt bodies', async () => {
    const state = { enabled: true, version: 2 };
    get.mockResolvedValue({ data: state }); put.mockResolvedValue({ data: state });
    post.mockResolvedValue({ data: { request_id: 'run-01', business_date: '2026-10-15', authorization_version: 2, status: 'accepted' } });
    expect(await api.automation()).toEqual(state);
    expect(await api.updateAutomation({ version: 2, enabled: true, subscription_ids: ['s1'] })).toEqual(state);
    expect(await api.runAutomation({ request_id: 'run-01' })).toMatchObject({ status: 'accepted' });
    expect(get).toHaveBeenCalledWith('/api/v1/research-delivery/automation');
    expect(put).toHaveBeenCalledWith('/api/v1/research-delivery/automation', { version: 2, enabled: true, subscription_ids: ['s1'] });
    expect(post).toHaveBeenCalledWith('/api/v1/research-delivery/automation/run', { request_id: 'run-01' });
  });
  it('does not retry an ambiguous run request', async () => {
    post.mockRejectedValue(new Error('network'));
    await expect(api.runAutomation({ request_id: 'run-stable' })).rejects.toThrow();
    expect(post).toHaveBeenCalledTimes(1);
  });
  it('uses one setup write and a separate read-only recovery endpoint', async () => {
    const body = { request_id: 'setup-01', name: 'sub', strategy_id: 'volume_breakout', top_n: 5, llm_mode: 'off' as const, planned_time: '17:10', destination_id: 'one', destination_version: 2 };
    post.mockResolvedValue({ data: { request_id: body.request_id } }); get.mockResolvedValue({ data: { request_id: body.request_id } });
    await api.setup(body); await api.setupStatus(body.request_id);
    expect(post).toHaveBeenCalledWith('/api/v1/research-delivery/setups', body);
    expect(get).toHaveBeenCalledWith('/api/v1/research-delivery/setups/setup-01');
    expect(post).toHaveBeenCalledTimes(1);
  });
  it('preserves snake_case and never returns secret placeholders', async () => {
    const row = { id: 'one', credential_configured: true, signing_configured: false };
    get.mockResolvedValue({ data: { items: [row] } });
    expect(await api.destinations()).toEqual([row]);
    expect(get).toHaveBeenCalledWith('/api/v1/research-delivery/destinations');
  });
  it('writes credentials only to the specific target with its version', async () => {
    put.mockResolvedValue({ data: { version: 3 } });
    const body = { version: 2, webhook_url: 'https://open.feishu.cn/open-apis/bot/v2/hook/fixture', keyword: '阶跃' };
    await api.credentials('one', body);
    expect(put).toHaveBeenCalledWith('/api/v1/research-delivery/destinations/one/credentials', body);
    expect(post).not.toHaveBeenCalled();
  });
  it('uses a bounded longer preview timeout and explicit LLM consent', async () => {
    post.mockResolvedValue({ data: { status: 'blocked' } });
    await api.preview('sub', { source_run_id: 'saved', use_llm: false });
    expect(post).toHaveBeenCalledWith('/api/v1/research-delivery/subscriptions/sub/preview', { source_run_id: 'saved', use_llm: false }, { timeout: 60000 });
  });
  it('does not retry an ambiguous test send', async () => {
    post.mockRejectedValue(new Error('network'));
    await expect(api.test('one', { version: 1, confirm: true, request_id: 'request-one' })).rejects.toThrow();
    expect(post).toHaveBeenCalledTimes(1);
  });
});
