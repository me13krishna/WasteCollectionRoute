/**
 * EcoRoute AI - Master Application Controller
 * Coordinates UI tabs, API calls, Map visualization, Chart analytics, and Simulation.
 */

import { api } from './api.js';
import { mapManager } from './map.js';
import { charts } from './charts.js';
import { simulation } from './simulation.js';

class EcoRouteApp {
  constructor() {
    this.config = null;
    this.dataset = null;
    this.lastSolution = null;
    this.lastComparison = null;
    this.currencySymbol = '₹';
  }

  async init() {
    try {
      // 1. Fetch system configuration
      this.config = await api.getConfig();
      this.currencySymbol = this.config.currency_symbol || '₹';

      // 2. Initialize Leaflet Map
      mapManager.init('map', [18.5204, 73.8480], 13);

      // 3. Load initial dataset
      await this.refreshDataset();

      // 4. Fetch real-time traffic status
      this.refreshTrafficStatus();

      // 5. Setup Event Listeners
      this.setupEventListeners();

      // 6. Update OSRM status badge
      this.updateRoutingStatusBadge();

      this.showToast('EcoRoute AI Ready • Pune Smart City', 'info');
    } catch (err) {
      console.error('Initialization error:', err);
      this.showToast('Failed to initialize dashboard: ' + err.message, 'error');
    }
  }

  async refreshDataset() {
    this.dataset = await api.getDataset();
    mapManager.renderDepot(this.dataset.depot);
    mapManager.renderStops(this.dataset.locations);
    this.updateVehicleOptions(this.dataset.vehicles);
    this.updateMissedStopSelect(this.dataset.locations);
    this.renderIncidentsList(this.dataset.incidents);
    mapManager.renderIncidents(this.dataset.incidents);

    const stopCountLabel = document.getElementById('setup-total-stops');
    if (stopCountLabel) stopCountLabel.innerText = `${this.dataset.locations.length} points`;
  }

  async refreshTrafficStatus() {
    try {
      const traffic = await api.getTrafficStatus();
      const periodLabel = document.getElementById('traffic-period-badge');
      if (periodLabel) {
        periodLabel.innerText = `${traffic.period_name} (${traffic.base_congestion_factor}x)`;
      }
    } catch (e) {
      console.warn('Traffic status refresh warning:', e);
    }
  }

  async updateRoutingStatusBadge() {
    try {
      const routing = await api.getRoutingStatus();
      const dot = document.getElementById('osrm-status-dot');
      const text = document.getElementById('osrm-status-text');
      if (dot && text) {
        if (routing.fallback_mode) {
          dot.className = 'status-dot fallback';
          text.innerText = 'Fallback Circuity Mode';
          text.title = 'Urban Road Network Circuity Factor (1.32x Haversine)';
        } else {
          dot.className = 'status-dot done';
          text.innerText = 'Live OSRM Engine';
          text.title = 'Connected to OSRM Driving Road Network';
        }
      }
    } catch (e) {
      console.warn('Routing status error:', e);
    }
  }

  updateVehicleOptions(vehicles) {
    const list = document.getElementById('vehicles-list-view');
    if (!list) return;
    list.innerHTML = vehicles.map(v => `
      <div class="vehicle-row" style="border-left-color:${v.color || '#10b981'};">
        <div class="vehicle-header">
          <span>${v.name}</span>
          <span style="font-weight:bold; color:${v.color};">${v.capacity_kg} kg</span>
        </div>
        <div class="vehicle-stats">
          <span>Max Duration: ${Math.round(v.max_duration_sec / 3600)}h</span>
          <span style="text-transform:capitalize;">${v.status}</span>
        </div>
      </div>
    `).join('');
  }

  updateMissedStopSelect(locations) {
    const select = document.getElementById('missed-stop-select');
    if (!select) return;
    select.innerHTML = '<option value="">-- Choose Stop to Mark Missed --</option>' +
      locations.slice(0, 40).map(s => `
        <option value="${s.id}">#${s.id} - ${s.name} (${s.demand_kg} kg)</option>
      `).join('');
  }

