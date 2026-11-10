import React from 'react';
import { API_BASE } from '../api.js';
import { PageIntro } from '../components/ui.jsx';

export default function NoDevice() {
  return (
    <>
      <PageIntro
        eyebrow="WAITING FOR DATA"
        title="No device connected yet"
        desc="DBSentinel shows the machine that runs the DBSentinel agent. Start the agent and it appears here within a few seconds."
      />
      <section className="card setup-card">
        <h3>Connect a device</h3>
        <p>1. On the Windows laptop you want to monitor, open <code>dbsentinel-agent.exe</code>. With no configuration yet it starts a short guided setup (or run it with <code>--setup</code>).</p>
        <p>2. Enter this server's address, <code>{API_BASE || window.location.origin}</code>, and the server's <code>INGEST_KEY</code>. Optionally add the laptop's local PostgreSQL connection.</p>
        <p>3. Let the agent run. This page refreshes every 5 seconds and the device then appears in the top bar.</p>
      </section>
    </>
  );
}
