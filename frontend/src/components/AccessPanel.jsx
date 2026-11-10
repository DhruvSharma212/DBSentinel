// Settings > Device access: create one-time enrollment codes, see how each device authenticates, revoke devices.
// Everything here needs the server's ADMIN_KEY, which is asked for once and kept only for this tab.
import React, { useCallback, useEffect, useState } from 'react';
import { Copy, KeyRound, LockKeyhole, Plus } from 'lucide-react';
import { ApiError, API_BASE, adminRequest, getAdminKey, setAdminKey } from '../api.js';
import { fmtDateTime } from '../lib/format.js';
import { Pill, Table } from './ui.jsx';

const REFRESH_MS = 8000;
const ACCESS_TONE = { 'device token': 'green', 'shared key': 'yellow', revoked: 'red' };
const CODE_TONE = { active: 'green', used: 'blue', expired: 'orange', revoked: 'red' };
const LIFETIMES = [[24, '24 hours'], [72, '3 days'], [168, '7 days']];

export default function AccessPanel({ toast }) {
  const [keyInput, setKeyInput] = useState('');
  const [unlocked, setUnlocked] = useState(() => !!getAdminKey());
  const [problem, setProblem] = useState('');
  const [data, setData] = useState({ devices: [], pending: [], codes: [] });
  const [label, setLabel] = useState('');
  const [hours, setHours] = useState(24);
  const [fresh, setFresh] = useState(null);
  const [busy, setBusy] = useState(false);

  const lock = useCallback((message = '') => {
    setAdminKey('');
    setUnlocked(false);
    setFresh(null);
    setProblem(message);
  }, []);

  const load = useCallback(async () => {
    try {
      const [devices, codes] = await Promise.all([adminRequest('/api/admin/devices'), adminRequest('/api/admin/enroll-codes')]);
      setData({ devices: devices.devices, pending: devices.pending, codes: codes.codes });
      setProblem('');
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) lock('That admin key was not accepted.');
      else setProblem(e.message);
    }
  }, [lock]);

  useEffect(() => {
    if (!unlocked) return undefined;
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [unlocked, load]);

  async function create(event) {
    event.preventDefault();
    setBusy(true);
    try {
      setFresh(await adminRequest('/api/admin/enroll-codes', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label: label.trim(), ttl_hours: hours }),
      }));
      setLabel('');
      load();
    } catch (e) {
      setProblem(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function act(path, options, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      await adminRequest(path, options);
      load();
    } catch (e) {
      setProblem(e.message);
    }
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast('Copied.'); } catch (e) { toast('Copy failed: select the text and copy it.'); }
  }

  if (!unlocked) {
    return (
      <section className="card setup-card access-panel">
        <h3><KeyRound size={15} /> Device access</h3>
        <p>Give each computer its own one-time enrollment code instead of sharing a key. Administration is protected by the server&apos;s <code>ADMIN_KEY</code>.</p>
        <form className="inline-form" onSubmit={(e) => { e.preventDefault(); if (keyInput.trim()) { setAdminKey(keyInput.trim()); setKeyInput(''); setUnlocked(true); } }}>
          <input type="password" value={keyInput} onChange={(e) => setKeyInput(e.target.value)} placeholder="Admin key" autoComplete="off" />
          <button className="secondarybtn" type="submit" disabled={!keyInput.trim()}>Unlock</button>
        </form>
        {problem && <p className="warntext">{problem}</p>}
      </section>
    );
  }

  const server = API_BASE || window.location.origin;
  const packageCommand = fresh
    ? `powershell -ExecutionPolicy Bypass -File .\\make-package.ps1 -Server ${server} -Code ${fresh.code} -Name "${fresh.label || 'Friend'}"`
    : '';

  return (
    <section className="card setup-card access-panel">
      <div className="section-head">
        <div><h3><KeyRound size={15} /> Device access</h3><p>One-time enrollment codes, per-device tokens and revoking</p></div>
        <button className="textbtn" onClick={() => lock()}><LockKeyhole size={13} /> Lock</button>
      </div>
      {problem && (
        <p className="warntext">{/ADMIN_KEY/.test(problem) ? 'Enrollment is switched off: set ADMIN_KEY on the server (see the README), then reload this page.' : problem}</p>
      )}

      <form className="inline-form" onSubmit={create}>
        <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Who is it for? (becomes the device name)" maxLength={80} />
        <select value={hours} onChange={(e) => setHours(Number(e.target.value))} title="How long the code stays valid">
          {LIFETIMES.map(([value, text]) => <option key={value} value={value}>valid {text}</option>)}
        </select>
        <button className="secondarybtn" type="submit" disabled={busy}><Plus size={13} /> Create code</button>
      </form>

      {fresh && (
        <div className="code-box">
          <span>One-time code for {fresh.label || 'a new device'} · valid until {fmtDateTime(fresh.expires_at)} · shown only now</span>
          <div className="code-row">
            <b className="code-value">{fresh.code}</b>
            <button className="smallbtn" onClick={() => copy(fresh.code)}><Copy size={12} /> Copy</button>
          </div>
          <p>Either type it into the agent&apos;s setup (<code>Enrollment code</code>), or build a package that needs no typing:</p>
          <div className="code-row">
            <code className="command">{packageCommand}</code>
            <button className="smallbtn" onClick={() => copy(packageCommand)}><Copy size={12} /> Copy</button>
          </div>
        </div>
      )}

      <h4>Devices</h4>
      <Table
        headers={['DEVICE', 'STATUS', 'ACCESS', 'LAST REPORT', 'ACTION']}
        rows={[
          ...data.devices.map((d) => [
            <b>{d.name || d.host}</b>,
            <Pill value={d.online ? 'online' : 'offline'} tone={d.online ? 'green' : 'orange'} />,
            <Pill value={d.access} tone={ACCESS_TONE[d.access]} />,
            fmtDateTime(d.last_seen),
            d.access === 'device token'
              ? <button className="smallbtn danger" onClick={() => act(`/api/admin/devices/${encodeURIComponent(d.device_id)}/revoke`, { method: 'POST' },
                `Revoke access for ${d.name || d.host}? It stops reporting immediately. Its stored data is kept.`)}>Revoke</button>
              : '—',
          ]),
          ...data.pending.map((p) => [
            <b>{p.label || p.device_id}</b>, <Pill value="not reported yet" tone="blue" />, <Pill value="device token" tone="green" />, '—',
            <button className="smallbtn danger" onClick={() => act(`/api/admin/devices/${encodeURIComponent(p.device_id)}/revoke`, { method: 'POST' }, 'Revoke this enrolled device?')}>Revoke</button>,
          ]),
        ]}
        empty="No devices yet"
      />

      <h4>Enrollment codes</h4>
      <Table
        headers={['FOR', 'STATUS', 'CREATED', 'EXPIRES / USED', 'ACTION']}
        rows={data.codes.map((c) => [
          <b>{c.label || '—'}</b>,
          <Pill value={c.status} tone={CODE_TONE[c.status]} />,
          fmtDateTime(c.created_at),
          c.status === 'used' ? fmtDateTime(c.used_at) : fmtDateTime(c.expires_at),
          c.status === 'active' ? <button className="smallbtn danger" onClick={() => act(`/api/admin/enroll-codes/${c.id}`, { method: 'DELETE' }, 'Cancel this unused code?')}>Cancel</button> : '—',
        ])}
        empty="No codes created yet"
      />
    </section>
  );
}
