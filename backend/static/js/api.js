/**
 * EcoRoute AI - API Client Module
 * Provides asynchronous communication methods with backend REST API.
 */

const API_BASE = '/api/v1';

export const api = {
  async getConfig() {
    const res = await fetch(`${API_BASE}/config`);
    if (!res.ok) throw new Error('Failed to fetch system config');
    return res.json();
  },

  async getRoutingStatus() {
    const res = await fetch(`${API_BASE}/routing/status`);
    if (!res.ok) throw new Error('Failed to fetch routing engine status');
    return res.json();
  },

  async getDataset() {
    const res = await fetch(`${API_BASE}/dataset`);
    if (!res.ok) throw new Error('Failed to fetch dataset');
    return res.json();
  },

  async getTrafficStatus() {
    const res = await fetch(`${API_BASE}/traffic/status`);
    if (!res.ok) throw new Error('Failed to fetch traffic conditions');
    return res.json();
  },

  async runOptimization(payload) {
    const res = await fetch(`${API_BASE}/optimize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err?.error?.message || err?.detail || 'Optimization failed');
    }
    return res.json();
  },

  async reportIncident(payload) {
    const res = await fetch(`${API_BASE}/incidents`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err?.error?.message || err?.detail || 'Incident reporting failed');
    }
    return res.json();
  },

  async undoIncident() {
    const res = await fetch(`${API_BASE}/incidents/undo`, {
      method: 'POST'
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err?.detail || 'Could not undo incident');
    }
    return res.json();
  },

  async resetIncidents() {
    const res = await fetch(`${API_BASE}/incidents/reset`, {
      method: 'POST'
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err?.detail || 'Could not reset dataset');
    }
    return res.json();
  },

  async extractOsm(payload) {
    const res = await fetch(`${API_BASE}/osm/extract`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err?.detail || 'OpenStreetMap extraction failed');
    }
    return res.json();
  },

  async uploadCsv(file) {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch(`${API_BASE}/upload-csv`, {
      method: 'POST',
      body: formData
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err?.detail || 'CSV upload failed');
    }
    return res.json();
  }
};
