// Talking to the DBSentinel API.
// VITE_API_URL is baked in at build time. When it is empty the page calls /api on its own domain
// (in development the Vite server proxies that to the backend).
export const API_BASE = import.meta.env.VITE_API_URL || '';

const KEY_STORAGE = 'dbsentinel.dashboardKey';

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

// The optional dashboard key lives in sessionStorage: it is forgotten when the tab is closed.
export function getDashboardKey() {
  try { return sessionStorage.getItem(KEY_STORAGE) || ''; } catch (e) { return ''; }
}

export function setDashboardKey(key) {
  try {
    if (key) sessionStorage.setItem(KEY_STORAGE, key);
    else sessionStorage.removeItem(KEY_STORAGE);
  } catch (e) { /* storage unavailable: the key just will not be remembered */ }
}

export async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const key = getDashboardKey();
  if (key) headers['X-Dashboard-Key'] = key;
  let response;
  try {
    response = await fetch(API_BASE + path, { ...options, headers });
  } catch (e) {
    throw new ApiError('Cannot reach the API. Check that the backend is running and the address is correct.', 0);
  }
  if (!response.ok) {
    let detail = '';
    try { detail = (await response.json()).detail; } catch (e) { /* body was not JSON */ }
    throw new ApiError(typeof detail === 'string' && detail ? detail : `${response.status} ${response.statusText}`, response.status);
  }
  return response.json();
}

export function saveNote(note) {
  return request('/api/investigation-notes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(note),
  });
}
