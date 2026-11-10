import React, { useCallback, useEffect, useState } from 'react';
import {
  Activity, AlertTriangle, Bell, CheckCircle2, ChevronDown, Clock3, Database, GitMerge, LockKeyhole, Menu, Monitor,
  RefreshCw, ScanSearch, ScrollText, Settings as SettingsIcon, Shield, ShieldCheck, X,
} from 'lucide-react';
import { getDashboardKey } from './api.js';
import { useDashboard } from './hooks/useDashboard.js';
import { InfoBanner } from './components/ui.jsx';
import LastUpdated from './components/LastUpdated.jsx';
import LoginGate from './components/LoginGate.jsx';
import Overview from './pages/Overview.jsx';
import OSMonitor from './pages/OSMonitor.jsx';
import DatabaseMonitor from './pages/DatabaseMonitor.jsx';
import EventCorrelation from './pages/EventCorrelation.jsx';
import Incidents from './pages/Incidents.jsx';
import SafeResponse from './pages/SafeResponse.jsx';
import AuditTrail from './pages/AuditTrail.jsx';
import SettingsPage from './pages/SettingsPage.jsx';
import NoDevice from './pages/NoDevice.jsx';

const PAGES = {
  'Overview': Overview,
  'OS Monitor': OSMonitor,
  'Database Monitor': DatabaseMonitor,
  'Event Correlation': EventCorrelation,
  'Incidents & Forensics': Incidents,
  'Safe Response': SafeResponse,
  'Audit Trail': AuditTrail,
  'Settings': SettingsPage,
};

const NAV = [
  ['Overview', Activity], ['OS Monitor', Monitor], ['Database Monitor', Database], ['Event Correlation', GitMerge],
  ['Incidents & Forensics', ScanSearch], ['Safe Response', ShieldCheck], ['Audit Trail', ScrollText], ['Settings', SettingsIcon],
];

function Sidebar({ page, go, open, close, dash, alertCount }) {
  const { cur } = dash;
  return (
    <aside className={'sidebar ' + (open ? 'show' : '')}>
      <div className="brand">
        <div className="brandmark"><Shield size={22} /><span /></div>
        <div><b>DB<span>Sentinel</span></b><small>SECURITY OBSERVABILITY</small></div>
        <button className="mobile-close iconbtn" onClick={close}><X /></button>
      </div>
      <div className="workspace">
        <div className="workspace-avatar">DS</div>
        <div>
          <b>{cur ? (cur.name || cur.host) : 'No device'}</b>
          <span>{cur ? (cur.online ? 'Agent online' : 'Agent offline') : 'Waiting for an agent'}</span>
        </div>
        <ChevronDown size={14} />
      </div>
      <div className="nav-label">MONITORING</div>
      <nav>
        {NAV.map(([name, Icon]) => (
          <button key={name} className={'navitem ' + (page === name ? 'active' : '')} onClick={() => go(name)}>
            <Icon size={17} /><span>{name}</span>
            {name === 'Incidents & Forensics' && alertCount > 0 && <em>{alertCount}</em>}
          </button>
        ))}
      </nav>
      <div className="sidebar-bottom">
        <div className="secure-box">
          <div><span className="secure-icon"><LockKeyhole size={15} /></span><b>Safe mode</b><span className="pill green">ON</span></div>
          <p>Response actions require review. No destructive commands are enabled.</p>
        </div>
      </div>
    </aside>
  );
}

function Header({ page, dash, go, openMenu, alertCount }) {
  const { devices, deviceId, cur, error, lastUpdated, refresh, switchDevice, lock } = dash;
  const badgeWarn = error || !cur || !cur.online;
  const badgeText = error ? 'API disconnected' : !cur ? 'No agent connected' : cur.online ? 'Agent online' : 'Agent offline';
  return (
    <header className="topbar">
      <button className="mobile-menu iconbtn" onClick={openMenu}><Menu /></button>
      <div className="crumb"><span>Workspace</span><b>/</b><strong>{page}</strong></div>
      <div className="top-actions">
        {devices.length > 0 && (
          <select className="devicesel" value={deviceId} onChange={(e) => switchDevice(e.target.value)} title="Monitored device">
            {devices.map((d) => <option key={d.device_id} value={d.device_id}>{(d.online ? '● ' : '○ ') + (d.name || d.host)}</option>)}
          </select>
        )}
        <div className={'live' + (badgeWarn ? ' warn' : '')}><i />{badgeText}</div>
        <span className="updated"><Clock3 size={14} /> Updated <LastUpdated date={lastUpdated} /></span>
        <button className="iconbtn" onClick={refresh} title="Refresh data"><RefreshCw size={17} /></button>
        <button className="iconbtn" onClick={() => go('Incidents & Forensics')} title="Active conditions">
          <Bell size={17} />{alertCount > 0 && <em className="dotcount">{alertCount}</em>}
        </button>
        {getDashboardKey() && <button className="iconbtn" onClick={lock} title="Lock the dashboard"><LockKeyhole size={17} /></button>}
      </div>
    </header>
  );
}

export default function App() {
  const [page, setPage] = useState('Overview');
  const [menuOpen, setMenuOpen] = useState(false);
  const [notice, setNotice] = useState('');
  const dash = useDashboard(page);

  const toast = useCallback((text) => {
    setNotice(text);
    setTimeout(() => setNotice(''), 2600);
  }, []);
  const go = (name) => { setPage(name); setMenuOpen(false); };

  const alertCount = dash.data?.alerts?.length || 0;
  useEffect(() => { document.title = (alertCount ? `(${alertCount}) ` : '') + 'DBSentinel'; }, [alertCount]);

  if (dash.needsKey) return <LoginGate onSubmit={dash.unlock} rejected={dash.keyRejected} />;

  const { cur, error, loading, devices, version } = dash;
  const Page = PAGES[page] || Overview;
  const noDevice = !loading && !error && devices.length === 0 && page !== 'Settings';

  return (
    <div className="app">
      <Sidebar page={page} go={go} open={menuOpen} close={() => setMenuOpen(false)} dash={dash} alertCount={alertCount} />
      {menuOpen && <div className="scrim" onClick={() => setMenuOpen(false)} />}
      <main className="main">
        <Header page={page} dash={dash} go={go} openMenu={() => setMenuOpen(true)} alertCount={alertCount} />
        <div className="content">
          {error && (
            <div className="error-banner"><AlertTriangle size={16} /><span>{error}</span><button onClick={dash.refresh}>Retry</button></div>
          )}
          {loading && !error && (
            <InfoBanner icon={RefreshCw} title="Connecting to the API…">The free hosting tier can take up to a minute to wake up after being idle.</InfoBanner>
          )}
          {!error && cur && !cur.online && (
            <div className="error-banner">
              <AlertTriangle size={16} />
              <span>Agent offline — {cur.name || cur.host} last reported {new Date(cur.last_seen).toLocaleString()}. Showing the last known values.</span>
            </div>
          )}
          {noDevice ? <NoDevice /> : <Page key={dash.deviceId} dash={dash} go={go} toast={toast} />}
          <footer>
            <span>DBSentinel <b>{version ? 'v' + version : ''}</b></span>
            <span><i /> Live refresh · 5s</span>
            <span className="mono">{cur ? cur.device_id : ''}</span>
          </footer>
        </div>
      </main>
      {notice && <div className="toast"><CheckCircle2 size={17} />{notice}</div>}
    </div>
  );
}