  renderIncidentsList(incidents) {
    const timeline = document.getElementById('incidents-timeline');
    if (!timeline) return;

    if (!incidents || incidents.length === 0) {
      timeline.innerHTML = '<div style="font-size:12px; color:var(--text-dim); text-align:center; padding:12px;">No active incidents recorded.</div>';
      return;
    }

    timeline.innerHTML = incidents.slice().reverse().map(inc => `
      <div class="timeline-item" style="border-left-color:${inc.incident_type === 'road_closure' ? '#ef4444' : '#f59e0b'};">
        <div class="timeline-meta">
          <strong style="text-transform:uppercase;">${inc.incident_type.replace('_', ' ')}</strong>
          <span>${inc.timestamp || 'Just now'}</span>
        </div>
        <div>${inc.description}</div>
      </div>
    `).join('');
  }

  setupEventListeners() {
    // 1. Navigation Tabs
    document.querySelectorAll('.nav-tab').forEach(tab => {
      tab.addEventListener('click', (e) => {
        const targetTab = tab.getAttribute('data-tab');
        document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(`tab-${targetTab}`)?.classList.add('active');

        // Trigger map resize if sidebar changed
        setTimeout(() => mapManager.init().invalidateSize(), 200);
      });
    });

    // 2. Sidebar Toggle
    document.getElementById('toggle-sidebar-btn')?.addEventListener('click', () => {
      const sidebar = document.getElementById('sidebar');
      sidebar?.classList.toggle('collapsed');
      setTimeout(() => mapManager.init().invalidateSize(), 300);
    });

    // 3. Theme Toggle
    document.getElementById('theme-toggle-btn')?.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme') || 'dark';
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      mapManager.updateTileLayer();
      if (this.lastComparison) {
        charts.renderComparisonChart('comparisonChart', this.lastComparison.baseline, this.lastComparison.optimized, this.currencySymbol);
      }
    });

    // 4. Form Sliders Sync
    const stopSlider = document.getElementById('opt-stop-count');
    const stopVal = document.getElementById('opt-stop-count-val');
    stopSlider?.addEventListener('input', (e) => stopVal.innerText = e.target.value);

    const vehSlider = document.getElementById('opt-veh-count');
    const vehVal = document.getElementById('opt-veh-count-val');
    vehSlider?.addEventListener('input', (e) => vehVal.innerText = e.target.value);

    const timeSlider = document.getElementById('opt-time-limit');
    const timeVal = document.getElementById('opt-time-limit-val');
    timeSlider?.addEventListener('input', (e) => timeVal.innerText = `${e.target.value}s`);

    // 5. Optimize Routes Action Button
    document.getElementById('btn-run-optimize')?.addEventListener('click', () => this.runOptimization());

    // 6. Layer Controls
    document.getElementById('layer-toggle-traffic')?.addEventListener('change', (e) => {
      mapManager.toggleTraffic(e.target.checked);
    });
    document.getElementById('layer-toggle-baseline')?.addEventListener('change', (e) => {
      mapManager.toggleBaseline(e.target.checked);
    });

    // 7. Interactive Incident Placement Buttons
    document.getElementById('btn-add-road-closure')?.addEventListener('click', () => {
      mapManager.setClickMode('road_closure', (lat, lon) => {
        this.reportIncident({
          incident_type: 'road_closure',
          description: `Road Closure reported at [${lat.toFixed(4)}, ${lon.toFixed(4)}]`,
          lat: lat,
          lon: lon,
          auto_reoptimize: true
        });
      });
    });

    document.getElementById('btn-add-urgent-pickup')?.addEventListener('click', () => {
      mapManager.setClickMode('urgent_pickup', (lat, lon) => {
        this.reportIncident({
          incident_type: 'urgent_pickup',
          description: `Urgent Bin Overflow at [${lat.toFixed(4)}, ${lon.toFixed(4)}]`,
          lat: lat,
          lon: lon,
          auto_reoptimize: true
        });
      });
    });

    document.getElementById('btn-cancel-placement')?.addEventListener('click', () => {
      mapManager.exitClickMode();
    });

    document.getElementById('btn-report-missed')?.addEventListener('click', () => {
      const select = document.getElementById('missed-stop-select');
      const stopId = parseInt(select?.value, 10);
      if (!stopId) {
        this.showToast('Please select a collection stop to mark missed.', 'error');
        return;
      }
      this.reportIncident({
        incident_type: 'missed_stop',
        description: `Stop #${stopId} reported as missed by crew`,
        stop_id: stopId,
        auto_reoptimize: true
      });
    });

    document.getElementById('btn-undo-incident')?.addEventListener('click', () => this.undoIncident());
    document.getElementById('btn-reset-incidents')?.addEventListener('click', () => this.resetIncidents());

    // 8. Shift Simulation Controls
    document.getElementById('sim-play-btn')?.addEventListener('click', () => {
      if (document.getElementById('sim-play-btn').innerText.includes('▶')) {
        simulation.play();
      } else {
        simulation.pause();
      }
    });

    document.getElementById('sim-speed-select')?.addEventListener('change', (e) => {
      simulation.setSpeed(e.target.value);
    });

    document.getElementById('sim-progress-slider')?.addEventListener('input', (e) => {
      simulation.seek(e.target.value);
    });

    // 9. Dataset Ingestion (Overpass & CSV)
    document.getElementById('btn-extract-osm')?.addEventListener('click', () => this.handleOsmExtract());
    document.getElementById('csv-upload-input')?.addEventListener('change', (e) => this.handleCsvUpload(e));
  }

  async runOptimization() {
    const btn = document.getElementById('btn-run-optimize');
    const dot = document.getElementById('solver-status-dot');
    const text = document.getElementById('solver-status-text');

    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="status-dot solving"></span> Solving CVRP Guided Local Search...';
    }
    if (dot && text) {
      dot.className = 'status-dot solving';
      text.innerText = 'Solving...';
    }

    try {
      const stopCount = parseInt(document.getElementById('opt-stop-count')?.value || '50', 10);
      const vehCount = parseInt(document.getElementById('opt-veh-count')?.value || '4', 10);
      const timeLimit = parseInt(document.getElementById('opt-time-limit')?.value || '15', 10);
      const objective = document.getElementById('opt-objective')?.value || 'fuel';
      const fleetMode = document.getElementById('opt-fleet-mode')?.value || 'auto_minimize_vehicles';

      const payload = {
        selected_stop_count: stopCount,
        active_vehicles_count: vehCount,
        time_limit_sec: timeLimit,
        objective: objective,
        fleet_mode: fleetMode,
        traffic_enabled: true
      };

      const res = await api.runOptimization(payload);
      this.handleOptimizationSuccess(res);
      this.showToast('Routes Optimized Successfully!', 'success');

      // Switch to results tab automatically
      document.querySelector('[data-tab="results"]')?.click();
    } catch (err) {
      console.error('Optimization error:', err);
      this.showToast(err.message, 'error');
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '⚡ Optimize Multi-Vehicle Routes';
      }
      if (dot && text) {
        dot.className = 'status-dot done';
        text.innerText = 'Done';
      }
    }
  }

  handleOptimizationSuccess(res) {
    this.lastSolution = res.solution;
    this.lastComparison = res.comparison;
    const sol = res.solution;
    const comp = res.comparison;
    const savings = comp.savings;
    const metrics = sol.metrics;

    // 1. Render routes & baseline overlay on Leaflet map
    mapManager.renderRoutes(sol.routes);
    mapManager.renderBaselineOverlay(comp.baseline_routes || []);
    mapManager.renderTraffic(res.traffic?.hotspots, res.traffic_overlay);

    // Map stop sequence numbers
    const stopSeqMap = {};
    sol.routes.forEach(r => {
      r.stops.forEach((s, idx) => {
        if (!s.is_depot && s.location_id) {
          stopSeqMap[s.location_id] = idx;
        }
      });
    });
    mapManager.renderStops(this.dataset.locations, stopSeqMap);

    // 2. Update KPI Cards
    document.getElementById('kpi-distance').innerText = `${metrics.total_distance_km} km`;
    document.getElementById('kpi-fuel').innerText = `${metrics.est_fuel_liters} L`;
    document.getElementById('kpi-co2').innerText = `${metrics.est_co2_kg} kg`;
    document.getElementById('kpi-cost').innerText = `${this.currencySymbol}${metrics.est_total_cost.toLocaleString()}`;
    document.getElementById('kpi-vehicles').innerText = `${metrics.vehicles_used} / ${metrics.vehicles_available}`;
    document.getElementById('kpi-duration').innerText = `${Math.round(metrics.total_travel_time_sec / 60)} min`;

    // 3. Update Savings Banner
    document.getElementById('saving-km').innerText = `-${savings.distance_km} km (${savings.distance_percent}%)`;
    document.getElementById('saving-fuel').innerText = `-${savings.fuel_liters} L (${savings.fuel_percent}%)`;
    document.getElementById('saving-cost').innerText = `-${this.currencySymbol}${savings.cost_currency} (${savings.cost_percent}%)`;

    // 4. Render Charts
    charts.renderComparisonChart('comparisonChart', comp.baseline, comp.optimized, this.currencySymbol);
    charts.renderWorkloadChart('workloadChart', sol.routes);

    // 5. Render Per-Vehicle Breakdown Table
    this.renderVehicleBreakdown(sol.routes);

    // 6. Initialize Shift Simulation
    simulation.init(mapManager.init(), sol.routes);

    // 7. Render Route Diff if available
    if (res.diff_from_initial && res.diff_from_initial.has_diff) {
      const diffBanner = document.getElementById('route-diff-banner');
      if (diffBanner) {
        diffBanner.style.display = 'block';
        diffBanner.innerText = res.diff_from_initial.explanation;
      }
    }
  }

  renderVehicleBreakdown(routes) {
    const container = document.getElementById('results-vehicle-table');
    if (!container) return;

    container.innerHTML = routes.map(r => {
      const loadPct = Math.min(100, Math.round((r.total_demand_kg / r.vehicle_capacity_kg) * 100));
      return `
        <div class="vehicle-row" style="border-left-color:${r.color};">
          <div class="vehicle-header">
            <span><strong>${r.vehicle_name}</strong></span>
            <span style="color:${r.color}; font-weight:bold;">${Math.round(r.total_distance_m / 1000.0)} km</span>
          </div>
          <div class="progress-track">
            <div class="progress-bar" style="width:${loadPct}%; background:${r.color};"></div>
          </div>
          <div class="vehicle-stats">
            <span>Payload: ${r.total_demand_kg} / ${r.vehicle_capacity_kg} kg (${loadPct}%)</span>
            <span>Fuel: ${r.fuel.total_fuel_liters} L</span>
            <span>${r.stops.length} stops</span>
          </div>
        </div>
      `;
    }).join('');
  }

  async reportIncident(payload) {
    try {
      this.showToast('Applying incident & reoptimizing routes...', 'info');
      const res = await api.reportIncident(payload);
      await this.refreshDataset();
      if (res.reopt_data) {
        this.handleOptimizationSuccess(res.reopt_data);
      }
      this.showToast(`Incident logged: ${payload.incident_type.replace('_', ' ')}`, 'success');
    } catch (err) {
      this.showToast(err.message, 'error');
    }
  }

  async undoIncident() {
    try {
      this.showToast('Reverting last incident...', 'info');
      const res = await api.undoIncident();
      await this.refreshDataset();
      if (res.reopt_data) {
        this.handleOptimizationSuccess(res.reopt_data);
      }
      this.showToast('Last incident successfully undone!', 'success');
    } catch (err) {
      this.showToast(err.message, 'error');
    }
  }

  async resetIncidents() {
    try {
      this.showToast('Resetting all incidents to baseline...', 'info');
      const res = await api.resetIncidents();
      await this.refreshDataset();
      if (res.reopt_data) {
        this.handleOptimizationSuccess(res.reopt_data);
      }
      this.showToast('Dataset reset to clean 80 PMC collection points.', 'success');
    } catch (err) {
      this.showToast(err.message, 'error');
    }
  }

  async handleOsmExtract() {
    const cityInput = document.getElementById('osm-city-input');
    const cityName = cityInput?.value?.trim() || 'Pune';
    this.showToast(`Ingesting real waste infrastructure for ${cityName} from OpenStreetMap...`, 'info');

    try {
      const res = await api.extractOsm({ city: cityName, limit: 60 });
      await this.refreshDataset();
      this.showToast(`Imported ${res.total_stops} collection points for ${cityName}!`, 'success');
    } catch (err) {
      this.showToast(err.message, 'error');
    }
  }

  async handleCsvUpload(event) {
    const file = event.target.files?.[0];
    if (!file) return;

    this.showToast('Uploading custom collection points CSV...', 'info');
    try {
      const res = await api.uploadCsv(file);
      await this.refreshDataset();
      this.showToast(`Imported ${res.total_imported} stops from CSV!`, 'success');
    } catch (err) {
      this.showToast(err.message, 'error');
    }
  }

  showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    const icon = type === 'success' ? '✅' : (type === 'error' ? '❌' : 'ℹ️');
    toast.innerHTML = `<span>${icon}</span> <span>${message}</span>`;

    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      setTimeout(() => toast.remove(), 300);
    }, 3500);
  }
}

// Instantiate and launch app on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  window.ecoRouteApp = new EcoRouteApp();
  window.ecoRouteApp.init();
});
