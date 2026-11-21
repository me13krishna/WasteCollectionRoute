/**
 * EcoRoute - drivers & shift playback
 * One marker per driver. Markers move along their road geometry, timed by each
 * route's planned duration. While parked at the depot they fan out so every
 * driver stays clickable.
 */

import { esc } from './util.js';

const SLIDER_MAX = 1000;
const TICK_MS = 50;
const BASE_PLAYBACK_SEC = 60; // a full shift takes 60 s at 1x

let timer = null;
let playing = false;
let elapsed = 0;      // simulated seconds into the shift
let shiftLength = 0;  // longest route duration, seconds
let speed = 5;
let drivers = [];
let mapRef = null;
let focusedId = null;
const listeners = { click: null, tick: null };

function segLen(a, b) {
  const x = (b[0] - a[0]) * Math.cos(((a[1] + b[1]) / 2) * Math.PI / 180);
  const y = b[1] - a[1];
  return Math.sqrt(x * x + y * y);
}

/** Point at a given distance along the geometry. */
function pointAtDist(d, target) {
  const g = d.geometry;
  if (target <= 0 || g.length === 1) return g[0];
  if (target >= d.length) return g[g.length - 1];
  let lo = 0, hi = d.cum.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (d.cum[mid] < target) lo = mid + 1; else hi = mid;
  }
  const i = Math.max(1, lo);
  const span = d.cum[i] - d.cum[i - 1] || 1;
  const t = (target - d.cum[i - 1]) / span;
  const a = g[i - 1], b = g[i];
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
}

/**
 * Build a timetable of (time, distance-along-geometry) keyframes from the stops'
 * planned arrival/departure, so the marker waits at each stop while servicing
 * it and drives between stops in between. Matches the journey timeline.
 */
function buildKeyframes(d, stops) {
  const g = d.geometry;
  const frames = [];
  let gi = 0;
  stops.forEach((s, k) => {
    // Nearest geometry vertex to this stop, searching forward (routes revisit areas).
    let best = gi, bestD = Infinity;
    const end = k === stops.length - 1 ? g.length : Math.min(g.length, gi + 4000);
    for (let i = gi; i < end; i++) {
      const dd = segLen(g[i], [s.lon, s.lat]);
      if (dd < bestD) { bestD = dd; best = i; }
    }
    gi = best;
    const dist = d.cum[best];
    const arr = s.arrival_sec || 0;
    const dep = Math.max(arr, s.departure_sec ?? arr);
    frames.push({ t: arr, dist });
    if (dep > arr) frames.push({ t: dep, dist });
  });
  return frames.length >= 2 ? frames : null;
}

function positionAt(d, t) {
  const f = d.frames;
  if (!f) return pointAtDist(d, (Math.min(1, Math.max(0, t / d.duration))) * d.length);
  if (t <= f[0].t) return pointAtDist(d, f[0].dist);
  if (t >= f[f.length - 1].t) return pointAtDist(d, f[f.length - 1].dist);
  let i = 1;
  while (i < f.length && f[i].t < t) i++;
  const a = f[i - 1], b = f[i];
  const r = b.t > a.t ? (t - a.t) / (b.t - a.t) : 1;
  return pointAtDist(d, a.dist + (b.dist - a.dist) * r);
}

export function initials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

function driverIcon(d, parked) {
  // Parked drivers fan out in a row above the depot.
  const dx = parked ? (d.index - (drivers.length - 1) / 2) * 32 : 0;
  const dy = parked ? 28 : 0;
  const focused = focusedId === d.id;
  const dimmed = focusedId != null && !focused;
  return L.divIcon({
    className: 'mk',
    html: `<div class="mk-driver${focused ? ' is-focused' : ''}${dimmed ? ' is-dimmed' : ''}" style="--c:${d.color}">
             <span>${esc(initials(d.driver))}</span>
             ${focused ? `<em>${esc(d.driver)}</em>` : ''}
           </div>`,
    iconSize: [28, 28],
    iconAnchor: [14 - dx, 14 + dy]
  });
}

