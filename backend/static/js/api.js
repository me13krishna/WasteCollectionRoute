/**
 * EcoRoute - REST client
 */

const API_BASE = '/api/v1';

async function request(path, { method = 'GET', json, body, fallback } = {}) {
  const opts = { method };
  if (json !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(json);
  } else if (body !== undefined) {
    opts.body = body;
  }

  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, opts);
  } catch (e) {
    throw new Error('Server not reachable. Is it running?');
  }

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    let msg = err?.error?.message || err?.detail;
    if (Array.isArray(msg)) msg = msg.map(d => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join('; ');
    throw new Error(msg || fallback || `Request failed (${res.status})`);
  }
  return res.json();
}

/** POST that answers with newline-delimited JSON; `onEvent` fires for each line as it arrives. */
async function streamNdjson(path, json, onEvent, signal) {
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(json), signal
    });
  } catch (e) {
    if (e.name === 'AbortError') return;
    throw new Error('Server not reachable. Is it running?');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.error?.message || err?.detail || `Request failed (${res.status})`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
}

export const api = {
  getConfig: () => request('/config', { fallback: 'Could not load configuration' }),
  getRoutingStatus: () => request('/routing/status'),
  getDataset: () => request('/dataset', { fallback: 'Could not load collection points' }),
  getTrafficStatus: () => request('/traffic/status'),

  getScenarios: () => request('/scenarios'),
  randomizePoints: (payload) => request('/dataset/randomize', { method: 'POST', json: payload, fallback: 'Could not generate random points' }),
  fleetSweep: (payload) => request('/optimize/fleet-sweep', { method: 'POST', json: payload, fallback: 'Truck comparison failed' }),
  fleetStream: (payload, onEvent, signal) => streamNdjson('/optimize/fleet-sweep/stream', payload, onEvent, signal),
  runOptimization: (payload) => request('/optimize', { method: 'POST', json: payload, fallback: 'Optimization failed' }),
  reportIncident: (payload) => request('/incidents', { method: 'POST', json: payload, fallback: 'Could not report incident' }),
  undoIncident: () => request('/incidents/undo', { method: 'POST', fallback: 'Could not undo incident' }),
  resetIncidents: () => request('/incidents/reset', { method: 'POST', fallback: 'Could not reset' }),
  resetDataset: () => request('/reset-dataset', { method: 'POST', fallback: 'Could not restore sample data' }),

  extractOsm: (payload) => request('/osm/extract', { method: 'POST', json: payload, fallback: 'OpenStreetMap import failed' }),
  uploadCsv: (file) => {
    const fd = new FormData();
    fd.append('file', file);
    return request('/upload-csv', { method: 'POST', body: fd, fallback: 'CSV upload failed' });
  }
};
