import test from 'node:test';
import assert from 'node:assert/strict';
import { fmtBytes, fmtDuration, fmtRate, plural } from '../src/lib/format.js';
import { levelFor, processMemoryLevel, THRESHOLDS } from '../src/lib/thresholds.js';
import {
  buildRecommendations, evidenceLine, guidanceFor, incidentFromAlert, incidentFromEvent,
} from '../src/lib/recommendations.js';

test('fmtBytes scales units and survives bad input', () => {
  assert.equal(fmtBytes(0), '0 B');
  assert.equal(fmtBytes(1536), '1.5 KB');
  assert.equal(fmtBytes(5 * 1024 ** 3), '5.0 GB');
  assert.equal(fmtBytes(undefined), '—');
  assert.equal(fmtBytes(null), '—');
  assert.equal(fmtRate(2 * 1024 * 1024), '2.0 MB/s');
  assert.equal(fmtRate(NaN), '—');
});

test('fmtDuration reads naturally', () => {
  assert.equal(fmtDuration(0), '0s');
  assert.equal(fmtDuration(45), '45s');
  assert.equal(fmtDuration(125), '2m 5s');
  assert.equal(fmtDuration(3700), '1h 1m');
  assert.equal(fmtDuration(-1), '—');
  assert.equal(fmtDuration(undefined), '—');
  assert.equal(plural(1, 'snapshot'), '1 snapshot');
  assert.equal(plural(3, 'snapshot'), '3 snapshots');
});

test('thresholds match the backend alert levels', () => {
  // backend/main.py: CPU 85, memory 90, disk 90
  assert.equal(THRESHOLDS.cpu.crit, 85);
  assert.equal(THRESHOLDS.memory.crit, 90);
  assert.equal(THRESHOLDS.disk.crit, 90);
  assert.equal(levelFor('cpu', 40), 'ok');
  assert.equal(levelFor('cpu', 70), 'warn');
  assert.equal(levelFor('cpu', 85), 'crit');
  assert.equal(levelFor('memory', 89.9), 'warn');
  assert.equal(levelFor('disk', undefined), 'idle');
  assert.equal(processMemoryLevel(5), 'ok');
  assert.equal(processMemoryLevel(25), 'crit');
});

const alert = (id, severity, extra = {}) => ({ id, severity, title: id, detail: `${id} detail`, source: 'OS', evidence: {}, ...extra });

test('recommendations are generated from active alerts, most severe first', () => {
  const recs = buildRecommendations([
    alert('OS-CPU', 'high', { evidence: { top_process: { name: 'chrome.exe', pid: 999, cpu_percent: 40 } } }),
    alert('OS-MEM', 'critical', { evidence: { top_memory_processes: [{ name: 'a', memory_percent: 30 }, { name: 'b', memory_percent: 10 }] } }),
    alert('CORR-CPU-DB', 'warning'),
  ]);
  assert.deepEqual(recs.map((r) => r.key), ['OS-MEM', 'OS-CPU', 'CORR-CPU-DB']);
  assert.equal(recs[0].severity, 'critical');
  assert.match(recs[1].evidence[0], /chrome\.exe \(PID 999\) using 40% of CPU/);
  assert.match(recs[0].evidence[0], /Largest: a 30%, b 10%/);
  assert.ok(recs.every((r) => r.steps.length > 0 && r.why && r.risk));
});

test('no active alerts means no recommendations', () => {
  assert.deepEqual(buildRecommendations([]), []);
  assert.deepEqual(buildRecommendations(), []);
});

test('all blocked-session alerts become one recommendation', () => {
  const recs = buildRecommendations([
    alert('LOCK-11', 'warning', { evidence: { blocked_pid: 11, blocking_pid: 12 } }),
    alert('LOCK-21', 'warning', { evidence: { blocked_pid: 21, blocking_pid: 22 } }),
  ]);
  assert.equal(recs.length, 1);
  assert.equal(recs[0].alerts.length, 2);
  assert.deepEqual(recs[0].evidence, ['PID 11 waits on PID 12', 'PID 21 waits on PID 22']);
  assert.match(recs[0].steps.join(' '), /never done by DBSentinel/);
});

test('every rule the backend can raise has specific guidance', () => {
  const rules = ['OS-CPU', 'OS-MEM', 'OS-DISK', 'DB-CONN', 'LOCK-7', 'CORR-CPU-DB', 'CORR-MEM-DB', 'CORR-DISK-DB', 'CORR-IO-DB', 'CORR-LOCK-HOST'];
  const generic = guidanceFor('SOMETHING-NEW');
  for (const rule of rules) assert.notEqual(guidanceFor(rule), generic, rule);
  assert.equal(guidanceFor('SOMETHING-NEW').title, 'Review this condition');
});

test('nothing in the guidance suggests an automatic destructive action', () => {
  const rules = ['OS-CPU', 'OS-MEM', 'OS-DISK', 'DB-CONN', 'LOCK-1', 'CORR-CPU-DB', 'CORR-MEM-DB', 'CORR-DISK-DB', 'CORR-IO-DB', 'CORR-LOCK-HOST'];
  for (const rule of rules) {
    const text = JSON.stringify(guidanceFor(rule)).toLowerCase();
    assert.doesNotMatch(text, /dbsentinel (will|can|does) (kill|terminate|delete)/);
  }
});

test('evidence lines never print undefined', () => {
  assert.equal(evidenceLine(alert('OS-DISK', 'high', { evidence: { disk_percent: 95, disk_free: 1024 ** 3 } })), 'System volume 95% used, 1.0 GB free');
  assert.doesNotMatch(evidenceLine(alert('OS-CPU', 'high', { evidence: { top_process: {} } })), /undefined/);
  assert.equal(evidenceLine(alert('CORR-IO-DB', 'warning')), 'CORR-IO-DB detail');
});

test('a live alert and a stored incident normalise to the same shape', () => {
  const events = [{ id: 5, timestamp: 't0', last_seen: 't1', occurrences: 4, duration_seconds: 20 }];
  const fromAlert = incidentFromAlert(alert('OS-CPU', 'high', { event_id: 5 }), events);
  assert.equal(fromAlert.eventId, 5);
  assert.equal(fromAlert.snapshots, 4);
  assert.equal(fromAlert.active, true);
  const fromEvent = incidentFromEvent({
    id: 5, severity: 'high', message: 'High CPU utilization', evidence: 'text', source: 'OS', type: 'resource_pressure',
    details: { rule_id: 'OS-CPU', cpu_percent: 95 }, active: false, resolved_at: 't2', timestamp: 't0', last_seen: 't1',
    occurrences: 4, duration_seconds: 20,
  });
  assert.equal(fromEvent.ruleId, 'OS-CPU');
  assert.deepEqual(fromEvent.evidence, { cpu_percent: 95 });   // the rule id is not shown as "evidence"
  assert.equal(fromEvent.active, false);
  assert.deepEqual(Object.keys(fromAlert).sort().filter((k) => k !== 'resolvedAt'), Object.keys(fromEvent).sort().filter((k) => k !== 'resolvedAt'));
});

test('an alert without a stored incident still opens the drawer', () => {
  const incident = incidentFromAlert(alert('DB-CONN', 'warning'), []);
  assert.equal(incident.eventId, null);
  assert.equal(incident.snapshots, undefined);
});
