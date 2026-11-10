import React, { useState } from 'react';
import { CheckCircle2, ShieldCheck } from 'lucide-react';
import { buildRecommendations, incidentFromAlert } from '../lib/recommendations.js';
import InvestigationDrawer from '../components/InvestigationDrawer.jsx';
import { Empty, InfoBanner, PageIntro, Pill } from '../components/ui.jsx';

export default function SafeResponse({ dash, toast }) {
  const { data, deviceId, refresh } = dash;
  const alerts = data?.alerts || [];
  const [selected, setSelected] = useState(null);
  const recommendations = buildRecommendations(alerts);

  return (
    <>
      <PageIntro eyebrow="CONTROLLED RESPONSE" title="Safe Response Center" desc="Evidence-led recommendations. No process termination or database session cancellation is enabled." />
      <InfoBanner icon={ShieldCheck} title="Safe mode enabled">
        Actions are recommendation-only. Recommendations below are generated from the conditions detected right now, with the evidence behind each one; critical actions are never executed by DBSentinel.
      </InfoBanner>

      {recommendations.length === 0 ? (
        <section className="card">
          <Empty icon={CheckCircle2} title="Nothing needs attention" desc="No condition is active on this device, so there are no recommendations. Past incidents are kept under Incidents & Forensics." />
        </section>
      ) : (
        <div className="response-grid">
          {recommendations.map((r, i) => (
            <div className="card response-card" key={r.key}>
              <div className="response-icon"><ShieldCheck /></div>
              <div className="response-kicker">RECOMMENDATION {String(i + 1).padStart(2, '0')} · <Pill value={r.severity} /></div>
              <h3>{r.title}</h3>
              <p>{r.why}</p>
              <div className="response-evidence"><span>Current evidence</span><b>{r.evidence.map((line) => <div key={line}>{line}</div>)}</b></div>
              <div className="response-evidence"><span>Risk level</span><b>{r.risk}</b></div>
              <div className="response-evidence"><span>Suggested verification</span><b>{r.steps.map((s) => <div key={s}>• {s}</div>)}</b></div>
              <button className="secondarybtn" onClick={() => setSelected(incidentFromAlert(r.alerts[0], data?.events || []))}>Investigate and add a note</button>
              <button className="secondarybtn" onClick={() => toast('Recommendation acknowledged locally. No system action was executed.')}>Acknowledge recommendation</button>
            </div>
          ))}
        </div>
      )}

      {selected && (
        <InvestigationDrawer incident={selected} notes={data?.notes || []} deviceId={deviceId} close={() => setSelected(null)} onSaved={refresh} />
      )}
    </>
  );
}
