// Turns what the rules detected into explained, recommendation-only next steps.
// DBSentinel never acts on the machine or the database: these are things a person can check or do.
import { fmtBytes } from './format.js';

const SEVERITY_ORDER = { critical: 0, high: 1, warning: 2, info: 3 };

const GUIDE = {
  'OS-CPU': {
    title: 'Inspect the busiest process',
    why: 'Sustained high CPU slows everything else on the machine, including PostgreSQL.',
    risk: 'Low',
    steps: [
      'Open Task Manager (or the OS Monitor page) and look at the process listed in the evidence.',
      'Check who started it and whether this workload is expected right now.',
      'Do not end system processes. If it is an application, save its work first, then restart it.',
    ],
  },
  'OS-MEM': {
    title: 'Find what is using the memory',
    why: 'Near-full memory forces swapping to disk, which makes every program, and the database, much slower.',
    risk: 'Medium',
    steps: [
      'Look at the largest processes in the evidence and in the OS Monitor page.',
      'Close or restart the biggest consumer after saving its work. A memory that keeps growing may be a leak.',
      'If PostgreSQL is the consumer, review its memory settings (shared_buffers, work_mem) before changing anything.',
    ],
  },
  'OS-DISK': {
    title: 'Free or extend disk space',
    why: 'A full disk can stop logging, backups and database writes, and PostgreSQL can shut down when it cannot write.',
    risk: 'Medium',
    steps: [
      'Use a trusted tool (Windows Storage settings, WinDirStat) to find the largest folders.',
      'Check log, backup and temporary-file retention, and PostgreSQL WAL and log folders.',
      'Do not delete files automatically or blindly: confirm what they are first.',
    ],
  },
  'DB-CONN': {
    title: 'Restore the PostgreSQL connection',
    why: 'While the agent cannot reach PostgreSQL, sessions, queries and locks are invisible.',
    risk: 'Low',
    steps: [
      'Check that the PostgreSQL service is running (services.msc).',
      'Verify host, port, user and password in the agent configuration (agent.config.json or --setup).',
      'Confirm the monitoring role can log in and has pg_monitor.',
    ],
  },
  LOCK: {
    title: 'Investigate blocked sessions',
    why: 'A session waiting on a lock holds a connection and a user waiting. A lock wait is not automatically a deadlock.',
    risk: 'Medium',
    steps: [
      'Identify the blocking transaction (the blocking PID in the evidence) and who owns it.',
      'Confirm the business impact, then let it finish or ask its owner to commit or roll back.',
      'Cancelling or terminating a session is a last resort and is never done by DBSentinel.',
    ],
  },
  'CORR-CPU-DB': {
    title: 'Check which query is driving the CPU',
    why: 'High CPU and active database sessions at the same moment suggest a database workload may be the cause. It is a lead, not proof.',
    risk: 'Low',
    steps: [
      'Look at the longest active query in the evidence (and the Database Monitor session list).',
      'Run EXPLAIN (ANALYZE) on it in a safe environment to see whether it scans too much data.',
      'Compare with the OS Monitor: is postgres the busiest process, or is something else using the CPU?',
    ],
  },
  'CORR-MEM-DB': {
    title: 'Review PostgreSQL memory use',
    why: 'PostgreSQL processes hold a large share of RAM while memory is nearly exhausted.',
    risk: 'Medium',
    steps: [
      'Check shared_buffers and work_mem against the number of connections.',
      'Look for many parallel sessions each using large sorts or hashes.',
      'Change settings in a maintenance window and observe the effect on this dashboard.',
    ],
  },
  'CORR-DISK-DB': {
    title: 'Protect PostgreSQL from a full disk',
    why: 'Active sessions keep writing while free space is almost gone.',
    risk: 'High',
    steps: [
      'Check WAL size, archiving and replication slots that keep WAL files.',
      'Check PostgreSQL log rotation and temporary-file usage.',
      'Free space safely (see the disk recommendation) before the database has to stop.',
    ],
  },
  'CORR-IO-DB': {
    title: 'Find the I/O-heavy activity',
    why: 'Disk throughput is high while PostgreSQL is busy.',
    risk: 'Low',
    steps: [
      'Check the active sessions for large scans, bulk loads or sorts that spill to disk.',
      'Check whether a VACUUM, checkpoint, backup or index build is running.',
      'Compare with the disk throughput on the OS Monitor page.',
    ],
  },
  'CORR-LOCK-HOST': {
    title: 'Resolve lock waits to relieve the load',
    why: 'Blocked sessions pile up while the machine is already under load.',
    risk: 'Medium',
    steps: [
      'Handle the blocked-session recommendation first: the blocking transaction is the key.',
      'Watch whether CPU and memory drop once the lock is released.',
    ],
  },
  default: {
    title: 'Review this condition',
    why: 'A rule detected something unusual.',
    risk: 'Low',
    steps: ['Read the evidence, compare it with the OS and Database Monitor pages, and check native logs before concluding anything.'],
  },
};

