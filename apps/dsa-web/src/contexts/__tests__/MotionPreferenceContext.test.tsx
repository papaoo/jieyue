import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MotionPreferenceProvider, useMotionPreference } from '../MotionPreferenceContext';

function Control() {
  const { motionEnabled, toggleMotion } = useMotionPreference();
  return <button onClick={toggleMotion} aria-pressed={motionEnabled}>motion</button>;
}

function renderControl() {
  return render(<MotionPreferenceProvider><Control /></MotionPreferenceProvider>);
}

describe('motion preference', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(),
    })));
  });
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it('retains a disabled preference across remounts and applies it to the whole document', () => {
    const first = renderControl();
    fireEvent.click(screen.getByRole('button', { name: 'motion' }));
    expect(document.documentElement.dataset.motion).toBe('off');
    first.unmount();
    renderControl();
    expect(screen.getByRole('button', { name: 'motion' })).toHaveAttribute('aria-pressed', 'false');
    expect(document.documentElement.dataset.motion).toBe('off');
  });

  it('honors operating-system reduced motion even when the local preference is enabled', () => {
    localStorage.setItem('guanlan.motion', 'on');
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(),
    })));
    renderControl();
    expect(screen.getByRole('button', { name: 'motion' })).toHaveAttribute('aria-pressed', 'false');
    expect(document.documentElement.dataset.motion).toBe('off');
  });

  it('keeps the session usable when browser storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    renderControl();
    fireEvent.click(screen.getByRole('button', { name: 'motion' }));
    expect(screen.getByRole('button', { name: 'motion' })).toHaveAttribute('aria-pressed', 'false');
  });
});
