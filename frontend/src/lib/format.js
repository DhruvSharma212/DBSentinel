// Small formatting helpers (no React, no browser APIs: easy to unit-test).

export function fmtBytes(n) {
  if (!Number.isFinite(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

export function fmtRate(bytesPerSecond) {
  return Number.isFinite(bytesPerSecond) ? `${fmtBytes(bytesPerSecond)}/s` : '—';
}

// 45 -> "45s", 125 -> "2m 5s", 3700 -> "1h 1m"
export function fmtDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function fmtDateTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

export function fmtClock(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// "1 snapshot" / "3 snapshots"
export function plural(count, word) {
  return `${count} ${word}${count === 1 ? '' : 's'}`;
}