export function guidanceFor(ruleId = '') {
  if (ruleId.startsWith('LOCK-')) return GUIDE.LOCK;
  return GUIDE[ruleId] || GUIDE.default;
}

// One readable line of evidence for an alert.
export function evidenceLine(alert) {
  const ev = alert.evidence || {};
  if (alert.id === 'OS-CPU' && ev.top_process) {
    return `Top process: ${ev.top_process.name || 'unknown'} (PID ${ev.top_process.pid ?? '—'}) using ${ev.top_process.cpu_percent ?? 0}% of CPU`;
  }
  if (alert.id === 'OS-MEM' && Array.isArray(ev.top_memory_processes)) {
    return 'Largest: ' + ev.top_memory_processes.slice(0, 3).map((p) => `${p.name} ${p.memory_percent}%`).join(', ');
  }
  if (alert.id === 'OS-DISK') return `System volume ${ev.disk_percent}% used, ${fmtBytes(ev.disk_free)} free`;
  if (alert.id.startsWith('LOCK-')) return `PID ${ev.blocked_pid} waits on PID ${ev.blocking_pid}`;
  return alert.detail || '';
}

// Active alerts -> recommendations, most severe first. All blocked-session alerts become one recommendation.
export function buildRecommendations(alerts = []) {
  const byKey = new Map();
  for (const alert of alerts) {
    const key = alert.id.startsWith('LOCK-') ? 'LOCK' : alert.id;
    if (!byKey.has(key)) byKey.set(key, { key, ...guidanceFor(alert.id), alerts: [] });
    byKey.get(key).alerts.push(alert);
  }
  const rank = (rec) => Math.min(...rec.alerts.map((a) => SEVERITY_ORDER[a.severity] ?? 9));
  return [...byKey.values()]
    .map((rec) => ({ ...rec, severity: rec.alerts.reduce((best, a) => ((SEVERITY_ORDER[a.severity] ?? 9) < (SEVERITY_ORDER[best] ?? 9) ? a.severity : best), rec.alerts[0].severity),
      evidence: rec.alerts.map(evidenceLine).filter(Boolean).slice(0, 4) }))
    .sort((a, b) => rank(a) - rank(b));
}

// Normalise a live alert or a stored incident into one shape for the investigation drawer.
export function incidentFromAlert(alert, events = []) {
  const stored = events.find((e) => e.id === alert.event_id);
  return {
    eventId: alert.event_id ?? null, ruleId: alert.id, severity: alert.severity, title: alert.title, detail: alert.detail,
    evidence: alert.evidence, source: alert.source, active: true,
    firstSeen: stored?.timestamp, lastSeen: stored?.last_seen, snapshots: stored?.occurrences, durationSeconds: stored?.duration_seconds,
  };
}

export function incidentFromEvent(event) {
  const { rule_id: ruleId, ...evidence } = event.details || {};
  return {
    eventId: event.id, ruleId: ruleId || event.type, severity: event.severity, title: event.message, detail: event.evidence,
    evidence, source: event.source, active: !!event.active, resolvedAt: event.resolved_at,
    firstSeen: event.timestamp, lastSeen: event.last_seen, snapshots: event.occurrences, durationSeconds: event.duration_seconds,
  };
}
