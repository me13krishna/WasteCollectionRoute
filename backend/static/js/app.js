/**
 * EcoRoute - app controller
 * Wires the panel, map, charts and playback to the REST API.
 */

import { api } from './api.js';
import { mapManager, labelForIncident } from './map.js';
import { charts } from './charts.js';
import { simulation, initials } from './simulation.js';
import { fleetFinder } from './fleet.js';
import { esc, fmt } from './util.js';

const $ = (id) => document.getElementById(id);

/** Seconds into the shift as "h:mm". */
const clock = (sec) => {
  const m = Math.round((sec || 0) / 60);
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}`;
};
const shortTruck = (name) => String(name || '').split('(')[0].trim();

class EcoRouteApp {
  constructor() {
    this.config = null;
    this.dataset = null;
    this.lastResult = null;
    this.currency = '₹';
    this.busy = false;
    this.focusedId = null;
    this.sweep = null; // last truck-count comparison
  }

  async init() {
    mapManager.init('map', [18.5204, 73.8480], 13);
    mapManager.setRouteClickHandler(id => this.focusDriver(id));
    mapManager.onSeqClick = (seq) => this.highlightJourneyStop(seq);
    simulation.on('click', id => this.focusDriver(id));
    simulation.on('tick', elapsed => this.updateJourneyProgress(elapsed));
    this.setupEventListeners();
    fleetFinder.bind({
      currency: () => this.currency,
      prepare: () => this.clearResults(),
      paintStops: (run) => {
        if (!this.dataset) return;
        const assignment = {};
        (run?.routes || []).forEach(r => r.stop_ids.forEach((id, i) => {
          assignment[id] = { color: r.color, seq: i + 1, vehicleId: r.vehicle_id, driver: r.driver };
        }));
        mapManager.renderStops(this.dataset.locations, assignment);
      }
    });

    api.getScenarios().then(({ scenarios }) => {
      $('rand-scenario').insertAdjacentHTML('beforeend', scenarios.map(s =>
        `<option value="${s.id}">${s.id}. ${esc(s.name)} (${s.count})</option>`).join(''));
    }).catch(e => console.warn('scenarios', e));

    try {
      this.config = await api.getConfig();
      this.currency = this.config.currency_symbol || '₹';
      await this.refreshDataset({ fit: true });
    } catch (err) {
      console.error(err);
      this.toast(`Could not reach the server: ${err.message}`, 'error');
      return;
    }

    this.refreshTrafficStatus();
    this.refreshRoutingStatus();
    setInterval(() => this.refreshTrafficStatus(), 5 * 60 * 1000);
  }

  /* ---------- Data & status ---------- */

  async refreshDataset({ fit = false } = {}) {
    this.dataset = await api.getDataset();
    const { depot, locations, vehicles, incidents } = this.dataset;

    mapManager.renderDepot(depot);
    mapManager.renderStops(locations, this.stopAssignment());
    mapManager.renderIncidents(incidents);
    if (fit) mapManager.fitToData(depot, locations);

    this.renderFleet(vehicles);
    this.renderMissedStopSelect(locations);
    this.renderIncidentLog(incidents);
    this.syncSliderLimits(vehicles.length);

    $('setup-total-stops').textContent = `${locations.length} points`;
  }

  async refreshTrafficStatus() {
    try {
      const t = await api.getTrafficStatus();
      $('traffic-period-badge').textContent = `${t.period_name} · ${t.base_congestion_factor}×`;
      $('traffic-period-badge').parentElement.title = t.period_desc || 'Time-of-day traffic';
    } catch (e) {
      console.warn('traffic status', e);
    }
  }

  async refreshRoutingStatus() {
    try {
      const r = await api.getRoutingStatus();
      const dot = $('osrm-status-dot');
      const text = $('osrm-status-text');
      if (r.fallback_mode) {
        dot.className = 'dot dot-warn';
        text.textContent = 'Estimated roads';
        text.parentElement.title = 'OSRM unreachable: using straight-line distance × circuity factor';
      } else {
        dot.className = 'dot dot-ok';
        text.textContent = 'OSRM roads';
        text.parentElement.title = 'Connected to OSRM road network';
      }
    } catch (e) {
      console.warn('routing status', e);
    }
  }

  syncSliderLimits(vehicleCount) {
    const veh = $('opt-veh-count');
    veh.max = Math.max(1, vehicleCount);
    if (+veh.value > +veh.max) veh.value = veh.max;
    $('opt-veh-count-val').textContent = veh.value;

    const fleetMax = $('fleet-max');
    fleetMax.max = Math.max(2, vehicleCount);
    if (+fleetMax.value > +fleetMax.max) fleetMax.value = fleetMax.max;
    $('fleet-max-val').textContent = `${fleetMax.value} trucks`;
  }

  /** location_id -> { color, seq, vehicleId, driver } for every stop in the current plan. */
  stopAssignment() {
    const map = {};
    (this.lastResult?.solution?.routes || []).forEach(r => {
      let n = 0;
      r.stops.forEach(s => {
        if (!s.is_depot && s.location_id != null) {
          map[s.location_id] = { color: r.color, seq: ++n, vehicleId: r.vehicle_id, driver: r.driver };
        }
      });
    });
    return map;
  }

  routeFor(vehicleId) {
    return (this.lastResult?.solution?.routes || []).find(r => r.vehicle_id === vehicleId) || null;
  }

  /* ---------- Rendering ---------- */

  renderFleet(vehicles) {
    $('vehicles-list-view').innerHTML = vehicles.map(v => `
      <div class="row row-avatar">
        <span class="avatar" style="--c:${esc(v.color)}">${esc(initials(v.driver || v.name))}</span>
        <div>
          <div class="row-title">${esc(v.driver || shortTruck(v.name))}</div>
          <div class="row-meta">${esc(shortTruck(v.name))} · ${esc((v.name.match(/\((.*)\)/) || [])[1] || v.status)}</div>
        </div>
        <span class="row-end">${Math.round(v.max_duration_sec / 3600)} h shift</span>
      </div>
    `).join('');
  }

  renderMissedStopSelect(locations) {
    const select = $('missed-stop-select');
    const prev = select.value;
    select.innerHTML = '<option value="">Choose a stop…</option>' + locations
      .filter(s => s.status !== 'missed')
      .map(s => `<option value="${s.id}">#${s.id} ${esc(s.name)}</option>`)
      .join('');
    if ([...select.options].some(o => o.value === prev)) select.value = prev;
  }

  renderIncidentLog(incidents) {
    const el = $('incidents-timeline');
    if (!incidents || incidents.length === 0) {
      el.innerHTML = '<p class="muted small">No incidents.</p>';
      return;
    }
    el.innerHTML = incidents.slice().reverse().map(inc => `
      <div class="row">
        <span class="row-swatch" style="background:var(${inc.incident_type === 'road_closure' || inc.incident_type.includes('pickup') ? '--p4' : '--p3'})"></span>
        <div>
          <div class="row-title">${esc(labelForIncident(inc.incident_type))}</div>
          <div class="row-meta">${esc(inc.description)}</div>
        </div>
        <span class="row-end">${esc((inc.timestamp || '').split(' ')[1] || '')}</span>
      </div>
    `).join('');
  }

  showResults(res) {
    this.lastResult = res;
    const sol = res.solution;
    const comp = res.comparison;
    const m = sol.metrics;
    const s = comp.savings;
    const c = this.currency;

    // Keep the open journey if that driver is still in the new plan.
    const keepFocus = this.focusedId != null && sol.routes.some(r => r.vehicle_id === this.focusedId);
    if (!keepFocus) this.closeJourney({ refit: false });

    mapManager.renderRoutes(sol.routes, { fit: !keepFocus });
    mapManager.renderBaselineOverlay(comp.baseline_routes || []);
    mapManager.renderTraffic(res.traffic?.hotspots, res.traffic_overlay);
    mapManager.renderStops(this.dataset.locations, this.stopAssignment());

    const saving = (v, unit, pct, prefix = '') =>
      `${v < 0 ? '+' : ''}${prefix}${fmt(Math.abs(v), unit ? 1 : 0)}${unit ? ' ' + unit : ''}<span class="pct">${pct}% ${v < 0 ? 'more' : 'less'}</span>`;
    $('saving-km').innerHTML = saving(s.distance_km, 'km', Math.abs(s.distance_percent));
    $('saving-fuel').innerHTML = saving(s.fuel_liters, 'L', Math.abs(s.fuel_percent));
    $('saving-cost').innerHTML = saving(s.cost_currency, '', Math.abs(s.cost_percent), c);

    $('kpi-distance').textContent = `${fmt(m.total_distance_km)} km`;
    $('kpi-fuel').textContent = `${fmt(m.est_fuel_liters)} L`;
    $('kpi-co2').textContent = `${fmt(m.est_co2_kg)} kg`;
    $('kpi-cost').textContent = `${c}${fmt(m.est_total_cost, 0)}`;
    $('kpi-vehicles').textContent = `${m.vehicles_used} of ${m.vehicles_available}`;
    $('kpi-duration').textContent = `${Math.floor(m.total_travel_time_sec / 3600)}h ${String(Math.round((m.total_travel_time_sec % 3600) / 60)).padStart(2, '0')}m`;

    const diff = res.diff_from_initial;
    const diffEl = $('route-diff-banner');
    const changed = diff && diff.has_diff && ((diff.affected_vehicles || []).length > 0 || diff.distance_delta_km !== 0);
    diffEl.hidden = !changed;
    if (changed) diffEl.textContent = diff.explanation;

    const unservedEl = $('unserved-banner');
    unservedEl.hidden = !(m.unserved_count > 0);
    if (m.unserved_count > 0) {
      const names = (sol.unserved_stops || []).slice(0, 3).map(u => u.name).join(', ');
      unservedEl.textContent = `${m.unserved_count} stop${m.unserved_count > 1 ? 's' : ''} could not be served with this fleet${names ? ` (${names}${m.unserved_count > 3 ? ', …' : ''})` : ''}. Add trucks or allow more solver time.`;
    }

    this.renderCharts();
    this.renderRouteList(sol.routes);
    this.renderDriversLegend(sol.routes);
    simulation.init(mapManager.init(), sol.routes);
    if (keepFocus) this.focusDriver(this.focusedId, { fit: false });

    $('results-empty').hidden = true;
    $('results-body').hidden = false;
    this.setExportsEnabled(true);
  }

  renderCharts() {
    if (!this.lastResult) return;
    const { solution, comparison } = this.lastResult;
    charts.renderComparisonChart('comparisonChart', comparison.baseline, comparison.optimized, this.currency);
    charts.renderWorkloadChart('workloadChart', solution.routes);
  }

  renderRouteList(routes) {
    const maxLoad = Math.max(1, ...routes.map(r => r.total_demand_kg));
    $('results-vehicle-table').innerHTML = routes.map(r => {
      const pct = Math.round((r.total_demand_kg / maxLoad) * 100);
      const stops = r.stops.filter(s => !s.is_depot).length;
      const mins = Math.round(r.total_duration_sec / 60);
      return `
        <div class="row row-avatar clickable" data-vehicle="${r.vehicle_id}" title="Show ${esc(r.driver || 'this')} journey on the map">
          <span class="avatar" style="--c:${esc(r.color)}">${esc(initials(r.driver || r.vehicle_name))}</span>
          <div>
            <div class="row-title">${esc(r.driver || shortTruck(r.vehicle_name))}</div>
            <div class="row-meta">${esc(shortTruck(r.vehicle_name))} · ${stops} stops · ${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m · ${fmt(r.fuel?.total_fuel_liters ?? 0)} L</div>
            <div class="bar"><span style="width:${pct}%; background:${esc(r.color)}"></span></div>
          </div>
          <span class="row-end">${fmt(r.total_distance_m / 1000)} km<br>${fmt(r.total_demand_kg, 0)} kg</span>
        </div>
      `;
    }).join('') || '<p class="muted small">No trucks were dispatched.</p>';
  }

  renderDriversLegend(routes) {
    const el = $('drivers-legend');
    el.innerHTML = '<div class="drivers-title">Drivers</div>' + routes.map(r => `
      <button class="driver-chip${r.vehicle_id === this.focusedId ? ' active' : ''}" data-vehicle="${r.vehicle_id}">
        <span class="avatar" style="--c:${esc(r.color)}">${esc(initials(r.driver || r.vehicle_name))}</span>
        <span>${esc(r.driver || shortTruck(r.vehicle_name))}</span>
        <span class="meta">${r.stops.filter(s => !s.is_depot).length}</span>
      </button>
    `).join('');
    el.hidden = routes.length === 0;
  }

  /* ---------- Driver journey ---------- */

  focusDriver(vehicleId, { fit = true } = {}) {
    const r = this.routeFor(vehicleId);
    if (!r) return;
    if (this.focusedId === vehicleId && fit && !$('journey').hidden) {
      // Clicking the focused driver again just re-centres on their route.
      mapManager.focusRoute(vehicleId, { padRight: this.journeyPad() });
      return;
    }
    this.focusedId = vehicleId;

    const stops = r.stops.filter(s => !s.is_depot);
    const av = $('journey-avatar');
    av.style.setProperty('--c', r.color);
    av.textContent = initials(r.driver || r.vehicle_name);
    $('journey').style.setProperty('--c', r.color);
    $('journey-driver').textContent = r.driver || shortTruck(r.vehicle_name);
    $('journey-truck').textContent = r.vehicle_name;
    $('journey-stops').textContent = stops.length;
    $('journey-km').textContent = `${fmt(r.total_distance_m / 1000)} km`;
    $('journey-time').textContent = clock(r.total_duration_sec);
    $('journey-load').textContent = `${fmt(r.total_demand_kg, 0)} kg`;

    let seq = 0;
    $('journey-timeline').innerHTML = r.stops.map((s, i) => {
      const isStart = i === 0;
      const isEnd = i === r.stops.length - 1;
      if (!s.is_depot) seq += 1;
      const name = s.is_depot ? (isStart ? 'Leave depot' : 'Return to depot') : (s.location_name || s.name || `Stop ${seq}`);
      const sub = s.is_depot
        ? (isEnd ? `Unload ${fmt(s.cumulative_kg || r.total_demand_kg, 0)} kg` : 'Shift start')
        : `Stop ${seq} · +${fmt(s.collected_kg, 0)} kg`;
      return `
        <li class="${s.is_depot ? 'depot' : ''}" data-i="${i}" data-seq="${s.is_depot ? '' : seq}" data-arrival="${s.arrival_sec || 0}" data-lat="${s.lat}" data-lon="${s.lon}">
          <span class="tl-time">${clock(s.arrival_sec)}</span>
          <span class="tl-name">${esc(name)}<small>${esc(sub)}</small></span>
          <span class="tl-load">${s.is_depot ? '' : fmt(s.cumulative_kg, 0) + ' kg'}</span>
        </li>`;
    }).join('');

    $('journey').hidden = false;
    document.querySelectorAll('.driver-chip').forEach(c => c.classList.toggle('active', +c.dataset.vehicle === vehicleId));
    mapManager.focusRoute(vehicleId, { fit, padRight: this.journeyPad() });
    simulation.setFocus(vehicleId);
    this.updateJourneyProgress(simulation.elapsed);
  }

  journeyPad() {
    return window.innerWidth > 760 ? 340 : 0;
  }

  closeJourney({ refit = true } = {}) {
    if (this.focusedId == null && $('journey').hidden) return;
    this.focusedId = null;
    $('journey').hidden = true;
    document.querySelectorAll('.driver-chip').forEach(c => c.classList.remove('active'));
    mapManager.focusRoute(null);
    simulation.setFocus(null);
    if (refit && this.lastResult) {
      mapManager.renderRoutes(this.lastResult.solution.routes, { fit: true });
    }
  }

  /** Mark visited stops in the open journey as playback advances. */
  updateJourneyProgress(elapsed) {
    if (this.focusedId == null) return;
    const r = this.routeFor(this.focusedId);
    const items = [...document.querySelectorAll('#journey-timeline li')];
    if (!r || items.length === 0) return;

    let current = -1;
    items.forEach((li, i) => {
      const done = elapsed > 0 && +li.dataset.arrival <= elapsed;
      li.classList.toggle('done', done);
      if (done) current = i;
    });
    items.forEach((li, i) => li.classList.toggle('current', i === current && elapsed > 0));

    const status = $('journey-status');
    if (elapsed <= 0) status.textContent = 'At depot, not started';
    else if (elapsed >= r.total_duration_sec) status.textContent = 'Shift complete';
    else {
      const visited = items.filter(li => li.dataset.seq && li.classList.contains('done')).length;
      status.textContent = `${clock(elapsed)} · ${visited} of ${items.filter(li => li.dataset.seq).length} stops done`;
    }
    $('journey-play').textContent = simulation.isPlaying ? 'Pause' : (elapsed > 0 && elapsed < r.total_duration_sec ? 'Resume' : 'Play journey');

    if (simulation.isPlaying && current >= 0) {
      const li = items[current];
      const box = $('journey-timeline');
      if (li.offsetTop < box.scrollTop || li.offsetTop > box.scrollTop + box.clientHeight - 40) {
        box.scrollTo({ top: li.offsetTop - 40, behavior: 'smooth' });
      }
    }
  }

  highlightJourneyStop(seq) {
    const li = document.querySelector(`#journey-timeline li[data-seq="${seq}"]`);
    if (!li) return;
    li.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    li.classList.add('current');
    setTimeout(() => this.updateJourneyProgress(simulation.elapsed), 1200);
  }

  clearResults() {
    this.closeJourney({ refit: false });
    $('drivers-legend').hidden = true;
    this.lastResult = null;
    simulation.stop();
    $('sim-play-btn').disabled = true;
    $('sim-progress-slider').disabled = true;
    mapManager.clearRoutes();
    $('results-empty').hidden = false;
    $('results-body').hidden = true;
    this.setExportsEnabled(false);
  }

  setExportsEnabled(on) {
    ['export-geojson', 'export-manifest', 'export-report'].forEach(id => $(id).classList.toggle('is-disabled', !on));
    $('export-hint').textContent = on ? 'Downloads reflect the plan shown on the map.' : 'Run an optimization first.';
  }

  /* ---------- Events ---------- */

  setupEventListeners() {
    document.querySelectorAll('.tab').forEach(tab => {
      tab.addEventListener('click', () => this.switchTab(tab.dataset.tab));
    });

    $('toggle-sidebar-btn').addEventListener('click', () => {
      $('sidebar').classList.toggle('collapsed');
      setTimeout(() => mapManager.invalidate(), 250);
    });

    $('theme-toggle-btn').addEventListener('click', () => {
      const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('ecoroute-theme', next); } catch (e) { /* storage unavailable */ }
      mapManager.updateTileLayer();
      if (this.dataset) {
        mapManager.renderStops(this.dataset.locations, this.stopAssignment());
        mapManager.renderIncidents(this.dataset.incidents);
      }
      if (this.lastResult) {
        mapManager.renderRoutes(this.lastResult.solution.routes, { fit: false }); // casing follows theme
        mapManager.renderBaselineOverlay(this.lastResult.comparison.baseline_routes || []);
        mapManager.renderTraffic(this.lastResult.traffic?.hotspots, this.lastResult.traffic_overlay);
        this.renderCharts();
      }
      if (this.sweep) this.renderFleetChart();
    });

    const bindSlider = (id, fmtFn = v => v) => {
      $(id).addEventListener('input', e => { $(`${id}-val`).textContent = fmtFn(e.target.value); });
    };
    bindSlider('opt-veh-count');
    bindSlider('opt-time-limit', v => `${v}s`);
    bindSlider('rand-count');
    bindSlider('fleet-max', v => `${v} trucks`);
    bindSlider('fleet-time', v => `${v}s`);

    $('btn-run-optimize').addEventListener('click', () => this.findOptimalFleet());
    $('btn-randomize').addEventListener('click', () => this.generateRandomPoints());
    $('rand-scenario').addEventListener('change', () => {
      const fixed = !!$('rand-scenario').value; // presets fix the point count
      $('rand-count-field').hidden = fixed;
      $('randomize-each-label').hidden = fixed;
    });
    $('btn-run-sweep').addEventListener('click', () => this.runSweep());
    $('fleet-table').addEventListener('click', (e) => {
      const view = e.target.closest('[data-view]');
      if (view) { this.viewSweepRun(+view.dataset.view); return; }
      const row = e.target.closest('tr.ft-row');
      if (row) this.toggleSweepRow(+row.dataset.k);
    });

    $('layer-toggle-traffic').addEventListener('change', e => mapManager.toggleTraffic(e.target.checked));
    $('layer-toggle-baseline').addEventListener('change', e => mapManager.toggleBaseline(e.target.checked));

    // Map-placed incidents
    const placeable = {
      'btn-add-road-closure': ['road_closure', 'Road closed'],
      'btn-add-traffic-jam': ['traffic_jam', 'Traffic jam'],
      'btn-add-urgent-pickup': ['urgent_pickup', 'Overflowing bin']
    };
    Object.entries(placeable).forEach(([id, [type, label]]) => {
      const btn = $(id);
      btn.dataset.place = type;
      btn.addEventListener('click', () => {
        if (btn.classList.contains('armed')) { mapManager.exitClickMode(); return; }
        mapManager.setClickMode(type, (lat, lon) => this.reportIncident({
          incident_type: type,
          description: `${label} near ${lat.toFixed(4)}, ${lon.toFixed(4)}`,
          lat, lon
        }));
      });
    });
    $('btn-cancel-placement').addEventListener('click', () => mapManager.exitClickMode());

    $('btn-report-missed').addEventListener('click', () => {
      const stopId = parseInt($('missed-stop-select').value, 10);
      if (!stopId) { this.toast('Choose a stop first.', 'error'); return; }
      const stop = this.dataset.locations.find(s => s.id === stopId);
      this.reportIncident({
        incident_type: 'missed_stop',
        description: `${stop ? stop.name : `Stop #${stopId}`} was missed`,
        stop_id: stopId,
        lat: stop?.lat,
        lon: stop?.lon
      });
    });

    $('btn-undo-incident').addEventListener('click', () => this.undoIncident());
    $('btn-reset-incidents').addEventListener('click', () => this.resetIncidents());

    // Drivers: map panel list, results list, journey card
    const pickDriver = (e) => {
      const el = e.target.closest('[data-vehicle]');
      if (el) this.focusDriver(+el.dataset.vehicle);
    };
    $('drivers-legend').addEventListener('click', pickDriver);
    $('results-vehicle-table').addEventListener('click', pickDriver);
    $('journey-close').addEventListener('click', () => this.closeJourney());
    $('journey-play').addEventListener('click', () => {
      const r = this.routeFor(this.focusedId);
      if (!r) return;
      if (simulation.isPlaying) { simulation.pause(); }
      else {
        if (simulation.elapsed >= r.total_duration_sec) simulation.seekTo(0);
        simulation.play();
      }
      this.updateJourneyProgress(simulation.elapsed);
    });
    $('journey-timeline').addEventListener('click', (e) => {
      const li = e.target.closest('li');
      if (!li) return;
      simulation.pause();
      simulation.seekTo(+li.dataset.arrival); // move every driver to that moment
      mapManager.panTo(+li.dataset.lat, +li.dataset.lon);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !e.defaultPrevented && !$('journey').hidden) this.closeJourney();
    });

    $('sim-play-btn').addEventListener('click', () => simulation.toggle());
    $('sim-speed-select').addEventListener('change', e => simulation.setSpeed(e.target.value));
    $('sim-progress-slider').addEventListener('input', e => simulation.seek(e.target.value));
    simulation.setSpeed($('sim-speed-select').value);

    $('btn-extract-osm').addEventListener('click', () => this.importCity());
    $('osm-city-input').addEventListener('keydown', e => { if (e.key === 'Enter') this.importCity(); });
    $('csv-upload-input').addEventListener('change', e => this.uploadCsv(e));
    $('btn-reset-dataset').addEventListener('click', () => this.restoreSampleData());
  }

  switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === `tab-${name}`));
    if (name === 'results' && this.lastResult) requestAnimationFrame(() => this.renderCharts());
    if (name === 'fleet' && this.sweep) requestAnimationFrame(() => this.renderFleetChart());
  }

  /** Runs one server action at a time so plan state can't interleave. */
  async withBusy(label, fn) {
    if (this.busy) { this.toast('Still working on the previous request…'); return; }
    this.busy = true;
    const buttons = document.querySelectorAll('.panel-scroll button, .panel-scroll label.btn');
    buttons.forEach(b => b.classList.add('is-disabled'));
    const dot = $('solver-status-dot');
    const text = $('solver-status-text');
    dot.className = 'dot dot-busy';
    text.textContent = label;
    try {
      return await fn();
    } catch (err) {
      console.error(err);
      this.toast(err.message, 'error');
    } finally {
      this.busy = false;
      buttons.forEach(b => b.classList.remove('is-disabled'));
      dot.className = this.lastResult ? 'dot dot-ok' : 'dot';
      text.textContent = this.lastResult ? 'Plan ready' : 'Idle';
    }
  }

  optimizePayload() {
    return {
      active_vehicles_count: parseInt($('opt-veh-count').value, 10),
      time_limit_sec: parseInt($('opt-time-limit').value, 10),
      objective: $('opt-objective').value,
      fleet_mode: $('opt-fleet-mode').value,
      traffic_enabled: $('opt-traffic-enabled').checked
    };
  }

  /** Replaces the points with a fresh random layout and makes every one of them part of the plan. */
  async applyRandomPoints() {
    const scenario = parseInt($('rand-scenario').value, 10) || null;
    const res = await api.randomizePoints(scenario ? { scenario } : { count: parseInt($('rand-count').value, 10) });
    this.clearResults();
    this.clearSweep();
    await this.refreshDataset({ fit: true });
    $('active-city-label').textContent = res.scenario ? `Pune: ${res.scenario}` : 'Pune (random)';
    return res;
  }

  async generateRandomPoints() {
    await this.withBusy('Generating…', async () => {
      const res = await this.applyRandomPoints();
      this.toast(`${res.total_stops} points${res.scenario ? ` (${res.scenario})` : ''}, ${fmt(res.total_demand_kg, 0)} kg to collect.${res.on_road ? '' : ' Not snapped to roads here.'}`);
    });
  }

  async runOptimization({ keepPoints = false } = {}) {
    const btn = $('btn-run-optimize');
    await this.withBusy('Solving…', async () => {
      if (!keepPoints && $('opt-randomize-each').checked && !$('rand-scenario').value) await this.applyRandomPoints();
      const payload = this.optimizePayload();
      btn.textContent = `Solving (up to ${payload.time_limit_sec}s)…`;
      try {
        const res = await api.runOptimization(payload);
        this.showResults(res);
        this.refreshRoutingStatus();
        const m = res.solution.metrics;
        this.toast(m.unserved_count > 0
          ? `${m.stops_served} stops on ${m.vehicles_used} trucks, ${m.unserved_count} not served.`
          : `All ${m.stops_served} stops covered by ${m.vehicles_used} truck${m.vehicles_used === 1 ? '' : 's'}, ${fmt(m.total_distance_km)} km.`);
        this.switchTab('results');
      } finally {
        btn.textContent = 'Optimize routes';
      }
    });
  }

  /* ---------- Truck-count comparison ---------- */

  clearSweep() {
    fleetFinder.reset();
    this.sweep = null;
    $('fleet-body').hidden = true;
    $('fleet-empty').hidden = false;
  }

  async findOptimalFleet() {
    const btn = $('btn-run-optimize');
    await this.withBusy('Finding best fleet…', async () => {
      btn.textContent = 'Planning…';
      try {
        await fleetFinder.start({
          maxTrucks: parseInt($('fleet-max').value, 10),
          timeLimit: parseInt($('fleet-time').value, 10),
          traffic: $('opt-traffic-enabled').checked
        });
      } finally {
        btn.textContent = 'Optimize routes';
      }
    });
  }

  async runSweep() {
    const maxTrucks = parseInt($('fleet-max').value, 10);
    const timeLimit = parseInt($('fleet-time').value, 10);
    const btn = $('btn-run-sweep');
    await this.withBusy('Comparing…', async () => {
      btn.textContent = `Comparing 1 to ${maxTrucks} trucks (up to ${maxTrucks * timeLimit}s)…`;
      try {
        const res = await api.fleetSweep({
          min_trucks: 1,
          max_trucks: maxTrucks,
          time_limit_sec: timeLimit,
          traffic_enabled: $('opt-traffic-enabled').checked
        });
        this.sweep = res;
        this.renderSweep();
        this.toast(res.best_trucks ? `Best: ${res.best_trucks} truck${res.best_trucks === 1 ? '' : 's'}.` : 'No truck count covers every point.', res.best_trucks ? 'info' : 'error');
      } finally {
        btn.textContent = 'Compare truck counts';
      }
    });
  }

  renderSweep() {
    const sw = this.sweep;
    if (!sw) return;
    const c = this.currency;
    const plural = (n) => `${n} truck${n === 1 ? '' : 's'}`;
    const best = sw.runs.find(r => r.trucks === sw.best_trucks);
    const fastest = sw.runs.find(r => r.trucks === sw.fastest_trucks);
    const cheapest = sw.runs.find(r => r.trucks === sw.cheapest_trucks);
    const hhmm = (min) => `${Math.floor(min / 60)}h ${String(Math.round(min % 60)).padStart(2, '0')}m`;

    const chips = [];
    if (fastest && fastest.trucks !== sw.best_trucks) chips.push(`<span><b>Fastest</b> ${plural(fastest.trucks)} · ${hhmm(fastest.longest_route_min)}</span>`);
    if (cheapest && cheapest.trucks !== sw.best_trucks) chips.push(`<span><b>Lowest cost</b> ${plural(cheapest.trucks)} · ${c}${fmt(cheapest.total_cost, 0)}</span>`);

    $('fleet-summary').className = `callout${best ? '' : ' callout-warn'}`;
    $('fleet-summary').innerHTML = best
      ? `<div class="callout-label">Least distance</div>
         <div class="callout-big">${plural(best.trucks)}<span>${fmt(best.total_distance_km)} km</span></div>
         <p>${esc(sw.recommendation)}</p>
         ${chips.length ? `<div class="chips">${chips.join('')}</div>` : ''}
         <button class="btn btn-sm" data-view="${best.trucks}">Show on map</button>`
      : `<div class="callout-label">No feasible plan</div><p>${esc(sw.recommendation)}</p>`;
    $('fleet-summary').querySelector('[data-view]')?.addEventListener('click', e => this.viewSweepRun(+e.currentTarget.dataset.view));

    $('fleet-table').querySelector('tbody').innerHTML = sw.runs.map(r => {
      const tags = [
        r.trucks === sw.best_trucks ? '<span class="tag tag-best">Best</span>' : '',
        r.trucks === sw.fastest_trucks && r.trucks !== sw.best_trucks ? '<span class="tag">Fastest</span>' : '',
        r.trucks === sw.cheapest_trucks && r.trucks !== sw.best_trucks ? '<span class="tag">Cheapest</span>' : ''
      ].join('');
      if (!r.feasible && r.total_distance_km == null) {
        return `
          <tr class="ft-row ft-bad" data-k="${r.trucks}">
            <td><b>${r.trucks}</b></td><td colspan="3">Can't carry the load</td><td class="chev">›</td>
          </tr>
          <tr class="ft-detail" data-detail="${r.trucks}" hidden><td colspan="5"><p class="muted small">${esc(r.reason)}.</p></td></tr>`;
      }
      const extra = r.extra_km_vs_best;
      const vsBest = !r.feasible ? esc(r.reason)
        : extra === 0 ? 'Shortest total distance'
        : `+${fmt(extra)} km (${fmt(r.extra_pct_vs_best)}%) vs best`;
      const truckLines = r.routes.map(t => `
        <div class="truck-line">
          <span class="avatar" style="--c:${esc(t.color)}">${esc(initials(t.driver || String(t.vehicle_id)))}</span>
          <span class="tl-who">${esc(t.driver || `Truck ${t.vehicle_id}`)}<small>${t.stops} stops · ${fmt(t.load_kg, 0)} kg</small></span>
          <span class="tl-num">${fmt(t.distance_km)} km<small>${hhmm(t.duration_min)}</small></span>
        </div>`).join('');
      return `
        <tr class="ft-row${r.trucks === sw.best_trucks ? ' ft-best open' : ''}${r.feasible ? '' : ' ft-bad'}" data-k="${r.trucks}">
          <td><b>${r.trucks}</b>${tags}</td>
          <td>${fmt(r.total_distance_km)} km</td>
          <td>${hhmm(r.longest_route_min)}</td>
          <td>${c}${fmt(r.total_cost, 0)}</td>
          <td class="chev">›</td>
        </tr>
        <tr class="ft-detail" data-detail="${r.trucks}"${r.trucks === sw.best_trucks ? '' : ' hidden'}>
          <td colspan="5">
            <p class="ft-vs${r.feasible ? '' : ' warn'}">${vsBest}</p>
            <dl class="mini-kpis">
              <div><dt>Diesel</dt><dd>${fmt(r.fuel_liters)} L</dd></div>
              <div><dt>CO₂</dt><dd>${fmt(r.co2_kg)} kg</dd></div>
              <div><dt>Avg load</dt><dd>${fmt(r.avg_load_kg, 0)} kg</dd></div>
              <div><dt>Drive time</dt><dd>${hhmm(r.total_drive_min)}</dd></div>
              <div><dt>Km spread</dt><dd>±${fmt(r.workload_balance_std_km)}</dd></div>
              <div><dt>Stops</dt><dd>${r.stops_served}/${sw.stops}</dd></div>
            </dl>
            ${truckLines}
            <button class="btn btn-sm ft-view" data-view="${r.trucks}">Show ${r.trucks === 1 ? 'this route' : `these ${r.trucks} routes`} on map</button>
          </td>
        </tr>`;
    }).join('');

    $('fleet-empty').hidden = true;
    $('fleet-body').hidden = false;
    this.renderFleetChart();
  }

  renderFleetChart() {
    if (!this.sweep || $('fleet-body').hidden) return;
    charts.renderFleetChart('fleetChart', this.sweep.runs, this.sweep.best_trucks);
  }

  toggleSweepRow(k) {
    const detail = document.querySelector(`#fleet-table tr[data-detail="${k}"]`);
    const row = document.querySelector(`#fleet-table tr.ft-row[data-k="${k}"]`);
    if (!detail) return;
    detail.hidden = !detail.hidden;
    row?.classList.toggle('open', !detail.hidden);
  }

  /** Plans exactly k trucks on the current points and shows the routes on the map. */
  async viewSweepRun(k) {
    const slider = $('opt-veh-count');
    slider.value = Math.min(k, +slider.max);
    $('opt-veh-count-val').textContent = slider.value;
    $('opt-fleet-mode').value = 'exact_fleet';
    $('opt-objective').value = 'distance';
    await this.runOptimization({ keepPoints: true });
  }

  async reportIncident(payload) {
    // Re-plan only if there is a plan to change; otherwise just record it.
    payload.auto_reoptimize = !!this.lastResult;
    await this.withBusy(payload.auto_reoptimize ? 'Re-planning…' : 'Saving…', async () => {
      const res = await api.reportIncident(payload);
      await this.refreshDataset();
      if (res.reopt_data) this.showResults(res.reopt_data);
      this.toast(res.reopt_data
        ? `${labelForIncident(payload.incident_type)} added. Routes updated.`
        : `${labelForIncident(payload.incident_type)} added. Optimize to plan around it.`);
    });
  }

  async undoIncident() {
    if (!this.dataset?.incidents?.length) { this.toast('Nothing to undo.'); return; }
    await this.withBusy('Re-planning…', async () => {
      const res = await api.undoIncident();
      await this.refreshDataset();
      if (res.reopt_data && this.lastResult) this.showResults(res.reopt_data);
      this.toast('Last incident removed.');
    });
  }

  async resetIncidents() {
    await this.withBusy('Resetting…', async () => {
      if (this.lastResult) {
        const res = await api.resetIncidents();
        await this.refreshDataset({ fit: true });
        if (res.reopt_data) this.showResults(res.reopt_data);
      } else {
        await api.resetDataset();
        await this.refreshDataset({ fit: true });
      }
      $('active-city-label').textContent = 'Pune';
      this.toast('All incidents cleared.');
    });
  }

  async restoreSampleData() {
    await this.withBusy('Loading…', async () => {
      await api.resetDataset();
      this.clearResults();
      this.clearSweep();
      await this.refreshDataset({ fit: true });
      $('active-city-label').textContent = 'Pune';
      this.toast(`Restored ${this.dataset.locations.length} Pune collection points.`);
    });
  }

  async importCity() {
    const city = $('osm-city-input').value.trim();
    if (!city) { this.toast('Enter a city name.', 'error'); return; }
    await this.withBusy('Importing…', async () => {
      const res = await api.extractOsm({ city, limit: 60 });
      this.clearResults();
      this.clearSweep();
      await this.refreshDataset({ fit: true });
      $('active-city-label').textContent = res.city || city;
      this.toast(`Imported ${res.total_stops} points for ${res.city || city}.`);
    });
  }

  async uploadCsv(event) {
    const input = event.target;
    const file = input.files?.[0];
    if (!file) return;
    await this.withBusy('Uploading…', async () => {
      try {
        const res = await api.uploadCsv(file);
        this.clearResults();
        this.clearSweep();
        await this.refreshDataset({ fit: true });
        this.toast(`Imported ${res.total_imported} stops from ${file.name}.`);
      } finally {
        input.value = '';
      }
    });
  }

  toast(message, type = 'info') {
    const container = $('toast-container');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0';
      el.style.transform = 'translateY(6px)';
      setTimeout(() => el.remove(), 250);
    }, type === 'error' ? 6000 : 3500);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  window.ecoRouteApp = new EcoRouteApp();
  window.ecoRouteApp.init();
});
