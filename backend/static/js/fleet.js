/**
 * EcoRoute - optimal fleet finder.
 *
 * Plans 1, 2, 3 … trucks on the current points. Each plan is streamed from the server as soon as
 * it is solved, then its roads fill in on the map while the next count is still being computed.
 * The search stops one truck past the minimum distance, so the chart shows the full U: the
 * optimum is the bottom of the U and the last point is "optimal + 1".
 */

import { api } from './api.js';
import { mapManager } from './map.js';
import { charts } from './charts.js';
import { initials } from './simulation.js';
import { esc, fmt } from './util.js';

const $ = (id) => document.getElementById(id);
const hhmm = (min) => `${Math.floor(min / 60)}h ${String(Math.round(min % 60)).padStart(2, '0')}m`;
const plural = (n) => `${n} truck${n === 1 ? '' : 's'}`;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const FILL_MS = 3200;   // how long one truck count takes to fill in
const CYCLE_PAUSE_MS = 900;

export const fleetFinder = {
  runs: [],
  best: null,        // truck count at the minimum
  maxTrucks: 0,
  shown: null,       // truck count currently on the map
  done: false,
  finalized: false,
  queue: [],
  animating: false,
  token: 0,          // bumped on reset: stale async work stops itself
  cycleId: 0,        // bumped to stop the "show every count" loop
  abort: null,
  hooks: { prepare() {}, paintStops() {}, currency: () => '₹' },

  bind(hooks) {
    this.hooks = { ...this.hooks, ...hooks };
    $('uc-show-all').addEventListener('change', () => this.onToggle());
    $('ucChart').addEventListener('ucurve-pick', (e) => this.pick(e.detail));
  },

  /** Drop everything (new points, new run). */
  reset() {
    this.token += 1;
    this.cycleId += 1;
    this.abort?.abort();
    this.abort = null;
    Object.assign(this, { runs: [], best: null, shown: null, done: false, finalized: false, queue: [], animating: false });
    mapManager.fleetClear();
    $('uc-body').hidden = true;
  },

  async start({ maxTrucks, timeLimit, traffic }) {
    this.reset();
    this.hooks.prepare();
    this.maxTrucks = maxTrucks;
    const tok = this.token;
    this.abort = new AbortController();

    $('uc-body').hidden = false;
    $('uc-kpis').innerHTML = '';
    $('uc-trucks').innerHTML = '';
    this.setStatus('Planning', 'Solving 1 truck…', 'Each plan appears on the map as soon as it is ready.');
    charts.renderUCurve('ucChart', [], null, maxTrucks, { reset: true });

    await api.fleetStream(
      { max_trucks: maxTrucks, time_limit_sec: timeLimit, traffic_enabled: traffic, stop_at_optimal: true },
      (ev) => { if (tok === this.token) this.onEvent(ev); },
      this.abort.signal
    );
  },

  onEvent(ev) {
    if (ev.type === 'start') {
      this.maxTrucks = ev.max_trucks;
      charts.renderUCurve('ucChart', [], null, this.maxTrucks, { reset: true });
    } else if (ev.type === 'run') {
      this.runs.push(ev.run);
      charts.renderUCurve('ucChart', this.runs, null, this.maxTrucks);
      this.queue.push(ev.run);
      this.pump();
    } else if (ev.type === 'done') {
      this.done = true;
      this.best = ev.best_trucks;
      this.pump();
    }
  },

  /** Plays queued plans one after another; finishes the search once the queue is empty. */
  async pump() {
    if (this.animating) return;
    this.animating = true;
    const tok = this.token;
    while (this.queue.length && tok === this.token) {
      await this.show(this.queue.shift(), { animate: true });
      if (tok === this.token && !this.done && !this.queue.length) {
        this.setStatus('Planning', `Solving ${plural(this.runs.length + 1)}…`, `${plural(this.runs.length)} done. Waiting for the next plan.`);
      }
    }
    this.animating = false;
    if (tok === this.token && this.done && !this.queue.length && !this.finalized) await this.finalize();
  },

  async show(run, { animate }) {
    const tok = this.token;
    this.shown = run.trucks;
    this.hooks.paintStops(null);
    this.renderRun(run);
    charts.renderUCurve('ucChart', this.runs, this.finalized ? this.best : null, this.axisMax(), { selected: run.trucks });
    if (!this.finalized) {
      this.setStatus('Planning', `${plural(run.trucks)} · ${fmt(run.total_distance_km)} km`, 'Laying out the roads for each truck…');
    }
    await mapManager.fleetPlay(run.routes, { duration: animate ? FILL_MS : 0 });
    if (tok === this.token) this.hooks.paintStops(run);
  },

  /** Bars shown on the chart: everything tested once the search has ended. */
  axisMax() {
    return this.finalized && this.runs.length ? Math.max(...this.runs.map(r => r.trucks)) : this.maxTrucks;
  },

  async finalize() {
    this.finalized = true;
    const best = this.runs.find(r => r.trucks === this.best);
    const after = this.runs.find(r => r.trucks === this.best + 1);
    if (!best) {
      this.setStatus('No feasible plan', 'Every count failed', 'No truck count served all the points within the shift limit.', true);
      return;
    }
    const worse = after ? `${plural(after.trucks)} drives ${fmt(after.total_distance_km - best.total_distance_km)} km more` : 'Fleet limit reached';
    this.setStatus(
      'Optimal fleet',
      `${plural(best.trucks)} · ${fmt(best.total_distance_km)} km`,
      `Too few trucks mean long routes, unload trips and overtime; too many each add their own trip out to the area. ${worse}.`
    );
    charts.renderUCurve('ucChart', this.runs, this.best, this.axisMax(), { selected: this.best });
    if ($('uc-show-all').checked) await this.cycle();
    else await this.show(best, { animate: true });
  },

  /* ---------- Show every count, 1 → optimal + 1 ---------- */

  onToggle() {
    if (!this.finalized) return;
    this.cycleId += 1;
    if ($('uc-show-all').checked) this.cycle();
    else this.pick(this.best);
  },

  async cycle() {
    const id = ++this.cycleId;
    const tok = this.token;
    const set = this.runs;
    while (id === this.cycleId && tok === this.token && $('uc-show-all').checked) {
      for (const run of set) {
        if (id !== this.cycleId || tok !== this.token) return;
        await this.show(run, { animate: true });
        await sleep(CYCLE_PAUSE_MS);
      }
    }
  },

  async pick(k) {
    const run = this.runs.find(r => r.trucks === k);
    if (!run || (!this.finalized && this.animating)) return;
    this.cycleId += 1; // manual choice ends any auto-cycling
    $('uc-show-all').checked = false;
    await this.show(run, { animate: true });
  },

  /* ---------- Rendering ---------- */

  setStatus(label, big, text, warn = false) {
    const el = $('uc-summary');
    el.className = `callout${warn ? ' callout-warn' : ''}`;
    el.innerHTML = `<div class="callout-label">${esc(label)}</div>
      <div class="callout-big">${esc(big)}</div>
      <p>${esc(text)}</p>`;
  },

  renderRun(run) {
    const c = this.hooks.currency();
    $('uc-kpis').innerHTML = `
      <div><dt>Total distance</dt><dd>${fmt(run.total_distance_km)} km</dd></div>
      <div><dt>Unload trips</dt><dd>${run.unload_trips}</dd></div>
      <div><dt>Last truck home</dt><dd>${hhmm(run.longest_route_min)}</dd></div>
      <div><dt>Diesel</dt><dd>${fmt(run.fuel_liters)} L</dd></div>
      <div><dt>Cost</dt><dd>${c}${fmt(run.total_cost, 0)}</dd></div>
      <div><dt>Trucks</dt><dd>${run.trucks}</dd></div>`;
    $('uc-trucks').innerHTML = run.routes.map(t => `
      <div class="truck-line">
        <span class="avatar" style="--c:${esc(t.color)}">${esc(initials(t.driver || String(t.vehicle_id)))}</span>
        <span class="tl-who">${esc(t.driver || `Truck ${t.vehicle_id}`)}<small>${t.stops} stops · ${fmt(t.load_kg, 0)} kg · ${t.trips} trip${t.trips === 1 ? '' : 's'}</small></span>
        <span class="tl-num">${fmt(t.distance_km)} km<small>${hhmm(t.duration_min)}</small></span>
      </div>`).join('');
  }
};
