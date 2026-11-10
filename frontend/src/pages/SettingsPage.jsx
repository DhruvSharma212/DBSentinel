import React from 'react';
import { ExternalLink } from 'lucide-react';
import { fmtDateTime } from '../lib/format.js';
import { PageIntro } from '../components/ui.jsx';

export default function SettingsPage({ dash, toast }) {
  const { settings, cur, error, data } = dash;
  const os = data?.os;
  const installed = settings?.psycopg2_available;

  const service = [
    ['Backend API', error ? 'Unavailable' : 'Reachable', !error],
    ['Evidence store (PostgreSQL)', settings?.evidence_store_connected ? 'Connected' : 'Unavailable', !!settings?.evidence_store_connected],
    ['PostgreSQL driver (server)', installed ? 'Installed' : 'Unavailable', !!installed],
    ['Evidence database URL', settings?.database_configured ? 'Configured' : 'Not configured', !!settings?.database_configured],
    ['Agent ingest key', settings?.ingest_key_configured ? 'Configured' : 'Not configured', !!settings?.ingest_key_configured],
    ['Dashboard access key', settings?.dashboard_key_required ? 'Required' : 'Not set (dashboard is open)', !!settings?.dashboard_key_required],
    ['Devices registered', String(settings?.devices_registered ?? '—'), (settings?.devices_registered || 0) > 0],
    ['PostgreSQL log integration', settings?.postgres_log_integration || '—', false],
    ['History retention', settings?.history_retention || '—', false],
  ];
  const host = [
    ['Device', cur ? `${cur.name || cur.host} (${cur.online ? 'online' : 'offline'})` : '—'],
    ['Hostname', os?.host],
    ['Platform', os?.platform],
    ['CPU cores', os?.cpu_count],
    ['Agent version', cur?.agent_version],
    ['Last report', cur?.last_seen ? fmtDateTime(cur.last_seen) : '—'],
    ['Refresh interval', `${settings?.refresh_seconds || 5} seconds`],
    ['Incident closes after', settings?.incident_grace_seconds ? `${settings.incident_grace_seconds} s without a sighting` : '—'],
    ['Server version', settings?.version],
    ['Safe actions', settings?.safe_actions],
  ];

  return (
    <>
      <PageIntro eyebrow="CONFIGURATION & HEALTH" title="Settings" desc="Inspect service configuration and connection prerequisites without exposing secrets." />
      <div className="settings-grid">
        <section className="card settings-card">
          <h3>Service diagnostics</h3>
          {service.map(([name, value, ok]) => (
            <div className="setting-row" key={name}><span>{name}</span><b className={ok ? 'goodtext' : 'warntext'}>{value}</b></div>
          ))}
        </section>
        <section className="card settings-card">
          <h3>Host information</h3>
          {host.map(([name, value]) => <div className="setting-row" key={name}><span>{name}</span><b>{value || '—'}</b></div>)}
        </section>
      </div>
      <section className="card setup-card">
        <h3>Connect a device</h3>
        <p>
          Run the DBSentinel agent on each Windows laptop you want to monitor. Opening the agent for the first time starts a short guided setup that asks for this
          server's address, the <code>INGEST_KEY</code> and, optionally, the laptop's local PostgreSQL connection. Use a least-privilege role such as <code>pg_monitor</code>
          for PostgreSQL. Never commit credentials.
        </p>
        <button className="secondarybtn" onClick={() => toast('See agent/README.md for the agent setup.')}><ExternalLink size={14} /> Setup instructions</button>
      </section>
    </>
  );
}
