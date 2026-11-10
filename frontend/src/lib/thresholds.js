// Alert thresholds, kept in step with backend/main.py (CPU_THRESHOLD, MEM_THRESHOLD, DISK_THRESHOLD).
// `warn` is where a tile starts to look cautious; `crit` is where the backend raises an alert.
export const THRESHOLDS = {
  cpu: { warn: 70, crit: 85 },
  memory: { warn: 80, crit: 90 },
  disk: { warn: 80, crit: 90 },
};

// 'idle' (no value) | 'ok' | 'warn' | 'crit'
export function levelFor(kind, value) {
  if (!Number.isFinite(value)) return 'idle';
  const t = THRESHOLDS[kind];
  if (value >= t.crit) return 'crit';
  if (value >= t.warn) return 'warn';
  return 'ok';
}

// One process holding this share of all RAM is worth a look (system-wide memory has its own thresholds above).
export const PROCESS_MEMORY = { warn: 10, crit: 20 };

export function processMemoryLevel(percent) {
  if (!Number.isFinite(percent)) return 'idle';
  if (percent >= PROCESS_MEMORY.crit) return 'crit';
  if (percent >= PROCESS_MEMORY.warn) return 'warn';
  return 'ok';
}
