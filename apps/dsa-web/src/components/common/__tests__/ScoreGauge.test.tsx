import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ScoreGauge } from '../ScoreGauge';

vi.mock('../../../contexts/MotionPreferenceContext', () => ({
  useMotionPreference: () => ({ motionEnabled: false, toggleMotion: () => undefined }),
}));
vi.mock('next-themes', () => ({ useTheme: () => ({ resolvedTheme: 'dark' }) }));

describe('ScoreGauge with motion disabled', () => {
  it('shows the actual score immediately without scheduling an animation loop', () => {
    const frame = vi.spyOn(window, 'requestAnimationFrame');
    const { rerender } = render(<ScoreGauge score={72} />);
    expect(screen.getByText('72')).toBeVisible();
    expect(frame).not.toHaveBeenCalled();
    rerender(<ScoreGauge score={38} />);
    expect(screen.getByText('38')).toBeVisible();
    expect(frame).not.toHaveBeenCalled();
    frame.mockRestore();
  });
});