function setButton() {
  const btn = document.getElementById('sim-play-btn');
  if (!btn) return;
  btn.classList.toggle('playing', playing);
  btn.title = playing ? 'Pause' : 'Play shift';
  btn.setAttribute('aria-label', btn.title);
}

export const simulation = {
  get isPlaying() { return playing; },
  get elapsed() { return elapsed; },

  on(event, fn) { listeners[event] = fn; },

  init(map, routes) {
    this.stop();
    mapRef = map;

    drivers = routes
      .filter(r => (r.geometry || []).length > 0 || (r.stops || []).length > 0)
      .map((r, index) => {
        const geometry = (r.geometry && r.geometry.length) ? r.geometry : r.stops.map(s => [s.lon, s.lat]);
        const cum = [0];
        for (let i = 1; i < geometry.length; i++) cum.push(cum[i - 1] + segLen(geometry[i - 1], geometry[i]));
        const d = {
          id: r.vehicle_id,
          index,
          driver: r.driver || r.vehicle_name,
          color: r.color || '#15803d',
          geometry, cum,
          length: cum[cum.length - 1],
          duration: r.total_duration_sec || 1,
          parked: true,
          marker: null,
          frames: null
        };
        if (r.geometry && r.geometry.length > 1 && (r.stops || []).length > 1) d.frames = buildKeyframes(d, r.stops);
        const start = geometry[0];
        d.marker = L.marker([start[1], start[0]], {
          icon: driverIcon(d, true),
          zIndexOffset: 1200,
          title: d.driver,
          riseOnHover: true
        })
          .on('click', () => listeners.click?.(d.id))
          .addTo(map);
        return d;
      });

    shiftLength = Math.max(1, ...drivers.map(d => d.duration));
    elapsed = 0;

    const btn = document.getElementById('sim-play-btn');
    const slider = document.getElementById('sim-progress-slider');
    if (btn) btn.disabled = drivers.length === 0;
    if (slider) { slider.disabled = drivers.length === 0; slider.max = SLIDER_MAX; }
    this.render();
  },

  setFocus(id) {
    focusedId = id;
    drivers.forEach(d => d.marker.setIcon(driverIcon(d, d.parked)));
  },

  play() {
    if (playing || drivers.length === 0) return;
    if (elapsed >= shiftLength) elapsed = 0;
    playing = true;
    setButton();
    timer = setInterval(() => {
      elapsed += (shiftLength / (BASE_PLAYBACK_SEC * 1000 / TICK_MS)) * speed;
      if (elapsed >= shiftLength) {
        elapsed = shiftLength;
        this.pause();
      }
      this.render();
    }, TICK_MS);
  },

  pause() {
    playing = false;
    clearInterval(timer);
    timer = null;
    setButton();
    listeners.tick?.(elapsed);
  },

  toggle() {
    playing ? this.pause() : this.play();
  },

  stop() {
    this.pause();
    elapsed = 0;
    drivers.forEach(d => mapRef?.removeLayer(d.marker));
    drivers = [];
    this.render();
  },

  setSpeed(v) {
    speed = parseInt(v, 10) || 1;
  },

  seek(sliderValue) {
    elapsed = (parseInt(sliderValue, 10) / SLIDER_MAX) * shiftLength;
    this.render();
  },

  /** Jump the playback clock to a given time into the shift. */
  seekTo(seconds) {
    elapsed = Math.max(0, Math.min(shiftLength, seconds));
    this.render();
  },

  render() {
    drivers.forEach(d => {
      const f = elapsed / d.duration;
      const parked = f <= 0 || f >= 1;
      if (parked !== d.parked) {
        d.parked = parked;
        d.marker.setIcon(driverIcon(d, parked));
      }
      const pt = positionAt(d, Math.min(elapsed, d.duration));
      if (pt) d.marker.setLatLng([pt[1], pt[0]]);
    });

    const slider = document.getElementById('sim-progress-slider');
    if (slider) slider.value = shiftLength > 1 ? Math.round((elapsed / shiftLength) * SLIDER_MAX) : 0;

    const label = document.getElementById('sim-time-label');
    if (label) {
      const mins = Math.round(elapsed / 60);
      label.textContent = `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`;
    }
    listeners.tick?.(elapsed);
  }
};
