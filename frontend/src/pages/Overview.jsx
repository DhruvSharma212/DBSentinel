import React, { useMemo, useState } from 'react';
import { Activity, AlertTriangle, ArrowUpRight, CheckCircle2, Cpu, Database, HardDrive, MemoryStick, ShieldCheck } from 'lucide-react';
import { Area, AreaChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { fmtBytes, fmtClock } from '../lib/format.js';
import { THRESHOLDS, levelFor, processMemoryLevel } from '../lib/thresholds.js';
import { BarCell, ChartCard, Empty, Metric, Pill } from '../components/ui.jsx';

const TOOLTIP_STYLE = { background: '#101827', border: '1px solid #263449', borderRadius: 10 };
const TREND_POINTS = 30;

export default function Overview({ dash, go }) {
  const { data, cur, error } = dash;
  const os = data?.os;
  const db = data?.database;
  const alerts = data?.alerts || [];
  const busiest = (data?.processes || []).slice(0, 5);
  const [period, setPeriod] = useState('Live');
  const allHistory = data?.history || [];
  const history = useMemo(() => (period === 'Live' ? allHistory : allHistory.slice(-Number(period))), [allHistory, period]);
  const trend = (key) => allHistory.slice(-TREND_POINTS).map((h) => h[key]);

  const offline = cur && !cur.online;
  const dbSub = db?.connected
    ? `${db.counts.active} active · ${db.counts.idle} idle`
    : (db?.configured === false ? 'PostgreSQL not set up on agent' : 'PostgreSQL disconnected');
  const components = [
    ['Agent (host telemetry)', cur ? `${cur.name || cur.host} · v${cur.agent_version || '?'}` : 'no agent connected', !!cur?.online],
    ['API service', 'HTTP API', !error],
    ['PostgreSQL (on agent)', db?.configured === false ? 'not configured on this agent' : 'pg_stat_activity', !!db?.connected],
  ];

  return (
    <>
      <div className="welcome">
        <div>
          <div className="eyebrow"><span className="pulse" />SYSTEM HEALTH / LIVE MONITORING</div>
          <h1>System Overview</h1>
          <p>{cur ? `${cur.name || cur.host}${os?.platform ? ' · ' + os.platform : ''}` : 'Host resources, database activity and incidents in one place.'}</p>
        </div>
        <div className={'hero-status ' + (error || offline ? 'warn' : alerts.length ? 'alert' : '')}>
          <ShieldCheck size={21} />
          <div>
            <b>{error ? 'Connection issue' : offline ? 'Agent offline' : alerts.length ? 'Attention required' : 'All clear'}</b>
            <span>{error ? 'API unavailable' : offline ? 'Showing last known values' : `${alerts.length} active condition${alerts.length === 1 ? '' : 's'}`}</span>
          </div>
        </div>
      </div>

      <div className="metrics">
        <Metric icon={Cpu} label="CPU UTILIZATION" value={os?.cpu_percent?.toFixed(1) ?? '—'} unit="%" percent={os?.cpu_percent}
          level={levelFor('cpu', os?.cpu_percent)} spark={trend('cpu')} sub={`${os?.cpu_count || '—'} logical cores`} />
        <Metric icon={MemoryStick} label="MEMORY USED" value={os?.memory_percent?.toFixed(1) ?? '—'} unit="%" percent={os?.memory_percent} tone="violet"
          level={levelFor('memory', os?.memory_percent)} spark={trend('memory')} sub={`${fmtBytes(os?.memory_used)} of ${fmtBytes(os?.memory_total)}`} />
        <Metric icon={HardDrive} label="DISK UTILIZATION" value={os?.disk_percent?.toFixed(1) ?? '—'} unit="%" percent={os?.disk_percent} tone="blue"
          level={levelFor('disk', os?.disk_percent)} sub={`${fmtBytes(os?.disk_used)} used`} />
        <Metric icon={Database} label="DB CONNECTIONS" value={db?.connected ? db.counts.total : '—'} tone="green" spark={trend('connections')} sub={dbSub} />
        <Metric icon={Activity} label="RUNNING QUERIES" value={db?.connected ? db.counts.active : '—'} spark={trend('active')} sub="Active sessions right now" />
        <Metric icon={AlertTriangle} label="ACTIVE CONDITIONS" value={alerts.length} tone={alerts.length ? 'orange' : 'green'}
          level={alerts.length ? 'crit' : 'ok'} sub="Derived from current telemetry" />
      </div>

      <div className="charts-grid">
        <ChartCard
          title="Host resource telemetry"
          subtitle="CPU and memory utilization · dashed lines mark the alert thresholds"
          extra={(
            <select value={period} onChange={(e) => setPeriod(e.target.value)}>
              <option value="Live">Recent</option>
              <option value="20">Last 20</option>
              <option value="60">Last 60</option>
              <option value="120">Last 120</option>
            </select>
          )}
        >
          <div className="chart">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={history}>
                <defs>
                  <linearGradient id="cpuFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#43d9e8" stopOpacity={0.25} />
                    <stop offset="95%" stopColor="#43d9e8" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis dataKey="timestamp" tickFormatter={fmtClock} minTickGap={28} stroke="#64748b" fontSize={10} />
                <YAxis domain={[0, 100]} stroke="#64748b" fontSize={10} unit="%" width={38} />
                <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={(v) => new Date(v).toLocaleTimeString()} formatter={(v) => `${Number(v).toFixed(1)}%`} />
                <ReferenceLine y={THRESHOLDS.cpu.crit} stroke="#f5ad62" strokeDasharray="4 4" strokeOpacity={0.55} />
                <ReferenceLine y={THRESHOLDS.memory.crit} stroke="#fb7185" strokeDasharray="4 4" strokeOpacity={0.45} />
                <Area type="monotone" dataKey="cpu" name="CPU" stroke="#43d9e8" fill="url(#cpuFill)" strokeWidth={2} isAnimationActive={false} />
                <Line type="monotone" dataKey="memory" name="Memory" stroke="#a78bfa" strokeWidth={2} dot={false} isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <div className="legend">
            <span><i className="cyan-dot" /> CPU utilization (alert at {THRESHOLDS.cpu.crit}%)</span>
            <span><i className="violet-dot" /> Memory utilization (alert at {THRESHOLDS.memory.crit}%)</span>
          </div>
        </ChartCard>

        <ChartCard title="Database activity" subtitle="Connections captured with each host snapshot">
          <div className="chart">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={history}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis dataKey="timestamp" tickFormatter={fmtClock} minTickGap={30} stroke="#64748b" fontSize={10} />
                <YAxis stroke="#64748b" fontSize={10} allowDecimals={false} width={30} />
                <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={(v) => new Date(v).toLocaleTimeString()} />
                <Line type="monotone" dataKey="connections" name="Connections" stroke="#7c9cff" strokeWidth={2} dot={false} isAnimationActive={false} />
                <Line type="monotone" dataKey="active" name="Active" stroke="#36d399" strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="legend">
            <span><i className="blue-dot" /> Total connections</span>
            <span><i className="green-dot" /> Active connections</span>
          </div>
        </ChartCard>
      </div>

      <div className="bottom-grid three">
        <section className="card">
          <div className="section-head">
            <div><h3>Latest conditions</h3><p>Rule-based observations from current telemetry</p></div>
            <button className="textbtn" onClick={() => go('Incidents & Forensics')}>View all <ArrowUpRight size={14} /></button>
          </div>
          {alerts.length
            ? alerts.slice(0, 4).map((a) => (
              <div className="alert-row" key={a.id}>
                <span className={'alert-icon ' + a.severity}><AlertTriangle size={16} /></span>
                <div className="alert-copy"><b>{a.title}</b><span>{a.detail}</span></div>
                <Pill value={a.severity} />
              </div>
            ))
            : <Empty icon={CheckCircle2} title="No active conditions" desc="No configured threshold or database lock condition is currently detected." />}
        </section>

        <section className="card">
          <div className="section-head">
            <div><h3>Busiest processes</h3><p>By CPU, % of total capacity</p></div>
            <button className="textbtn" onClick={() => go('OS Monitor')}>All processes <ArrowUpRight size={14} /></button>
          </div>
          {busiest.length
            ? (
              <table className="compact">
                <thead><tr><th>PROCESS</th><th>CPU %</th><th>MEM %</th></tr></thead>
                <tbody>
                  {busiest.map((p) => (
                    <tr key={p.pid}>
                      <td><b>{p.name}</b><small>PID {p.pid}</small></td>
                      <td><BarCell value={p.cpu_percent} level={levelFor('cpu', p.cpu_percent)} /></td>
                      <td><BarCell value={p.memory_percent} level={processMemoryLevel(p.memory_percent)} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
            : <Empty title="No process data" desc="Waiting for the agent." />}
        </section>

        <section className="card">
          <div className="section-head">
            <div><h3>Collector status</h3><p>Live component diagnostics</p></div>
            <span className={'pill ' + (cur?.online ? 'green' : 'orange')}>{cur?.online ? 'AGENT ONLINE' : 'AGENT OFFLINE'}</span>
          </div>
          {components.map(([name, detail, ok]) => (
            <div className="component-row" key={name}>
              <span className={'component-led ' + (ok ? '' : 'offline')} />
              <div><b>{name}</b><span>{detail}</span></div>
              <span className={ok ? 'goodtext' : 'warntext'}>{ok ? 'Operational' : 'Unavailable'}</span>
            </div>
          ))}
        </section>
      </div>
    </>
  );
}
