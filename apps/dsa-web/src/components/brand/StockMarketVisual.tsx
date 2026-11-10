import { useEffect, useId, useRef, useState } from 'react';

// A periodic, deliberately synthetic study of market structure. No quotes,
// symbols, returns or data-provider requests are represented by this artwork.
const TILE_WIDTH = 800;
const COUNT = 32;
const closeAt = (index: number) => 184 - 62 * Math.sin(index / COUNT * Math.PI * 2)
  - 24 * Math.sin(index / COUNT * Math.PI * 6 + 0.8);
const candles = Array.from({ length: COUNT }, (_, index) => {
  const open = closeAt(index - 1);
  const close = closeAt(index);
  const x = index * TILE_WIDTH / COUNT + 12.5;
  const volume = 22 + 48 * (0.5 + 0.5 * Math.sin(index * 1.7)) + Math.abs(close - open);
  return { x, open, close, high: Math.min(open, close) - 12 - index % 9,
    low: Math.max(open, close) + 14 + index % 7, volume, rising: close < open };
});
const trace = Array.from({ length: COUNT + 1 }, (_, i) => `${i === 0 ? 'M' : 'L'} ${i * 25} ${closeAt(i)}`).join(' ');
const ribbon = `${trace} L 800 305 L 0 305 Z`;

