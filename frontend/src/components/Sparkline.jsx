import React from 'react';

// A tiny trend line for a metric tile. Plain SVG: no chart library needed for 30 points.
export default function Sparkline({ values = [], color = '#43d9e8', max }) {
  const points = values.filter((v) => Number.isFinite(v));
  if (points.length < 2) return null;
  const top = max ?? Math.max(...points, 1);
  const low = Math.min(...points, 0);
  const span = top - low || 1;
  const step = 100 / (points.length - 1);
  const path = points.map((v, i) => `${(i * step).toFixed(2)},${(26 - ((v - low) / span) * 24).toFixed(2)}`).join(' ');
  return (
    <svg className="spark" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true">
      <polyline points={path} fill="none" stroke={color} strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
