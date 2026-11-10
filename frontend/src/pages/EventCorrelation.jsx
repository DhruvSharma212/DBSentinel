import React from 'react';
import { GitMerge } from 'lucide-react';
import { fmtDateTime, fmtDuration, plural } from '../lib/format.js';
import { InfoBanner, PageIntro, Pill, Table } from '../components/ui.jsx';

export default function EventCorrelation({ dash }) {
  const history = dash.data?.history || [];
  const events = dash.data?.events || [];

  return (
    <>
      <PageIntro eyebrow="CROSS-SOURCE ANALYSIS" title="Event Correlation" desc="A shared view of current OS and database observations. Correlation is evidence, not proof of causality." />
      <InfoBanner icon={GitMerge} title="Transparent correlation mode">
        PostgreSQL retains time-stamped snapshots, incidents with their duration and the number of snapshots that showed them, and investigation notes. Correlation rules produce leads only; native log ingestion and behavior baselines are not configured.
      </InfoBanner>

      <section className="card tablecard">
        <div className="section-head">
          <div><h3>Adjacent telemetry snapshots</h3><p>Compare resource and database signals over time</p></div>
          <span className="pill blue">{history.length} snapshots</span>
        </div>
        <Table
          headers={['TIME', 'CPU %', 'MEMORY %', 'DISK %', 'DB CONNECTIONS', 'ACTIVE SESSIONS']}
          rows={history.slice(-12).map((h) => [
            new Date(h.timestamp).toLocaleTimeString(), Number(h.cpu || 0).toFixed(1), Number(h.memory || 0).toFixed(1),
            Number(h.disk || 0).toFixed(1), h.connections ?? '—', h.active ?? '—',
          ])}
          empty="Waiting for snapshots"
        />
      </section>

      <section className="card">
        <div className="section-head">
          <div><h3>Persistent incident timeline</h3><p>Each incident is one stretch of time during which a condition or correlation lead was observed</p></div>
          <span className="pill blue">{events.length} incidents</span>
        </div>
        {events.length === 0 && <p className="mutedline">No incidents recorded for this device yet.</p>}
        {events.map((e) => (
          <div className="timeline-row" key={e.id}>
            <div className="timeline-track"><i /></div>
            <div className="timeline-body">
              <div className="timeline-meta">
                <span>{fmtDateTime(e.timestamp)}</span>
                <Pill value={e.severity} />
                <span className="source-tag">{e.source}</span>
                <span className="source-tag">{plural(e.occurrences, 'snapshot')}</span>
                <span className="source-tag">{fmtDuration(e.duration_seconds)}</span>
                <Pill value={e.active ? 'active' : 'ended'} tone={e.active ? 'orange' : 'green'} />
              </div>
              <b>{e.message}</b>
              <p>{e.evidence}</p>
              <small>{e.active ? 'Still being observed' : `Last seen: ${fmtDateTime(e.last_seen)}`}</small>
            </div>
          </div>
        ))}
      </section>

      <section className="card note-card">
        <h3>How to interpret correlation</h3>
        <p>Events close in time may be related, but temporal proximity alone does not establish root cause. Confirm using process details, query duration, lock relationships, and database logs.</p>
      </section>
    </>
  );
}
