import React, { useState } from 'react';
import { RefreshCw, ScanSearch, ShieldCheck } from 'lucide-react';
import { fmtDateTime, fmtDuration, plural } from '../lib/format.js';
import { incidentFromAlert, incidentFromEvent } from '../lib/recommendations.js';
import InvestigationDrawer from '../components/InvestigationDrawer.jsx';
import { Empty, InfoBanner, PageIntro, Pill, Stat } from '../components/ui.jsx';

export default function Incidents({ dash }) {
  const { data, deviceId, refresh } = dash;
  const alerts = data?.alerts || [];
  const events = data?.events || [];
  const notes = data?.notes || [];
  const [selected, setSelected] = useState(null);

  const notesFor = (eventId) => notes.filter((n) => n.event_id === eventId).length;
  const activeCount = events.filter((e) => e.active).length;
  const longest = events.reduce((best, e) => Math.max(best, e.duration_seconds || 0), 0);

  return (
    <>
      <PageIntro eyebrow="INVESTIGATION WORKSPACE" title="Incidents & Forensics" desc="Investigate observed conditions with evidence and explicit uncertainty." />
      <InfoBanner icon={ScanSearch} title="Evidence-backed observations">
        Every condition becomes an incident that is stored in PostgreSQL with when it started, how long it lasted and the evidence seen. An incident is not automatically a confirmed security breach.
      </InfoBanner>

      <div className="stat-row">
        <Stat label="Active now" value={activeCount} hint="incidents still being observed" tone={activeCount ? 'alert' : ''} />
        <Stat label="Incidents stored" value={events.length} hint="most recent 100" />
        <Stat label="Longest incident" value={events.length ? fmtDuration(longest) : '—'} hint="first to last sighting" />
        <Stat label="Investigation notes" value={notes.length} hint="saved for this device" />
      </div>

      <section className="card">
        <div className="section-head">
          <div><h3>Active conditions</h3><p>What the rules detect in the latest snapshot.</p></div>
          <button className="smallbtn" onClick={refresh}><RefreshCw size={13} /> Refresh</button>
        </div>
        {alerts.length
          ? alerts.map((a) => (
            <div className="incident" key={a.id}>
              <div className={'severity-bar ' + a.severity} />
              <div className="incident-main">
                <div className="incident-title"><Pill value={a.severity} /> <b>{a.title}</b><span className="mono">{a.id}</span></div>
                <p>{a.detail}</p>
                <div className="evidence-line">
                  <span>Source: {a.source}</span>
                  <span>Classification: observed condition</span>
                  {a.event_id != null && <span>Notes: {notesFor(a.event_id)}</span>}
                </div>
              </div>
              <button className="smallbtn" onClick={() => setSelected(incidentFromAlert(a, events))}>Investigate</button>
            </div>
          ))
          : <Empty icon={ShieldCheck} title="No conditions to investigate" desc="Threshold and lock observations will appear here when detected." />}
      </section>

      <section className="card" style={{ marginTop: 16 }}>
        <div className="section-head">
          <div><h3>Incident history</h3><p>Every stored incident of this device, newest first. Ended incidents are kept as evidence.</p></div>
          <span className="pill blue">{events.length} incidents</span>
        </div>
        {events.length === 0 && <p className="mutedline">No incidents recorded yet.</p>}
        {events.map((e) => (
          <div className="incident" key={e.id}>
            <div className={'severity-bar ' + e.severity} />
            <div className="incident-main">
              <div className="incident-title">
                <Pill value={e.severity} /> <Pill value={e.active ? 'active' : 'ended'} tone={e.active ? 'orange' : 'green'} /> <b>{e.message}</b>
                <span className="mono">#{e.id}</span>
              </div>
              <p>{e.evidence}</p>
              <div className="evidence-line">
                <span>First seen: {fmtDateTime(e.timestamp)}</span>
                <span>Last seen: {fmtDateTime(e.last_seen)}</span>
                <span>Lasted: {fmtDuration(e.duration_seconds)}</span>
                <span>Seen in {plural(e.occurrences, 'snapshot')}</span>
                <span>Notes: {notesFor(e.id)}</span>
              </div>
            </div>
            <button className="smallbtn" onClick={() => setSelected(incidentFromEvent(e))}>Investigate</button>
          </div>
        ))}
      </section>

      {selected && (
        <InvestigationDrawer incident={selected} notes={notes} deviceId={deviceId} close={() => setSelected(null)} onSaved={refresh} />
      )}
    </>
  );
}
