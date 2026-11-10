import React, { useState } from 'react';
import { ScrollText, Search } from 'lucide-react';
import { fmtDateTime, plural } from '../lib/format.js';
import { InfoBanner, PageIntro, Pill, Table } from '../components/ui.jsx';

export default function AuditTrail({ dash }) {
  const events = dash.data?.events || [];
  const notes = dash.data?.notes || [];
  const [search, setSearch] = useState('');

  const needle = search.toLowerCase();
  const filtered = events.filter((e) => (e.message + e.source + e.type).toLowerCase().includes(needle));
  const incidentTitle = (id) => events.find((e) => e.id === id)?.message;

  return (
    <>
      <PageIntro eyebrow="TRACEABILITY" title="Audit Trail" desc="Persisted application observations from the PostgreSQL evidence store." />
      <InfoBanner icon={ScrollText} title="Local evidence retention enabled">
        Snapshots, incidents (with duration and snapshot counts) and investigation notes persist across backend restarts. Native OS/PostgreSQL log ingestion is not configured.
      </InfoBanner>

      <section className="card tablecard">
        <div className="section-head">
          <div><h3>Diagnostic events</h3><p>Incidents stored for this device, newest first</p></div>
          <div className="searchbox">
            <Search size={15} />
            <input placeholder="Filter events…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
        </div>
        <Table
          headers={['STARTED', 'SOURCE', 'TYPE', 'SEVERITY', 'MESSAGE', 'EVIDENCE', 'SNAPSHOTS', 'STATUS']}
          rows={filtered.map((e) => [
            fmtDateTime(e.timestamp), e.source, e.type, <Pill value={e.severity} />, e.message, e.evidence, plural(e.occurrences, 'snapshot'),
            <Pill value={e.active ? 'active' : 'ended'} tone={e.active ? 'orange' : 'green'} />,
          ])}
        />
      </section>

      <section className="card tablecard">
        <div className="section-head">
          <div><h3>Investigation notes</h3><p>Operator notes kept in PostgreSQL, with the incident they refer to</p></div>
          <span className="pill blue">{notes.length} notes</span>
        </div>
        <Table
          headers={['SAVED AT', 'AUTHOR', 'INCIDENT', 'NOTE']}
          rows={notes.map((n) => [fmtDateTime(n.created_at), n.author, n.event_id == null ? '—' : (incidentTitle(n.event_id) || `#${n.event_id}`), n.note])}
          empty="No investigation notes saved yet"
        />
      </section>
    </>
  );
}
