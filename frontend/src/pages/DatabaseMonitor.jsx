import React, { useState } from 'react';
import { Activity, Database, LockKeyhole } from 'lucide-react';
import { Drawer, Metric, PageIntro, Pill, Table } from '../components/ui.jsx';

export default function DatabaseMonitor({ dash }) {
  const db = dash.data?.database;
  const [selected, setSelected] = useState(null);

  const notConfigured = db?.configured === false;
  const bannerText = db?.connected
    ? db.message
    : notConfigured
      ? 'PostgreSQL monitoring is not set up on this agent. Add postgres.dsn to agent.config.json (or run the agent with --setup) and restart it.'
      : (db?.message || 'No database data yet.');

  return (
    <>
      <PageIntro eyebrow="POSTGRESQL OBSERVABILITY" title="Database Monitor" desc="Inspect connection pressure, active sessions, and observed lock waits." />
      <div className="connection-banner">
        <span className={'connection-mark ' + (db?.connected ? 'ok' : 'bad')}><Database size={20} /></span>
        <div>
          <b>{db?.connected ? 'PostgreSQL connected' : 'PostgreSQL connection unavailable'}</b>
          <span>{bannerText}</span>
        </div>
        <span className={'pill ' + (db?.connected ? 'green' : notConfigured ? 'yellow' : 'red')}>
          {db?.connected ? 'CONNECTED' : notConfigured ? 'NOT CONFIGURED' : 'DISCONNECTED'}
        </span>
      </div>

      <div className="metrics three">
        <Metric icon={Database} label="TOTAL CONNECTIONS" value={db?.connected ? db.counts.total : '—'} sub="Current snapshot" />
        <Metric icon={Activity} label="ACTIVE SESSIONS" value={db?.connected ? db.counts.active : '—'} sub="state = active" tone="green" />
        <Metric icon={LockKeyhole} label="BLOCKED SESSIONS" value={db?.connected ? db.locks.length : '—'} sub="Current lock blocking relationships" tone="orange" />
      </div>

      <section className="card tablecard">
        <div className="section-head">
          <div><h3>Session inspector</h3><p>Query text is truncated for safer display. Requires PostgreSQL monitoring permissions.</p></div>
        </div>
        <Table
          headers={['PID', 'USER', 'DATABASE', 'STATE', 'DURATION', 'QUERY']}
          rows={(db?.queries || []).map((q) => [
            q.pid, q.username || '—', q.database || '—', <Pill value={q.state || 'unknown'} />, `${q.duration_seconds ?? 0}s`,
            <button className="querylink" onClick={() => setSelected(q)}>{(q.query || 'Query unavailable').slice(0, 65)}…</button>,
          ])}
        />
      </section>

      <section className="card tablecard">
        <div className="section-head"><div><h3>Lock waits</h3><p>A blocked session is not automatically a deadlock.</p></div></div>
        <Table
          headers={['BLOCKED PID', 'BLOCKING PID', 'QUERY EVIDENCE']}
          rows={(db?.locks || []).map((l) => [l.blocked_pid, l.blocking_pid, l.blocked_query || 'Unavailable'])}
          empty="No lock waits observed"
        />
      </section>

      {selected && (
        <Drawer title="Query inspection" close={() => setSelected(null)}>
          <div className="detailgrid">
            {Object.entries(selected).map(([k, v]) => <div key={k}><span>{k.replaceAll('_', ' ')}</span><b>{String(v ?? '—')}</b></div>)}
          </div>
          <pre className="sql">{selected.query || 'No query text available'}</pre>
        </Drawer>
      )}
    </>
  );
}
