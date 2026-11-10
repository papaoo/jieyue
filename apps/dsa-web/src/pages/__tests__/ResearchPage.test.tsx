import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DataCapabilityOverview } from '../../types/dataCapability';
import ResearchPage from '../ResearchPage';

const { getOverview, language, motionPreference } = vi.hoisted(() => ({
  getOverview: vi.fn(), language: { value: 'zh' }, motionPreference: { enabled: true },
}));
vi.mock('../../api/dataCapability', () => ({ dataCapabilityApi: { getOverview: () => getOverview() } }));
vi.mock('../../contexts/UiLanguageContext', () => ({ useUiLanguage: () => ({ language: language.value }) }));
vi.mock('../../contexts/MotionPreferenceContext', () => ({ useMotionPreference: () => ({ motionEnabled: motionPreference.enabled }) }));

const fixture: DataCapabilityOverview = {
  asOf: '2026-10-09T09:00:00Z', providers: [{
    name: 'akshare', label: 'AkShare', enabled: true, configured: true, status: 'partial',
    markets: ['cn'], datasets: ['kline.daily'], datasetMarkets: { 'kline.daily': ['cn'] },
    warnings: ['runtime_probe_unknown'],
  }], datasets: [{
    dataset: 'kline.daily', status: 'unknown', source: 'akshare', stale: null, lastSuccess: null,
    fallbackFrom: [], warnings: ['sample_not_verified'], coverage: null,
  }], priorities: [], warnings: [],
};
function mount(initialEntries = ['/research']) {
  const router = createMemoryRouter([
    { path: '/research', element: <ResearchPage /> },
    { path: '*', element: <div data-testid="destination-page">Destination</div> },
  ], { initialEntries, initialIndex: initialEntries.length - 1 });
  return { ...render(<RouterProvider router={router} />), router };
}

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); language.value = 'zh'; motionPreference.enabled = true;
  getOverview.mockResolvedValue(fixture);
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('ResearchPage', () => {
  it('loads only the overview and displays partial evidence without invented health', async () => {
    mount();
    expect(await screen.findByText('AkShare')).toBeInTheDocument();
    expect(getOverview).toHaveBeenCalledTimes(1);
    expect(screen.getByText('runtime_probe_unknown')).toBeInTheDocument();
    expect(screen.getByText('sample_not_verified')).toBeInTheDocument();
    expect(screen.getAllByText('未验证').length).toBeGreaterThan(0);
    expect(screen.getByRole('link', { name: /数据中心/ })).toHaveAttribute('href', '/data');
    fireEvent.click(screen.getByRole('button', { name: /BTC/ }));
    expect(screen.getByText('BTC 行情适配器尚未接入')).toBeInTheDocument();
    expect(screen.queryByText('AkShare')).not.toBeInTheDocument();
    expect(getOverview).toHaveBeenCalledTimes(1);
  });
  it('requires explicit save and preserves each asset unsaved draft', async () => {
    mount(); await screen.findByText('AkShare');
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'A-share thesis' } });
    expect(localStorage.getItem('guanlan.research-notebook.v1:cn')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /美股/ }));
    expect(screen.getByLabelText('研究假设')).toHaveValue('');
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'US thesis' } });
    fireEvent.click(screen.getByRole('button', { name: '保存到本机' }));
    const saved = JSON.parse(localStorage.getItem('guanlan.research-notebook.v1:us')!);
    expect(saved).toMatchObject({ version: 1, asset: 'us', fields: { thesis: 'US thesis' } });
    fireEvent.click(screen.getByRole('button', { name: /A股/ }));
    expect(screen.getByLabelText('研究假设')).toHaveValue('A-share thesis');
    expect(localStorage.getItem('guanlan.research-notebook.v1:cn')).toBeNull();
  });
  it('guards workspace links, retains drafts on cancel and never auto-saves on discard', async () => {
    const { router } = mount(); await screen.findByText('AkShare');
    const write = vi.spyOn(Object.getPrototypeOf(localStorage), 'setItem');
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'Sensitive draft' } });
    fireEvent.click(screen.getByRole('link', { name: /数据中心/ }));
    expect(await screen.findByText('有未保存的研究草稿')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/research');
    fireEvent.click(screen.getByRole('button', { name: '留在研究页' }));
    expect(screen.queryByText('有未保存的研究草稿')).not.toBeInTheDocument();
    expect(screen.getByLabelText('研究假设')).toHaveValue('Sensitive draft');
    fireEvent.click(screen.getByRole('link', { name: /数据中心/ }));
    fireEvent.click(await screen.findByRole('button', { name: '放弃草稿并离开' }));
    expect(await screen.findByTestId('destination-page')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/data');
    expect(write).not.toHaveBeenCalled();
  });
  it('guards inactive-asset drafts until every edited asset is explicitly saved', async () => {
    const { router } = mount(); await screen.findByText('AkShare');
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'Unsaved A-share thesis' } });
    fireEvent.click(screen.getByRole('button', { name: /美股/ }));
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'Saved US thesis' } });
    fireEvent.click(screen.getByRole('button', { name: '保存到本机' }));
    fireEvent.click(screen.getByRole('link', { name: /持仓管理/ }));
    expect(await screen.findByText('有未保存的研究草稿')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/research');
    expect(localStorage.getItem('guanlan.research-notebook.v1:cn')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '留在研究页' }));
    fireEvent.click(screen.getByRole('button', { name: /A股/ }));
    expect(screen.getByLabelText('研究假设')).toHaveValue('Unsaved A-share thesis');
    fireEvent.click(screen.getByRole('button', { name: '保存到本机' }));
    fireEvent.click(screen.getByRole('link', { name: /持仓管理/ }));
    expect(await screen.findByTestId('destination-page')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/portfolio');
    expect(screen.queryByText('有未保存的研究草稿')).not.toBeInTheDocument();
  });
  it('guards browser-history departure and allows explicit discard after a canceled back action', async () => {
    const { router } = mount(['/data', '/research']); await screen.findByText('AkShare');
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'Back-protected draft' } });
    await act(async () => { await router.navigate(-1); });
    expect(await screen.findByText('有未保存的研究草稿')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/research');
    fireEvent.click(screen.getByRole('button', { name: '留在研究页' }));
    expect(screen.getByLabelText('研究假设')).toHaveValue('Back-protected draft');
    await act(async () => { await router.navigate(-1); });
    fireEvent.click(await screen.findByRole('button', { name: '放弃草稿并离开' }));
    expect(await screen.findByTestId('destination-page')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/data');
    expect(localStorage.getItem('guanlan.research-notebook.v1:cn')).toBeNull();
  });
  it('protects refresh/close for inactive drafts and removes the handler once all drafts are saved', async () => {
    const { unmount } = mount(); await screen.findByText('AkShare');
    const clean = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'Refresh-protected draft' } });
    fireEvent.click(screen.getByRole('button', { name: /BTC/ }));
    const dirty = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
    expect(localStorage.getItem('guanlan.research-notebook.v1:cn')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /A股/ }));
    fireEvent.click(screen.getByRole('button', { name: '保存到本机' }));
    const saved = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(saved);
    expect(saved.defaultPrevented).toBe(false);
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'Unsaved again' } });
    unmount();
    const departed = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(departed);
    expect(departed.defaultPrevented).toBe(false);
  });
  it('blocks overwriting malformed stored data and permits exporting the current draft', async () => {
    localStorage.setItem('guanlan.research-notebook.v1:cn', JSON.stringify({ version: 99, asset: 'us', fields: {} }));
    mount(); await screen.findByText('AkShare');
    expect(screen.getByText(/本地记录格式异常/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存到本机' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '导出 JSON' })).not.toBeDisabled();
    expect(JSON.parse(localStorage.getItem('guanlan.research-notebook.v1:cn')!).version).toBe(99);
  });
  it('recovers from storage read errors without losing an edited draft', async () => {
    const read = vi.spyOn(Object.getPrototypeOf(localStorage), 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    mount(); await screen.findByText('AkShare');
    expect(screen.getByRole('button', { name: '保存到本机' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'Retained draft' } });
    read.mockRestore();
    fireEvent.click(screen.getByRole('button', { name: '重新读取存储' }));
    expect(screen.getByLabelText('研究假设')).toHaveValue('Retained draft');
    expect(screen.getByRole('button', { name: '保存到本机' })).not.toBeDisabled();
  });
  it('reports write failures and never claims the draft was saved', async () => {
    mount(); await screen.findByText('AkShare');
    vi.spyOn(Object.getPrototypeOf(localStorage), 'setItem').mockImplementation(() => { throw new Error('quota'); });
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'unsaved' } });
    fireEvent.click(screen.getByRole('button', { name: '保存到本机' }));
    expect(screen.getByText(/保存失败/)).toBeInTheDocument();
    expect(screen.queryByText('已保存到当前浏览器')).not.toBeInTheDocument();
  });
  it('clears a failed refresh instead of displaying the previous snapshot as current', async () => {
    mount(); await screen.findByText('AkShare');
    getOverview.mockRejectedValueOnce(new Error('offline'));
    fireEvent.click(screen.getByRole('button', { name: '刷新来源概览' }));
    expect(await screen.findByText('来源概览暂不可用')).toBeInTheDocument();
    expect(screen.queryByText('AkShare')).not.toBeInTheDocument();
    getOverview.mockResolvedValue(fixture);
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByText('AkShare')).toBeInTheDocument();
  });
  it('renders English, respects disabled motion and keeps navigation local', async () => {
    language.value = 'en'; motionPreference.enabled = false;
    const { container } = mount();
    await screen.findByText('AkShare');
    expect(screen.getByRole('heading', { name: 'Cross-asset research' })).toBeInTheDocument();
    expect(container.querySelector('.research-page')).toHaveAttribute('data-motion', 'off');
    expect(screen.getByRole('link', { name: /Backtest/ })).toHaveAttribute('href', '/backtest');
    expect(screen.getByRole('link', { name: /Portfolio/ })).toHaveAttribute('href', '/portfolio');
    await waitFor(() => expect(getOverview).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText('Research thesis'), { target: { value: 'Unsaved English draft' } });
    fireEvent.click(screen.getByRole('link', { name: /Backtest/ }));
    expect(await screen.findByText('Unsaved research drafts')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stay in research' })).toBeInTheDocument();
  });
  it('keeps the textbox accessible name stable after saving and switching assets', async () => {
    mount(); await screen.findByText('AkShare');
    fireEvent.change(screen.getByRole('textbox', { name: '研究假设' }), { target: { value: 'Saved thesis' } });
    fireEvent.click(screen.getByRole('button', { name: '保存到本机' }));
    fireEvent.click(screen.getByRole('button', { name: /BTC/ }));
    fireEvent.click(screen.getByRole('button', { name: /A股/ }));
    expect(screen.getByRole('textbox', { name: '研究假设' })).toHaveValue('Saved thesis');
  });
  it('exports the current unsaved draft with version, asset, date and structured fields', async () => {
    let captured: Blob | null = null;
    const OriginalURL = URL;
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', class extends OriginalURL {
      static createObjectURL(blob: Blob) { captured = blob; return 'blob:research-export'; }
      static revokeObjectURL = revokeObjectURL;
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    mount(); await screen.findByText('AkShare');
    fireEvent.change(screen.getByLabelText('研究假设'), { target: { value: 'Exportable draft' } });
    fireEvent.click(screen.getByRole('button', { name: '导出 JSON' }));
    expect(click).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem('guanlan.research-notebook.v1:cn')).toBeNull();
    const text = await new Promise<string>((resolve) => {
      const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(captured!);
    });
    expect(JSON.parse(text)).toMatchObject({ version: 1, asset: 'cn', exportedAt: expect.any(String), savedAt: null, fields: { thesis: 'Exportable draft', evidence: '', invalidation: '', date: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/) } });
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:research-export');
  });
  it('rejects invalid dates before writing a record', async () => {
    mount(); await screen.findByText('AkShare');
    fireEvent.change(screen.getByLabelText('记录日期'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '保存到本机' }));
    expect(screen.getByText(/请填写有效的记录日期/)).toBeInTheDocument();
    expect(localStorage.getItem('guanlan.research-notebook.v1:cn')).toBeNull();
  });
  it.each([
    { asset: 'us', date: '2026-10-09', thesis: '' },
    { asset: 'cn', date: '2026-02-31', thesis: '' },
    { asset: 'cn', date: '2026-10-09', thesis: 'x'.repeat(4001) },
  ])('does not overwrite a version-one record with invalid asset/date/field size: %j', async (invalid) => {
    const stored = { version: 1, asset: invalid.asset, savedAt: '2026-10-09T09:00:00Z', fields: { thesis: invalid.thesis, evidence: '', invalidation: '', date: invalid.date } };
    localStorage.setItem('guanlan.research-notebook.v1:cn', JSON.stringify(stored));
    mount(); await screen.findByText('AkShare');
    expect(screen.getByRole('button', { name: '保存到本机' })).toBeDisabled();
    expect(localStorage.getItem('guanlan.research-notebook.v1:cn')).toBe(JSON.stringify(stored));
  });
});
