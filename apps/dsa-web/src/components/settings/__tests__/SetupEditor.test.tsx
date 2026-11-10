import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SetupEditor } from '../research-delivery/SetupEditor';
import type { DeliveryCategory, DeliveryDestination } from '../../../types/researchDelivery';
const api = vi.hoisted(() => ({ setup: vi.fn(), setupStatus: vi.fn() }));
vi.mock('../../../api/researchDelivery', () => ({ researchDeliveryApi: api }));
const category: DeliveryCategory = { category_id: 'volume_breakout', display_name: '放量突破', code: 'S01', destination_kind: 'strategy', preview_available: true, planned_time: '17:10', description: '筛选', producer_status: 'historical_draft_only' };
const group = { id: 'g1', alias: '已有群', version: 3, credential_configured: true } as DeliveryDestination;
const renderForm = (onSaved = vi.fn()) => render(<SetupEditor categories={[category]} groups={[group]} llmAvailable disabled={false} onDirtyChange={vi.fn()} onSavingChange={vi.fn()} onSaved={onSaved} onClose={vi.fn()} />);
function fillNew() {
  fireEvent.change(screen.getByLabelText('内容类别'), { target: { value: 'volume_breakout' } });
  fireEvent.change(screen.getByLabelText('飞书群名称'), { target: { value: '突破研究群' } });
  fireEvent.change(screen.getByLabelText('该群 Webhook'), { target: { value: 'https://open.feishu.cn/open-apis/bot/v2/hook/fake' } });
}
describe('one-save subscription setup', () => {
  beforeEach(() => vi.resetAllMocks());
  it('submits new connection and subscription in one request', async () => {
    const onSaved = vi.fn(); api.setup.mockResolvedValue({ request_id: 'saved' }); renderForm(onSaved); fillNew();
    fireEvent.click(screen.getByRole('button', { name: '保存群订阅' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(api.setup).toHaveBeenCalledTimes(1);
    expect(api.setup).toHaveBeenCalledWith(expect.objectContaining({ strategy_id: 'volume_breakout', new_destination: expect.objectContaining({ alias: '突破研究群' }) }));
  });
  it('retains the draft on a definitive validation failure', async () => {
    api.setup.mockRejectedValue({ response: { status: 422, data: { message: 'PRIVATE' } } }); renderForm(); fillNew();
    fireEvent.click(screen.getByRole('button', { name: '保存群订阅' }));
    await screen.findByRole('alert');
    expect(screen.getByLabelText('飞书群名称')).toHaveValue('突破研究群');
    expect(screen.getByLabelText('该群 Webhook')).not.toBeDisabled();
    expect(screen.queryByText(/PRIVATE/)).not.toBeInTheDocument();
  });
  it('freezes uncertain writes and recovers by read without repeating creation', async () => {
    const onSaved = vi.fn(); api.setup.mockRejectedValue(new Error('network')); api.setupStatus.mockResolvedValue({ request_id: 'same' }); renderForm(onSaved); fillNew();
    fireEvent.click(screen.getByRole('button', { name: '保存群订阅' }));
    await screen.findByRole('button', { name: '核对是否已保存' });
    expect(screen.getByLabelText('飞书群名称')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '核对是否已保存' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(api.setupStatus).toHaveBeenCalledWith(api.setup.mock.calls[0][0].request_id);
    expect(api.setup).toHaveBeenCalledTimes(1);
  });
  it('retries only the identical request ID and payload after a not-yet-found read', async () => {
    api.setup.mockRejectedValue(new Error('network')); api.setupStatus.mockRejectedValue({ response: { status: 404 } }); renderForm(); fillNew();
    fireEvent.click(screen.getByRole('button', { name: '保存群订阅' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对是否已保存' }));
    await waitFor(() => expect(api.setupStatus).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole('button', { name: '用原编号重试保存' }));
    await waitFor(() => expect(api.setup).toHaveBeenCalledTimes(2));
    expect(api.setup.mock.calls[0][0]).toEqual(api.setup.mock.calls[1][0]);
  });
  it('uses an existing configured target with its exact version', async () => {
    api.setup.mockResolvedValue({ request_id: 'saved' }); renderForm();
    fireEvent.change(screen.getByLabelText('内容类别'), { target: { value: 'volume_breakout' } });
    fireEvent.click(screen.getByLabelText('使用已保存的群连接'));
    fireEvent.change(screen.getByLabelText('已有群连接'), { target: { value: 'g1' } });
    fireEvent.click(screen.getByRole('button', { name: '保存群订阅' }));
    await waitFor(() => expect(api.setup).toHaveBeenCalledWith(expect.objectContaining({ destination_id: 'g1', destination_version: 3 })));
    expect(api.setup.mock.calls[0][0].new_destination).toBeUndefined();
  });
  it.each([400, 401, 403, 404, 409, 422])('preserves original uncertainty when an identical retry is rejected with %s', async status => {
    const onSaved = vi.fn();
    api.setup.mockRejectedValueOnce(new Error('lost response')).mockRejectedValueOnce({ response: { status } });
    api.setupStatus.mockRejectedValueOnce({ response: { status: 404 } });
    renderForm(onSaved); fillNew();
    fireEvent.click(screen.getByRole('button', { name: '保存群订阅' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对是否已保存' }));
    fireEvent.click(await screen.findByRole('button', { name: '用原编号重试保存' }));
    await waitFor(() => expect(api.setup).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('首次保存结果仍未确认'));
    expect(screen.getByRole('alert')).not.toHaveTextContent('未保存：');
    expect(screen.getByLabelText('飞书群名称')).toBeDisabled();
    const original = api.setup.mock.calls[0][0];
    expect(api.setup.mock.calls[1][0]).toEqual(original);
    expect(screen.getByText(new RegExp(original.request_id))).toBeInTheDocument();
    api.setupStatus.mockResolvedValueOnce({ request_id: original.request_id });
    fireEvent.click(screen.getByRole('button', { name: '核对是否已保存' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(api.setupStatus).toHaveBeenLastCalledWith(original.request_id);
    expect(api.setup).toHaveBeenCalledTimes(2);
  });
});