export function StockMarketVisual({ motionEnabled, caption }: { motionEnabled: boolean; caption: string }) {
  const id = useId().replace(/:/g, '');
  const root = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(() => document.visibilityState !== 'hidden');
  const [inView, setInView] = useState(true);
  const active = motionEnabled && visible && inView;

  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', update);
    const observer = typeof IntersectionObserver === 'function'
      ? new IntersectionObserver(([entry]) => setInView(entry.isIntersecting), { threshold: 0.05 }) : null;
    if (root.current) observer?.observe(root.current);
    return () => { document.removeEventListener('visibilitychange', update); observer?.disconnect(); };
  }, []);

  return (
    <div ref={root} className="stock-scene" data-testid="stock-market-visual" data-ambient={active ? 'on' : 'off'}>
      <div className="stock-scene__bloom" aria-hidden="true" />
      <svg className="stock-scene__art" viewBox="0 0 800 420" fill="none" aria-hidden="true">
        <defs>
          <linearGradient id={`${id}-up`} x1="0" y1="0" x2="1" y2="1" gradientUnits="objectBoundingBox">
            <stop stopColor="#fff2e7" /><stop offset="0.2" stopColor="#f9b49b" /><stop offset="1" stopColor="#bc604f" />
          </linearGradient>
          <linearGradient id={`${id}-down`} x1="0" y1="0" x2="1" y2="1" gradientUnits="objectBoundingBox">
            <stop stopColor="#e0fff3" /><stop offset="0.25" stopColor="#89d9bd" /><stop offset="1" stopColor="#2a776e" />
          </linearGradient>
          <linearGradient id={`${id}-area`} x1="0" y1="80" x2="0" y2="310" gradientUnits="userSpaceOnUse">
            <stop stopColor="#9eb6bd" stopOpacity="0.2" /><stop offset="1" stopColor="#9eb6bd" stopOpacity="0" />
          </linearGradient>
          <linearGradient id={`${id}-scan`} x1="0" y1="0" x2="1" y2="0" gradientUnits="objectBoundingBox">
            <stop stopColor="#e6e3d2" stopOpacity="0" /><stop offset="1" stopColor="#e6e3d2" stopOpacity="0.12" />
          </linearGradient>
          <clipPath id={`${id}-clip`}><rect x="4" y="40" width="792" height="338" rx="8" /></clipPath>
        </defs>

        {/* Perspective grid belongs to the illustrative stage, not chart axes. */}
        <g className="stock-scene__grid">
          {[75, 130, 185, 240, 295].map(y => <path key={y} d={`M 0 ${y} H 800`} />)}
          {[70, 180, 290, 400, 510, 620, 730].map(x => <path key={x} d={`M ${x} 40 V 315`} />)}
          {[0, 100, 200, 300, 400, 500, 600, 700, 800].map(x => <path key={`floor-${x}`} d={`M ${400 + (x - 400) * 0.55} 314 L ${x} 405`} />)}
          {[330, 355, 387].map(y => <path key={`row-${y}`} d={`M ${(405 - y) * 2.1} ${y} H ${800 - (405 - y) * 2.1}`} />)}
        </g>
        <g clipPath={`url(#${id}-clip)`}>
          <g className="stock-scene__depth">
            {[52, 90, 126, 164, 140, 105, 64, 34].map((width, index) => (
              <rect key={index} x={792 - width} y={91 + index * 24} width={width} height="12" rx="2" />
            ))}
          </g>
          <g className="stock-scene__market-track">
            {[0, TILE_WIDTH].map(offset => (
              <g key={offset} transform={`translate(${offset} 0)`}>
                <path d={ribbon} fill={`url(#${id}-area)`} />
                <path d={trace} transform="translate(0 -43)" className="stock-scene__secondary-trace" />
                <path d={trace} transform="translate(0 44)" className="stock-scene__secondary-trace stock-scene__secondary-trace--faint" />
                <path d={trace} className="stock-scene__price-trace" />
                {candles.map((candle, index) => {
                  const y = Math.min(candle.open, candle.close);
                  const height = Math.max(5, Math.abs(candle.close - candle.open));
                  const color = candle.rising ? '#edac98' : '#8ed4ba';
                  const body = `${id}-${candle.rising ? 'up' : 'down'}`;
                  return (
                    <g key={index} className="stock-scene__candle">
                      <path d={`M ${candle.x} ${candle.high} V ${candle.low}`} stroke={color} strokeWidth="1.5" />
                      <path d={`M ${candle.x - 7} ${y} l 4 -4 h 14 l -4 4 Z`} fill={color} fillOpacity="0.85" />
                      <path d={`M ${candle.x + 7} ${y} l 4 -4 v ${height} l -4 4 Z`} fill={color} fillOpacity="0.35" />
                      <rect x={candle.x - 7} y={y} width="14" height={height} rx="1" fill={`url(#${body})`} />
                      <path d={`M ${candle.x - 6} ${y + height} V ${y + 1} H ${candle.x + 6}`} stroke="#ffffff" strokeOpacity="0.52" strokeWidth="0.7" />
                      <g className="stock-scene__volume" style={{ animationDelay: `${-index * 0.23}s` }}>
                        <rect x={candle.x - 7} y={372 - candle.volume} width="14" height={candle.volume} fill={color} fillOpacity="0.22" />
                        <path d={`M ${candle.x - 7} ${372 - candle.volume} h 14`} stroke={color} strokeOpacity="0.6" />
                      </g>
                    </g>
                  );
                })}
                <g className="stock-scene__signal">
                  <circle r="11" fill="currentColor" opacity="0.07" />
                  <circle r="6" fill="currentColor" opacity="0.16" />
                  <circle r="2.5" fill="currentColor" />
                </g>
              </g>
            ))}
          </g>
          <g className="stock-scene__scan">
            <rect x="-95" y="42" width="94" height="335" fill={`url(#${id}-scan)`} />
            <path d="M 0 42 V 377" stroke="#e4e9dc" strokeOpacity="0.32" />
          </g>
        </g>
        <path d="M 7 312 H 792" className="stock-scene__baseline" />
        <g className="stock-scene__focus" transform="translate(564 111)">
          <path d="M -15 -7 v -8 h 8 M 7 -15 h 8 v 8 M 15 7 v 8 h -8 M -7 15 h -8 v -8" stroke="#d8dcc6" strokeWidth="1" />
          <circle r="3" fill="#e4e8d4" />
          <circle className="stock-scene__pulse" r="12" stroke="#d8dcc6" strokeOpacity="0.6" />
        </g>
      </svg>
      <div className="stock-scene__caption"><span aria-hidden="true" />{caption}</div>
    </div>
  );
}
