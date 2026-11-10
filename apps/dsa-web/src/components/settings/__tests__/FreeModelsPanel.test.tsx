import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ButtonHTMLAttributes } from 'react';
import { FreeModelsPanel } from '../FreeModelsPanel';

const api = vi.hoisted(() => ({ status: vi.fn(), refresh: vi.fn(), test: vi.fn(), update: vi.fn() }));
vi.mock('../../../api/freeModels', () => ({ freeModelsApi: api }));
vi.mock('../../../api/systemConfig', () => ({ systemConfigApi: api }));
vi.mock('../../common', () => ({ Button: ({ variant, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: string }) => <button data-variant={variant} {...props} /> }));

const initial = {
  enabled: false, model: 'deepseek-v4.1-flash-free', daily_limit: 100, used_today: 0,
  catalog: { checked_at: 1, models: [
    { id: 'deepseek-v4.1-flash-free', name: 'DeepSeek V4.1 Flash', input_price: 0, output_price: 0 },
    { id: 'mimo-v2.6-flash-free', name: 'MiMo V2.6 Flash', input_price: 0, output_price: 0 },
  ] }, recent_calls: [],
};
function mount(disabled = false) {
  const onSaved = vi.fn().mockResolvedValue(undefined);
  render(<FreeModelsPanel configVersion="v1" disabled={disabled} onSavingChange={vi.fn()} onSaved={onSaved} />);
  return onSaved;
}

describe('FreeModelsPanel', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.status.mockResolvedValue(initial);
    api.refresh.mockResolvedValue(initial.catalog);
    api.update.mockResolvedValue({});
  });
  it('shows a separate free entry and keeps selection server-backed', async () => {
    const saved = mount();
    const control = await screen.findByRole('combobox', { name: '默认免费模型' });
    await waitFor(() => expect(control).toHaveValue(initial.model));
    fireEvent.change(control, { target: { value: 'mimo-v2.6-flash-free' } });
    await waitFor(() => expect(api.update).toHaveBeenCalledWith({ configVersion: 'v1', reloadNow: true,
      items: [{ key: 'FREE_MODEL_NAME', value: 'mimo-v2.6-flash-free' }] }));
    expect(saved).toHaveBeenCalledWith(['FREE_MODEL_NAME']);
  });
  it('enables through existing configuration persistence without changing paid keys', async () => {
    mount();
    const button = await screen.findByRole('button', { name: '启用并设为默认' });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(api.update).toHaveBeenCalledWith(expect.objectContaining({
      items: [{ key: 'FREE_MODEL_ENABLED', value: 'true' }],
    })));
    expect(api.refresh).toHaveBeenCalledOnce();
  });
  it('tests selected model and shows actual reply without activating channel', async () => {
    api.test.mockResolvedValue({ success: true, reply: 'OK', usage: { total_tokens: 9 }, status: initial });
    mount();
    const button = await screen.findByRole('button', { name: '测试连接' });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole('status')).toHaveTextContent('连接成功 · OK · 9 tokens');
    expect(api.test).toHaveBeenCalledWith(initial.model);
    expect(api.update).not.toHaveBeenCalled();
  });
  it('displays failure reason without switching models', async () => {
    api.test.mockResolvedValue({ success: false, message: '该模型在当前服务器地区不可用。', status: initial });
    mount();
    const button = await screen.findByRole('button', { name: '测试连接' });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent('地区不可用');
    expect(api.update).not.toHaveBeenCalled();
  });
});
