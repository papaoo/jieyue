import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import LoginPage from '../LoginPage';
import { createParsedApiError } from '../../api/error';
import { BRAND } from '../../config/brand';
import { MotionPreferenceProvider } from '../../contexts/MotionPreferenceContext';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import { ThemeProvider } from '../../components/theme/ThemeProvider';
import { UI_LANGUAGE_STORAGE_KEY } from '../../utils/uiLanguage';

const { navigate, useSearchParamsMock, useAuthMock } = vi.hoisted(() => ({
  navigate: vi.fn(),
  useSearchParamsMock: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../../hooks', () => ({
  useAuth: () => useAuthMock(),
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigate,
    useSearchParams: () => useSearchParamsMock(),
  };
});

describe('LoginPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
    document.documentElement.className = 'light';
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })));
    // Material fallback tests never need a real browser/GPU context.
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    useSearchParamsMock.mockReturnValue([new URLSearchParams('redirect=%2Fsettings')]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('blocks first-time setup when confirmation does not match', async () => {
    const login = vi.fn();
    useAuthMock.mockReturnValue({
      login,
      passwordSet: false,
      setupState: 'no_password',
    });

    render(<LoginPage />);

    fireEvent.change(screen.getByLabelText('管理员密码'), { target: { value: 'passwd6' } });
    fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'passwd7' } });
    fireEvent.click(screen.getByRole('button', { name: '完成设置并登录' }));

    expect(await screen.findByText('两次输入的密码不一致')).toBeInTheDocument();
    expect(login).not.toHaveBeenCalled();
    expect(screen.getByLabelText('管理员密码')).toHaveAttribute('data-appearance', 'login');
    expect(screen.getByLabelText('确认密码')).toHaveAttribute('data-appearance', 'login');
  });

  it('navigates to redirect after a successful login', async () => {
    useAuthMock.mockReturnValue({
      login: vi.fn().mockResolvedValue({ success: true }),
      passwordSet: true,
      setupState: 'enabled',
    });

    render(<LoginPage />);

    fireEvent.change(screen.getByLabelText('登录密码'), { target: { value: 'passwd6' } });
    fireEvent.click(screen.getByRole('button', { name: '进入工作台' }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/settings', { replace: true }));
    expect(screen.getByLabelText('登录密码')).toHaveAttribute('data-appearance', 'login');
  });

  it('does not override login theme tokens inline so light mode can take effect', () => {
    useAuthMock.mockReturnValue({
      login: vi.fn(),
      passwordSet: true,
      setupState: 'enabled',
    });

    const { container } = render(<LoginPage />);
    const pageRoot = container.firstElementChild as HTMLElement | null;

    expect(pageRoot).not.toBeNull();
    expect(pageRoot?.getAttribute('style') ?? '').not.toContain('--login-bg-main');
  });

  it('shows the working brand and honest HTTP connection notice', () => {
    useAuthMock.mockReturnValue({ login: vi.fn(), passwordSet: true, setupState: 'enabled' });

    render(<LoginPage />);

    expect(screen.getByRole('heading', { name: BRAND.name })).toBeInTheDocument();
    expect(document.title).toBe(`登录 - ${BRAND.name}`);
    expect(screen.getByText(/HTTP 未加密/)).toBeInTheDocument();
    expect(screen.queryByText(/DSA-V3-TLS/)).not.toBeInTheDocument();
  });

  it('turns decorative motion off without changing password input', () => {
    useAuthMock.mockReturnValue({ login: vi.fn(), passwordSet: true, setupState: 'enabled' });
    localStorage.removeItem('guanlan.motion');
    const { container } = render(<MotionPreferenceProvider><LoginPage /></MotionPreferenceProvider>);

    fireEvent.change(screen.getByLabelText('登录密码'), { target: { value: 'passwd6' } });
    fireEvent.click(screen.getByRole('button', { name: '切换动态效果' }));

    expect(container.querySelector('.jieyue-login')).toHaveAttribute('data-login-motion', 'off');
    expect(screen.getByLabelText('登录密码')).toHaveValue('passwd6');
    localStorage.removeItem('guanlan.motion');
  });

  it('uses the Jieyue identity and a decorative market study without claiming a live data feed', async () => {
    useAuthMock.mockReturnValue({ login: vi.fn(), passwordSet: true, setupState: 'enabled' });

    render(<LoginPage />);

    expect(BRAND).toMatchObject({ name: '阶跃', englishName: 'JIEYUE', shortName: 'JY' });
    expect(screen.getByRole('main')).toHaveClass('jieyue-login');
    expect(screen.getByRole('main')).toHaveAttribute('data-login-motion', 'on');
    expect(screen.getByRole('heading', { name: '阶跃', level: 1 })).toBeInTheDocument();
    expect(screen.getByTestId('stock-market-visual').querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    await waitFor(() => expect(screen.getByText('市场演示 · 非实时')).toBeVisible());
    expect(screen.getByText('研究辅助，不构成投资建议。')).toBeInTheDocument();
  });

  it('passes matching first-time passwords and keeps setup-specific autocomplete', async () => {
    const login = vi.fn().mockResolvedValue({ success: true });
    useAuthMock.mockReturnValue({ login, passwordSet: false, setupState: 'no_password' });

    render(<LoginPage />);

    const password = screen.getByLabelText('管理员密码');
    const confirmation = screen.getByLabelText('确认密码');
    expect(password).toHaveAttribute('autocomplete', 'new-password');
    expect(confirmation).toHaveAttribute('autocomplete', 'new-password');
    fireEvent.change(password, { target: { value: 'setup-password' } });
    fireEvent.change(confirmation, { target: { value: 'setup-password' } });
    fireEvent.click(screen.getByRole('button', { name: '完成设置并登录' }));

    await waitFor(() => expect(login).toHaveBeenCalledWith('setup-password', 'setup-password'));
    expect(navigate).toHaveBeenCalledWith('/settings', { replace: true });
  });

  it('uses an existing password without sending a setup confirmation', async () => {
    const login = vi.fn().mockResolvedValue({ success: true });
    useAuthMock.mockReturnValue({ login, passwordSet: true, setupState: 'password_retained' });

    render(<LoginPage />);

    expect(screen.queryByLabelText('确认密码')).not.toBeInTheDocument();
    expect(screen.getByLabelText('登录密码')).toHaveAttribute('autocomplete', 'current-password');
    fireEvent.change(screen.getByLabelText('登录密码'), { target: { value: 'retained-password' } });
    fireEvent.click(screen.getByRole('button', { name: '进入工作台' }));

    await waitFor(() => expect(login).toHaveBeenCalledWith('retained-password', undefined));
  });

  it('renders a failed result as an accessible error, never as a completed login', async () => {
    const error = createParsedApiError({
      title: '验证失败', message: '管理员密码不正确', rawMessage: 'incorrect password',
      status: 401, category: 'http_error',
    });
    useAuthMock.mockReturnValue({
      login: vi.fn().mockResolvedValue({ success: false, error }),
      passwordSet: true,
      setupState: 'enabled',
    });

    render(<LoginPage />);

    fireEvent.change(screen.getByLabelText('登录密码'), { target: { value: 'wrong-password' } });
    fireEvent.click(screen.getByRole('button', { name: '进入工作台' }));

    expect((await screen.findByText('管理员密码不正确')).closest('[role="alert"]')).not.toBeNull();
    expect(navigate).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '进入工作台' })).toBeEnabled();
    expect(screen.queryByText(/登录成功|已完成|Login successful|Completed/i)).not.toBeInTheDocument();
  });

  it('shows a fallback error when a failed result has no detailed error', async () => {
    useAuthMock.mockReturnValue({
      login: vi.fn().mockResolvedValue({ success: false }),
      passwordSet: true,
      setupState: 'enabled',
    });

    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText('登录密码'), { target: { value: 'wrong-password' } });
    fireEvent.click(screen.getByRole('button', { name: '进入工作台' }));

    expect((await screen.findByText('登录失败')).closest('[role="alert"]')).not.toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });

  it.each([false, true])('disables credential inputs and duplicate clicks while waiting (setup=%s)', async (setup) => {
    let resolveLogin!: (result: { success: boolean }) => void;
    const login = vi.fn(() => new Promise<{ success: boolean }>((resolve) => { resolveLogin = resolve; }));
    useAuthMock.mockReturnValue({ login, passwordSet: !setup, setupState: setup ? 'no_password' : 'enabled' });

    const { container } = render(<LoginPage />);
    const password = screen.getByLabelText(setup ? '管理员密码' : '登录密码');
    fireEvent.change(password, { target: { value: 'pending-password' } });
    if (setup) {
      fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'pending-password' } });
    }
    fireEvent.click(screen.getByRole('button', { name: setup ? '完成设置并登录' : '进入工作台' }));

    const submit = screen.getByRole('button', { name: setup ? '初始化中...' : '正在建立连接...' });
    expect(submit).toBeDisabled();
    expect(submit).toHaveAttribute('aria-busy', 'true');
    expect(password).toBeDisabled();
    if (setup) expect(screen.getByLabelText('确认密码')).toBeDisabled();
    expect(container.querySelector('form')).toHaveAttribute('aria-busy', 'true');
    fireEvent.click(submit);
    fireEvent.submit(container.querySelector('form') as HTMLFormElement);
    expect(login).toHaveBeenCalledTimes(1);
    expect(navigate).not.toHaveBeenCalled();

    await act(async () => { resolveLogin({ success: false }); });
    expect(screen.getByRole('button', { name: setup ? '完成设置并登录' : '进入工作台' })).toBeEnabled();
    expect(password).toBeEnabled();
    expect(screen.getByText('登录失败').closest('[role="alert"]')).not.toBeNull();
  });

  it('preserves the complete internal deep link after successful login', async () => {
    const destination = '/research?asset=CN-ETF-510300#notes';
    useSearchParamsMock.mockReturnValue([new URLSearchParams({ redirect: destination })]);
    useAuthMock.mockReturnValue({ login: vi.fn().mockResolvedValue({ success: true }), passwordSet: true, setupState: 'enabled' });

    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText('登录密码'), { target: { value: 'valid-password' } });
    fireEvent.click(screen.getByRole('button', { name: '进入工作台' }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith(destination, { replace: true }));
  });

  it.each([
    'https://external.example/path', '//external.example/path', '/%5cexternal.example/path',
    '/%2fexternal.example/path', '/login?redirect=%2Fresearch', '/LOGIN/',
  ])('rejects unsafe or looping redirect %s at the actual login submission', async (redirect) => {
    useSearchParamsMock.mockReturnValue([new URLSearchParams({ redirect })]);
    useAuthMock.mockReturnValue({ login: vi.fn().mockResolvedValue({ success: true }), passwordSet: true, setupState: 'enabled' });

    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText('登录密码'), { target: { value: 'valid-password' } });
    fireEvent.click(screen.getByRole('button', { name: '进入工作台' }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/', { replace: true }));
  });

  it.each(['light', 'dark'])('keeps the form usable in the %s theme without inline theme overrides', async (theme) => {
    localStorage.setItem('theme', theme);
    useAuthMock.mockReturnValue({ login: vi.fn(), passwordSet: true, setupState: 'enabled' });

    render(<ThemeProvider><LoginPage /></ThemeProvider>);

    await waitFor(() => expect(document.documentElement).toHaveClass(theme));
    expect(screen.getByRole('main')).toHaveClass('jieyue-login');
    expect(screen.getByRole('main').style.getPropertyValue('--login-bg-main')).toBe('');
    fireEvent.change(screen.getByLabelText('登录密码'), { target: { value: 'theme-password' } });
    expect(screen.getByLabelText('登录密码')).toHaveValue('theme-password');
  });

  it('switches both interface languages without discarding credentials or redirect', async () => {
    const login = vi.fn().mockResolvedValue({ success: true });
    useAuthMock.mockReturnValue({ login, passwordSet: true, setupState: 'enabled' });

    render(<UiLanguageProvider><LoginPage /></UiLanguageProvider>);
    fireEvent.change(screen.getByLabelText('登录密码'), { target: { value: 'bilingual-password' } });
    fireEvent.click(screen.getByRole('button', { name: '切换界面语言' }));

    expect(screen.getByLabelText('Password')).toHaveValue('bilingual-password');
    expect(document.documentElement).toHaveAttribute('lang', 'en');
    expect(document.title).toBe('Login - JIEYUE');
    await waitFor(() => expect(screen.getByText('Market study · not live')).toBeVisible());
    expect(screen.getByText(/Unencrypted HTTP/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Switch UI language' }));
    expect(screen.getByLabelText('登录密码')).toHaveValue('bilingual-password');
    expect(document.title).toBe('登录 - 阶跃');
    await waitFor(() => expect(screen.getByText('市场演示 · 非实时')).toBeVisible());
    fireEvent.click(screen.getByRole('button', { name: '进入工作台' }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/settings', { replace: true }));
  });

  it('respects system reduced motion and keeps a static market study even with a saved on preference', () => {
    localStorage.setItem('guanlan.motion', 'on');
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)', media: query,
      addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(),
    })));
    useAuthMock.mockReturnValue({ login: vi.fn(), passwordSet: true, setupState: 'enabled' });

    render(<MotionPreferenceProvider><LoginPage /></MotionPreferenceProvider>);

    expect(screen.getByRole('main')).toHaveAttribute('data-login-motion', 'off');
    expect(screen.getByRole('button', { name: '切换动态效果' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByTestId('stock-market-visual')).toHaveAttribute('data-ambient', 'off');
    expect(screen.getByTestId('stock-market-visual').querySelector('svg')).toBeInTheDocument();
    expect(screen.getByTestId('stock-market-visual').querySelector('canvas')).not.toBeInTheDocument();
    expect(screen.getByLabelText('登录密码')).toHaveAttribute('type', 'password');
  });
});
