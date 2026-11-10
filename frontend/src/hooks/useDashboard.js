// All data loading for the dashboard: which devices exist, the selected device's data, and polling.
// Two requests per refresh (/api/devices and /api/dashboard); settings are only fetched on the Settings page.
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, request, setDashboardKey } from '../api.js';

const POLL_MS = 5000;
const DEVICE_STORAGE = 'dbsentinel.device';

// The wanted device if it still exists, else the first online one, else the first one.
export function pickDevice(list, wanted) {
  return (list.find((d) => d.device_id === wanted) || list.find((d) => d.online) || list[0] || {}).device_id || '';
}

function readStoredDevice() {
  try { return localStorage.getItem(DEVICE_STORAGE) || ''; } catch (e) { return ''; }
}

export function useDashboard(page) {
  const [devices, setDevices] = useState([]);
  const [deviceId, setDeviceId] = useState(readStoredDevice);
  const [data, setData] = useState(null);
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState('');
  const [needsKey, setNeedsKey] = useState(false);
  const [keyRejected, setKeyRejected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [version, setVersion] = useState('');

  // Refs let the long-lived polling loop always see the latest values without being restarted.
  const deviceRef = useRef(deviceId);
  const pageRef = useRef(page);
  const needsKeyRef = useRef(false);
  const triedKeyRef = useRef(false);
  pageRef.current = page;

  const requireKey = useCallback((value) => { needsKeyRef.current = value; setNeedsKey(value); }, []);

  const chooseDevice = useCallback((id) => {
    deviceRef.current = id;
    setDeviceId(id);
    try { localStorage.setItem(DEVICE_STORAGE, id); } catch (e) { /* remembering the choice is optional */ }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const list = await request('/api/devices');
      const all = list.devices || [];
      setDevices(all);
      if (list.version) setVersion(list.version);
      const id = pickDevice(all, deviceRef.current);
      if (id !== deviceRef.current) chooseDevice(id);
      const wantsSettings = pageRef.current === 'Settings';
      const nextSettings = wantsSettings ? await request('/api/settings') : null;
      if (!id) {
        setData(null);
      } else {
        const next = await request('/api/dashboard?device_id=' + encodeURIComponent(id));
        if (id !== deviceRef.current) return; // the user switched device while this request was in flight
        setData(next);
      }
      if (nextSettings) setSettings(nextSettings);
      setError('');
      requireKey(false);
      setKeyRejected(false);
      triedKeyRef.current = false;
      setLastUpdated(new Date());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setKeyRejected(triedKeyRef.current); // a key was entered and the server said no
        requireKey(true);
        setError('');
      } else {
        setError(e.message);
      }
    } finally {
      setLoading(false);
    }
  }, [chooseDevice, requireKey]);

  // Poll every 5 s while the tab is visible and no key is needed; restart when the page changes.
  useEffect(() => {
    let stopped = false;
    let timer;
    const tick = async () => {
      if (stopped) return;
      if (!document.hidden && !needsKeyRef.current) await refresh();
      if (!stopped) timer = setTimeout(tick, POLL_MS);
    };
    tick();
    const onVisible = () => { if (!document.hidden) { clearTimeout(timer); tick(); } };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [page, refresh]);

  const switchDevice = useCallback((id) => {
    chooseDevice(id);
    setData(null);
    refresh();
  }, [chooseDevice, refresh]);

  const unlock = useCallback((key) => {
    setDashboardKey(key);
    triedKeyRef.current = true;
    requireKey(false);
    setLoading(true);
    refresh();
  }, [refresh, requireKey]);

  const lock = useCallback(() => {
    setDashboardKey('');
    triedKeyRef.current = false;
    setData(null);
    setDevices([]);
    requireKey(true);
  }, [requireKey]);

  const cur = devices.find((d) => d.device_id === deviceId);
  return { devices, deviceId, cur, data, settings, error, needsKey, keyRejected, loading, lastUpdated, version,
    refresh, switchDevice, unlock, lock };
}
