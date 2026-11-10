// Small presentational building blocks shared by every page. They are defined at module level so React
// keeps their identity between renders (inputs keep focus, dropdowns stay open while data refreshes).
import React, { useEffect } from 'react';
import { Activity, X } from 'lucide-react';
import Sparkline from './Sparkline.jsx';

const SPARK_COLOR = { ok: '#43d9e8', warn: '#f5ad62', crit: '#fb7185' };

// `level` (ok | warn | crit | idle) colours the tile; `spark` is a short history for the trend line.
export function Metric({ icon: Icon, label, value, unit, sub, tone = 'cyan', percent, level = 'idle', spark }) {
  return (
    <div className={'metric card lvl-' + level}>
      <div className="metric-top">
        <span className={'metric-icon ' + tone}><Icon size={17} /></span>
        <span className="metric-label">{label}</span>
        {spark && <Sparkline values={spark} color={SPARK_COLOR[level] || SPARK_COLOR.ok} />}
      </div>
      <div className="metric-value">{value}<small>{value === '—' ? '' : (unit || '')}</small></div>
      <div className="metric-bottom">
        {percent !== undefined && <div className="progress"><i style={{ width: Math.min(100, percent) + '%' }} /></div>}
        <span>{sub}</span>
      </div>
    </div>
  );
}

export function ChartCard({ title, subtitle, children, extra }) {
  return (
    <section className="card chartcard">
      <div className="section-head">
        <div><h3>{title}</h3><p>{subtitle}</p></div>
        {extra}
      </div>
      {children}
    </section>
  );
}

export function Empty({ icon: Icon = Activity, title, desc }) {
  return (
    <div className="empty">
      <Icon size={26} />
      <b>{title}</b>
      <span>{desc}</span>
    </div>
  );
}

export function Table({ headers, rows, empty = 'No records available' }) {
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{headers.map((h) => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>
          {rows.length
            ? rows.map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>)
            : <tr><td colSpan={headers.length}><Empty title={empty} desc="Data will appear when the connected source provides it." /></td></tr>}
        </tbody>
      </table>
    </div>
  );
}

const PILL_COLOR = { critical: 'red', high: 'orange', warning: 'yellow', info: 'blue' };

// A coloured label for a severity (critical/high/warning/info) or any state word.
export function Pill({ value, tone }) {
  if (!value) return '—';
  return <span className={'pill ' + (tone || PILL_COLOR[value] || 'green')}>{value}</span>;
}

// A number with a small proportional bar, for table cells.
export function BarCell({ value, max = 100, level = 'ok' }) {
  const width = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className="barcell">
      <span>{value.toFixed(1)}</span>
      <div className={'minibar ' + level}><i style={{ width: width + '%' }} /></div>
    </div>
  );
}

export function PageIntro({ eyebrow, title, desc }) {
  return (
    <div className="pageintro">
      <div className="eyebrow">{eyebrow}</div>
      <h1>{title}</h1>
      <p>{desc}</p>
    </div>
  );
}

export function Drawer({ title, close, children }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [close]);
  return (
    <div className="drawer-backdrop" onClick={close}>
      <aside className="drawer" role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <b>{title}</b>
          <button className="iconbtn" onClick={close} aria-label="Close"><X size={18} /></button>
        </div>
        {children}
      </aside>
    </div>
  );
}

export function InfoBanner({ icon: Icon, title, children }) {
  return (
    <div className="info-banner">
      <Icon size={20} />
      <div><b>{title}</b><span>{children}</span></div>
    </div>
  );
}

// A compact figure with a caption, used in summary rows.
export function Stat({ label, value, hint, tone }) {
  return (
    <div className={'stat card' + (tone ? ' ' + tone : '')}>
      <span>{label}</span>
      <b>{value}</b>
      {hint && <small>{hint}</small>}
    </div>
  );
}
