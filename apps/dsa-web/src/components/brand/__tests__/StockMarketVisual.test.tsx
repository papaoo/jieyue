import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StockMarketVisual } from '../StockMarketVisual';

describe('decorative stock market visual', () => {
  const caption = '市场结构 · 动态演示';
  const observe = vi.fn();
  const disconnect = vi.fn();
  const initialIntersectionObserver = globalThis.IntersectionObserver;
  let visibility: ReturnType<typeof vi.spyOn>;
  let intersectionChanged: (isIntersecting: boolean) => void;

  beforeEach(() => {
    vi.clearAllMocks();
    visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => {
      throw new Error('An SVG market study must not request Canvas/GPU access');
    });
    // setupTests defines a writable but non-configurable property; replace its value only.
    globalThis.IntersectionObserver = class ObserverMock implements IntersectionObserver {
      readonly root = null;
      readonly rootMargin = '0px';
      readonly thresholds = [0.05];
      observe = observe;
      disconnect = disconnect;
      unobserve = vi.fn();
      takeRecords = () => [];
      constructor(callback: IntersectionObserverCallback) {
        intersectionChanged = (isIntersecting) => {
          callback([{ isIntersecting } as IntersectionObserverEntry], this);
        };
      }
    };
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    globalThis.IntersectionObserver = initialIntersectionObserver;
  });

  function changeIntersection(isIntersecting: boolean) {
    act(() => { intersectionChanged(isIntersecting); });
  }

  it('retains K-lines, volumes and trajectories as an SVG when motion is disabled', () => {
    render(<StockMarketVisual motionEnabled={false} caption={caption} />);

    const visual = screen.getByTestId('stock-market-visual');
    expect(visual).toHaveAttribute('data-ambient', 'off');
    expect(visual.querySelector('svg')).toHaveAttribute('viewBox', '0 0 800 420');
    expect(visual.querySelector('.stock-scene__candle rect')).toBeInTheDocument();
    expect(visual.querySelectorAll('.stock-scene__candle rect').length).toBeGreaterThan(1);
    expect(visual.querySelector('.stock-scene__price-trace')).toBeInTheDocument();
    expect(visual.querySelector('canvas')).not.toBeInTheDocument();
    expect(HTMLCanvasElement.prototype.getContext).not.toHaveBeenCalled();
  });

  it('switches the ambient state with motion preferences without removing the market structure', () => {
    const view = render(<StockMarketVisual motionEnabled caption={caption} />);
    const visual = screen.getByTestId('stock-market-visual');
    expect(visual).toHaveAttribute('data-ambient', 'on');

    view.rerender(<StockMarketVisual motionEnabled={false} caption={caption} />);
    expect(visual).toHaveAttribute('data-ambient', 'off');
    expect(visual.querySelector('svg')).toBeInTheDocument();
    expect(screen.getByText(caption)).toBeVisible();

    view.rerender(<StockMarketVisual motionEnabled caption={caption} />);
    expect(visual).toHaveAttribute('data-ambient', 'on');
    expect(visual.querySelector('canvas')).not.toBeInTheDocument();
  });

  it.each([caption, 'Market structure · motion study'])('hides decorative artwork from assistive technology but exposes caption %s', (text) => {
    render(<StockMarketVisual motionEnabled caption={text} />);

    const visual = screen.getByTestId('stock-market-visual');
    expect(visual.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    expect(visual).not.toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText(text)).toBeVisible();
    expect(screen.getByText(text).closest('[aria-hidden="true"]')).toBeNull();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('pauses while the document is hidden and resumes only when visible', () => {
    render(<StockMarketVisual motionEnabled caption={caption} />);
    const visual = screen.getByTestId('stock-market-visual');

    visibility.mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    expect(visual).toHaveAttribute('data-ambient', 'off');
    expect(visual.querySelector('svg')).toBeInTheDocument();

    visibility.mockReturnValue('visible');
    fireEvent(document, new Event('visibilitychange'));
    expect(visual).toHaveAttribute('data-ambient', 'on');
  });

  it('starts paused when the document is already hidden', () => {
    visibility.mockReturnValue('hidden');
    render(<StockMarketVisual motionEnabled caption={caption} />);

    expect(screen.getByTestId('stock-market-visual')).toHaveAttribute('data-ambient', 'off');
    expect(screen.getByText(caption)).toBeVisible();
  });

  it('pauses outside the viewport and resumes after it intersects again', () => {
    render(<StockMarketVisual motionEnabled caption={caption} />);
    const visual = screen.getByTestId('stock-market-visual');
    expect(observe).toHaveBeenCalledWith(visual);

    changeIntersection(false);
    expect(visual).toHaveAttribute('data-ambient', 'off');
    expect(visual.querySelector('svg')).toBeInTheDocument();
    changeIntersection(true);
    expect(visual).toHaveAttribute('data-ambient', 'on');
  });

  it('never resumes after visibility or viewport events when the user has disabled motion', () => {
    render(<StockMarketVisual motionEnabled={false} caption={caption} />);

    visibility.mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    changeIntersection(false);
    visibility.mockReturnValue('visible');
    fireEvent(document, new Event('visibilitychange'));
    changeIntersection(true);

    expect(screen.getByTestId('stock-market-visual')).toHaveAttribute('data-ambient', 'off');
    expect(screen.getByTestId('stock-market-visual').querySelector('svg')).toBeInTheDocument();
  });

  it('removes the exact visibility listener and disconnects its observer when unmounted', () => {
    const added = vi.spyOn(document, 'addEventListener');
    const removed = vi.spyOn(document, 'removeEventListener');
    const view = render(<StockMarketVisual motionEnabled caption={caption} />);
    const registration = added.mock.calls.find(([event]) => event === 'visibilitychange');
    expect(registration).toBeDefined();

    view.unmount();

    expect(removed).toHaveBeenCalledWith('visibilitychange', registration?.[1]);
    expect(disconnect).toHaveBeenCalledTimes(1);
  });

  it('remains usable without IntersectionObserver support', () => {
    Reflect.set(globalThis, 'IntersectionObserver', undefined);
    const view = render(<StockMarketVisual motionEnabled caption={caption} />);

    expect(screen.getByTestId('stock-market-visual')).toHaveAttribute('data-ambient', 'on');
    expect(screen.getByText(caption)).toBeVisible();
    expect(() => view.unmount()).not.toThrow();
  });
});
