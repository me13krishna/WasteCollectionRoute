/**
 * EcoRoute AI - Shift Playback Simulation Module
 * Simulates real-time truck progression along actual road network polylines.
 */

let simulationInterval = null;
let isPlaying = false;
let currentStepIndex = 0;
let totalSteps = 0;
let simSpeed = 1;
let activeTruckMarkers = [];
let routeWaypoints = []; // list of [{ lat, lon, vehicle_id, vehicle_name, color, load_kg }]

export const simulation = {
  init(map, routes) {
    this.stop();
    this.map = map;
    this.routes = routes;

    // Dissect routes into interpolated simulation steps
    routeWaypoints = [];
    routes.forEach(r => {
      const geom = r.geometry || [];
      if (geom.length > 0) {
        geom.forEach((pt, idx) => {
          routeWaypoints.push({
            lat: pt[1],
            lon: pt[0],
            vehicle_id: r.vehicle_id,
            vehicle_name: r.vehicle_name,
            color: r.color || '#10b981',
            progress_ratio: idx / geom.length,
            load_kg: Math.round(r.total_demand_kg * (idx / geom.length))
          });
        });
      }
    });

    totalSteps = Math.max(1, Math.max(...routes.map(r => (r.geometry || []).length)));
    currentStepIndex = 0;

    // Create a simulation marker per vehicle
    activeTruckMarkers = routes.map(r => {
      const startPt = r.geometry?.[0] || [r.stops[0].lon, r.stops[0].lat];
      const truckIcon = L.divIcon({
        className: 'sim-truck-marker',
        html: `
          <div style="background:${r.color || '#10b981'}; width:28px; height:28px; border-radius:50%; display:flex; align-items:center; justify-content:center; box-shadow:0 0 10px ${r.color || '#10b981'}; border:2px solid #fff; color:#fff; font-size:14px;">
            🚛
          </div>
        `,
        iconSize: [28, 28],
        iconAnchor: [14, 14]
      });
      return {
        vehicle_id: r.vehicle_id,
        marker: L.marker([startPt[1], startPt[0]], { icon: truckIcon }).addTo(map),
        geometry: r.geometry || [],
        total_dist: r.total_distance_m,
        total_time: r.total_duration_sec
      };
    });

    this.updateUI();
  },

  play() {
    if (isPlaying) return;
    isPlaying = true;
    const playBtn = document.getElementById('sim-play-btn');
    if (playBtn) playBtn.innerHTML = '⏸️';

    simulationInterval = setInterval(() => {
      currentStepIndex += simSpeed;
      if (currentStepIndex >= totalSteps) {
        this.pause();
        currentStepIndex = totalSteps;
      }
      this.renderFrame(currentStepIndex);
      this.updateUI();
    }, 100);
  },

  pause() {
    isPlaying = false;
    clearInterval(simulationInterval);
    const playBtn = document.getElementById('sim-play-btn');
    if (playBtn) playBtn.innerHTML = '▶️';
  },

  stop() {
    this.pause();
    currentStepIndex = 0;
    activeTruckMarkers.forEach(t => {
      if (t.marker && this.map) {
        this.map.removeLayer(t.marker);
      }
    });
    activeTruckMarkers = [];
    this.updateUI();
  },

  setSpeed(speed) {
    simSpeed = parseInt(speed, 10) || 1;
  },

  seek(step) {
    currentStepIndex = parseInt(step, 10);
    this.renderFrame(currentStepIndex);
    this.updateUI();
  },

  renderFrame(step) {
    activeTruckMarkers.forEach(t => {
      const geom = t.geometry;
      if (geom.length > 0) {
        const clampedStep = Math.min(step, geom.length - 1);
        const pt = geom[clampedStep];
        if (pt) {
          t.marker.setLatLng([pt[1], pt[0]]);
        }
      }
    });
  },

  updateUI() {
    const slider = document.getElementById('sim-progress-slider');
    const timeLabel = document.getElementById('sim-time-label');
    if (slider) {
      slider.max = totalSteps;
      slider.value = currentStepIndex;
    }
    if (timeLabel) {
      const pct = Math.round((currentStepIndex / Math.max(1, totalSteps)) * 100);
      const estShiftMins = Math.round((currentStepIndex / Math.max(1, totalSteps)) * 360);
      const hrs = Math.floor(estShiftMins / 60);
      const mins = estShiftMins % 60;
      timeLabel.innerText = `Shift: ${hrs}h ${mins}m (${pct}%)`;
    }
  }
};
