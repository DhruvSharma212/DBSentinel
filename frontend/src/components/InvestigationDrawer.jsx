// Side panel for one incident: what was seen, how long, what to check, and the notes written about it.
import React, { useState } from 'react';
import { saveNote } from '../api.js';
import { guidanceFor } from '../lib/recommendations.js';
import { fmtDateTime, fmtDuration, plural } from '../lib/format.js';
import { Drawer, Pill } from './ui.jsx';

const AUTHOR_STORAGE = 'dbsentinel.author';

function storedAuthor() {
  try { return localStorage.getItem(AUTHOR_STORAGE) || ''; } catch (e) { return ''; }
}

export default function InvestigationDrawer({ incident, notes, deviceId, close, onSaved }) {
  const [note, setNote] = useState('');
  const [author, setAuthor] = useState(storedAuthor);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const guide = guidanceFor(incident.ruleId);
  const related = incident.eventId == null ? [] : notes.filter((n) => n.event_id === incident.eventId);

  async function save() {
    const text = note.trim();
    if (!text) return;
    setSaving(true);
    try {
      const name = author.trim() || 'Local operator';
      await saveNote({ event_id: incident.eventId, device_id: deviceId || null, author: name, note: text });
      try { localStorage.setItem(AUTHOR_STORAGE, author.trim()); } catch (e) { /* optional */ }
      setNote('');
      setMessage('Saved. The note is listed below.');
      onSaved();
    } catch (e) {
      setMessage('Save failed: ' + e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Drawer title="Investigation details" close={close}>
      <span><Pill value={incident.severity} /> <Pill value={incident.active ? 'active' : 'ended'} tone={incident.active ? 'orange' : 'green'} /></span>
      <h2>{incident.title}</h2>
      <p>{incident.detail}</p>

      <div className="detailgrid">
        <div><span>first seen</span><b>{fmtDateTime(incident.firstSeen)}</b></div>
        <div><span>last seen</span><b>{fmtDateTime(incident.lastSeen)}</b></div>
        <div><span>duration</span><b>{fmtDuration(incident.durationSeconds)}</b></div>
        <div><span>seen in</span><b>{incident.snapshots == null ? '—' : plural(incident.snapshots, 'snapshot')}</b></div>
      </div>

      <h4>Observed evidence</h4>
      <pre className="sql">{JSON.stringify(incident.evidence || {}, null, 2)}</pre>

      <h4>Recommended investigation</h4>
      <ul>{guide.steps.map((step) => <li key={step}>{step}</li>)}</ul>

      <h4>Notes on this incident ({related.length})</h4>
      {related.length === 0 && <p className="mutedline">No notes yet.</p>}
      {related.map((n) => (
        <div className="note-item" key={n.id}>
          <div><b>{n.author}</b><span>{fmtDateTime(n.created_at)}</span></div>
          <p>{n.note}</p>
        </div>
      ))}

      <h4>Add a note</h4>
      <input className="author-input" value={author} onChange={(e) => setAuthor(e.target.value)} placeholder="Your name (shown with the note)" maxLength={80} />
      <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Record what you checked, evidence found, and next step…" rows={4} style={{ width: '100%', resize: 'vertical' }} />
      <button className="secondarybtn" disabled={saving || !note.trim()} onClick={save}>{saving ? 'Saving…' : 'Save investigation note'}</button>
      {message && <p className="mutedline">{message}</p>}
      <p className="mutedline">Safe mode: no automatic remediation has been performed.</p>
    </Drawer>
  );
}
