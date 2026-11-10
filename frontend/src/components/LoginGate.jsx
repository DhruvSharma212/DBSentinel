// Shown when the server requires a dashboard key (DASHBOARD_KEY is set on the API).
import React, { useState } from 'react';
import { Shield } from 'lucide-react';

export default function LoginGate({ onSubmit, rejected }) {
  const [key, setKey] = useState('');
  return (
    <div className="login-gate">
      <form className="card setup-card login-card" onSubmit={(e) => { e.preventDefault(); if (key.trim()) onSubmit(key.trim()); }}>
        <div className="brandmark"><Shield size={22} /><span /></div>
        <h3>DBSentinel is protected</h3>
        <p>Enter the dashboard key you were given to view this dashboard. It is kept only until you close this tab.</p>
        <input type="password" autoFocus value={key} onChange={(e) => setKey(e.target.value)} placeholder="Dashboard key" autoComplete="current-password" />
        {rejected && <p className="warntext">That key was not accepted. After several wrong attempts the server pauses for a minute.</p>}
        <button className="secondarybtn" type="submit" disabled={!key.trim()}>Unlock</button>
      </form>
    </div>
  );
}
