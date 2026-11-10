import { createContext, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { MotionConfig } from 'motion/react';

const STORAGE_KEY = 'guanlan.motion';
const MEDIA_QUERY = '(prefers-reduced-motion: reduce)';
type MotionPreference = { motionEnabled: boolean; toggleMotion: () => void };
const MotionPreferenceContext = createContext<MotionPreference>({
  motionEnabled: true,
  toggleMotion: () => undefined,
});

function readPreference(): boolean {
  try { return localStorage.getItem(STORAGE_KEY) !== 'off'; }
  catch { return true; }
}

export function MotionPreferenceProvider({ children }: { children: ReactNode }) {
  const [preferred, setPreferred] = useState(readPreference);
  const [systemReduced, setSystemReduced] = useState(
    () => typeof window.matchMedia === 'function' && window.matchMedia(MEDIA_QUERY).matches,
  );
  const motionEnabled = preferred && !systemReduced;

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const media = window.matchMedia(MEDIA_QUERY);
    const update = () => setSystemReduced(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.motion = motionEnabled ? 'on' : 'off';
    return () => { delete document.documentElement.dataset.motion; };
  }, [motionEnabled]);

  const toggleMotion = () => {
    const next = !preferred;
    setPreferred(next);
    try { localStorage.setItem(STORAGE_KEY, next ? 'on' : 'off'); }
    catch { /* A browser with blocked storage can still change the current session. */ }
  };

  return (
    <MotionPreferenceContext.Provider value={{ motionEnabled, toggleMotion }}>
      <MotionConfig reducedMotion={motionEnabled ? 'user' : 'always'}>
        {children}
      </MotionConfig>
    </MotionPreferenceContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components -- context and hook share one contract
export function useMotionPreference(): MotionPreference {
  return useContext(MotionPreferenceContext);
}
