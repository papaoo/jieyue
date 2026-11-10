import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SetupEditor } from '../research-delivery/SetupEditor';
import { SubscriptionEditor } from '../research-delivery/SubscriptionEditor';
import type { DeliveryCategory, DeliveryDestination } from '../../../types/researchDelivery';

const api = vi.hoisted(() => ({ setup: vi.fn(), setupStatus: vi.fn(), preview: vi.fn(), test: vi.fn(), runAutomation: vi.fn() }));
vi.mock('../../../api/researchDelivery', () => ({ researchDeliveryApi: api }));
const group = { id: 'g1', alias: '已有群', version: 1, credential_configured: true } as DeliveryDestination;

describe.each(['setup', 'subscription'] as const)('%s model settings', editor => {
  beforeEach(() => { vi.resetAllMocks(); api.setup.mockResolvedValue({ request_id: 'saved' }); });
  function mount(producer: DeliveryCategory['producer_status'], llmAvailable: boolean, preview = false, categoryId = 'market_radar') {
    const category: DeliveryCategory = { category_id: categoryId, code: categoryId === 'system_status' ? 'O01' : 'R01', display_name: '研究', description: '',
      destination_kind: 'radar', planned_time: '17:10', preview_available: preview, producer_status: producer };
    const callbacks = { onDirtyChange: vi.fn(), onSave: vi.fn(), onToggle: vi.fn(), onCancel: vi.fn(),
      onSavingChange: vi.fn(), onSaved: vi.fn(), onClose: vi.fn() };
    if (editor === 'setup') render(<SetupEditor categories={[category]} initialCategoryId={category.category_id} groups={[group]} disabled={false} llmAvailable={llmAvailable} {...callbacks} />);
    else render(<SubscriptionEditor category={category} groups={[group]} disabled={false} llmAvailable={llmAvailable} {...callbacks} />);
    return callbacks;
  }
  it.each(['etf_rotation', 'market_radar', 'announcement_risk'])('allows explanation mode changes for live %s without historical preview', categoryId => {
    const callbacks = mount('live_on_demand', true, false, categoryId);
    const control = screen.getByLabelText('大模型作用');
    expect(control).not.toBeDisabled();
    fireEvent.change(control, { target: { value: 'off' } });
    expect(control).toHaveValue('off');
    fireEvent.change(control, { target: { value: 'explain_only' } });
    expect(control).toHaveValue('explain_only');
    expect(callbacks.onSave).not.toHaveBeenCalled();
    expect(callbacks.onSaved).not.toHaveBeenCalled();
    expect(callbacks.onToggle).not.toHaveBeenCalled();
    expect(api.setup).not.toHaveBeenCalled();
  });
  it('keeps unsupported O01 controls disabled', () => {
    mount('not_connected', true, false, 'system_status');
    expect(screen.getByLabelText('大模型作用')).toBeDisabled();
  });
  it('respects global model capability even for live content', () => {
    mount('live_on_demand', false);
    expect(screen.getByLabelText('大模型作用')).toBeDisabled();
  });
  it('respects global model capability for historical preview too', () => {
    mount('historical_draft_only', false, true);
    expect(screen.getByLabelText('大模型作用')).toBeDisabled();
  });
  it('saves the chosen auxiliary mode only through the configuration seam', async () => {
    const callbacks = mount('live_on_demand', true, false, 'etf_rotation');
    fireEvent.change(screen.getByLabelText('大模型作用'), { target: { value: 'off' } });
    if (editor === 'setup') {
      fireEvent.click(screen.getByLabelText('使用已保存的群连接'));
      fireEvent.change(screen.getByLabelText('已有群连接'), { target: { value: 'g1' } });
      fireEvent.click(screen.getByRole('button', { name: '保存群订阅' }));
      await waitFor(() => expect(api.setup).toHaveBeenCalledWith(expect.objectContaining({ strategy_id: 'etf_rotation', llm_mode: 'off' })));
    } else {
      fireEvent.change(screen.getByLabelText('目标群'), { target: { value: 'g1' } });
      fireEvent.click(screen.getByRole('button', { name: '保存该类订阅' }));
      expect(callbacks.onSave).toHaveBeenCalledWith(expect.objectContaining({ strategy_id: 'etf_rotation', llm_mode: 'off' }));
    }
    expect(api.preview).not.toHaveBeenCalled();
    expect(api.test).not.toHaveBeenCalled();
    expect(api.runAutomation).not.toHaveBeenCalled();
  });
});
