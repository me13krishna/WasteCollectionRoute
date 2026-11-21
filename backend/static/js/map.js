/**
 * EcoRoute - map module
 * Basemap, stops, routes, traffic, incidents, and single-driver focus mode.
 */

import { esc, cssVar } from './util.js';

let mapInstance = null;
let baseLayer = null;
let labelLayer = null;
let canvasRenderer = null;
let depotMarker = null;

let stopsLayer = null;     // all collection points (dots)
let routeLayer = null;     // optimized routes (casing + line)
let focusLayer = null;     // numbered stops of the focused route
let baselineLayer = null;
let trafficLayer = null;
let incidentLayer = null;
let fleetLayer = null;      // truck-count finder: roads filling in
let fleetToken = 0;         // bumped to cancel a running fill animation

let routeShapes = new Map(); // vehicle_id -> { casing, line, route }
let stopMarkers = [];        // { marker, loc, vehicleId }
let focusedVehicle = null;
let onRouteClick = null;

let clickMode = null;
let onMapClickCallback = null;

const ESRI = 'https://services.arcgisonline.com/ArcGIS/rest/services/Canvas';
const ESRI_ATTR = 'Tiles © Esri — Esri, HERE, Garmin, © OpenStreetMap contributors';

const PLACEMENT_TEXT = {
  road_closure: 'Click a road to close it',
  traffic_jam: 'Click where the traffic jam is',
  urgent_pickup: 'Click where the overflowing bin is'
};

const INCIDENT_STYLE = {
  road_closure: { color: '--p4', letter: 'C' },
  traffic_jam: { color: '--p3', letter: 'T' },
  urgent_pickup: { color: '--p4', letter: '!' },
  emergency_pickup: { color: '--p4', letter: '!' }
};

const latlngs = (geom) => (geom || []).map(pt => [pt[1], pt[0]]);


const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

/** Planar cumulative length along a polyline (good enough to pace an animation). */
function cumulative(coords) {
  const k = Math.cos((coords[0][0] * Math.PI) / 180);
  const cum = [0];
  for (let i = 1; i < coords.length; i++) {
    const dy = coords[i][0] - coords[i - 1][0];
    const dx = (coords[i][1] - coords[i - 1][1]) * k;
    cum.push(cum[i - 1] + Math.hypot(dx, dy));
  }
  return cum;
}

/** The first `frac` of a polyline, ending exactly at the interpolated tip. */
function partial(coords, cum, frac) {
  const total = cum[cum.length - 1];
  if (frac <= 0 || total === 0) return [coords[0]];
  if (frac >= 1) return coords;
  const target = total * frac;
  let lo = 0;
  let hi = cum.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (cum[mid] <= target) lo = mid; else hi = mid;
  }
  const span = cum[hi] - cum[lo] || 1;
  const f = (target - cum[lo]) / span;
  const tip = [coords[lo][0] + (coords[hi][0] - coords[lo][0]) * f, coords[lo][1] + (coords[hi][1] - coords[lo][1]) * f];
  return [...coords.slice(0, lo + 1), tip];
}

export const mapManager = {
  init(containerId = 'map', defaultCenter = [18.5204, 73.8480], defaultZoom = 13) {
    if (mapInstance) return mapInstance;

    mapInstance = L.map(containerId, {
      maxZoom: 19,
      minZoom: 3,
      zoomControl: false,
      zoomSnap: 0.25
    }).setView(defaultCenter, defaultZoom);
    mapInstance.attributionControl.setPrefix(false);
    L.control.zoom({ position: 'topright' }).addTo(mapInstance);

    mapInstance.createPane('labels');
    mapInstance.getPane('labels').style.zIndex = 450; // above routes, below markers
    mapInstance.getPane('labels').style.pointerEvents = 'none';

    mapInstance.createPane('stops');
    mapInstance.getPane('stops').style.zIndex = 460; // dots sit above route lines and labels
    canvasRenderer = L.canvas({ padding: 0.5, pane: 'stops' });
    baselineLayer = L.layerGroup();
    trafficLayer = L.layerGroup().addTo(mapInstance);
    routeLayer = L.layerGroup().addTo(mapInstance);
    stopsLayer = L.layerGroup().addTo(mapInstance);
    focusLayer = L.layerGroup().addTo(mapInstance);
    incidentLayer = L.layerGroup().addTo(mapInstance);
    fleetLayer = L.layerGroup().addTo(mapInstance);

    this.updateTileLayer();

    mapInstance.on('click', (e) => {
      if (clickMode && onMapClickCallback) {
        const cb = onMapClickCallback;
        const mode = clickMode;
        this.exitClickMode();
        cb(e.latlng.lat, e.latlng.lng, mode);
      }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && clickMode) { e.preventDefault(); this.exitClickMode(); }
    });

    return mapInstance;
  },

  get map() { return mapInstance; },

  invalidate() {
    mapInstance?.invalidateSize();
  },

  updateTileLayer() {
    if (!mapInstance) return;
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    const tone = dark ? 'World_Dark_Gray' : 'World_Light_Gray';
    baseLayer && mapInstance.removeLayer(baseLayer);
    labelLayer && mapInstance.removeLayer(labelLayer);
    baseLayer = L.tileLayer(`${ESRI}/${tone}_Base/MapServer/tile/{z}/{y}/{x}`, {
      maxNativeZoom: 16, maxZoom: 19, attribution: ESRI_ATTR
    }).addTo(mapInstance);
    labelLayer = L.tileLayer(`${ESRI}/${tone}_Reference/MapServer/tile/{z}/{y}/{x}`, {
      maxNativeZoom: 16, maxZoom: 19, pane: 'labels'
    }).addTo(mapInstance);
  },

  /* ---------- Incident placement ---------- */

  setClickMode(mode, callback) {
    clickMode = mode;
    onMapClickCallback = callback;
    const banner = document.getElementById('placement-banner');
    const bannerText = document.getElementById('placement-banner-text');
    if (banner && bannerText) {
      bannerText.textContent = PLACEMENT_TEXT[mode] || 'Click on the map';
      banner.hidden = false;
    }
    document.querySelectorAll('[data-place]').forEach(b => b.classList.toggle('armed', b.dataset.place === mode));
    mapInstance.getContainer().style.cursor = 'crosshair';
  },

  exitClickMode() {
    clickMode = null;
    onMapClickCallback = null;
    const banner = document.getElementById('placement-banner');
    if (banner) banner.hidden = true;
    document.querySelectorAll('[data-place]').forEach(b => b.classList.remove('armed'));
    if (mapInstance) mapInstance.getContainer().style.cursor = '';
  },

  /* ---------- Depot & stops ---------- */

  renderDepot(depot) {
    if (!mapInstance || !depot) return;
    if (depotMarker) mapInstance.removeLayer(depotMarker);
    depotMarker = L.marker([depot.lat, depot.lon], {
      icon: L.divIcon({ html: '<div class="mk-depot"></div>', className: 'mk', iconSize: [16, 16], iconAnchor: [8, 8] }),
      zIndexOffset: 500,
      title: depot.name
    })
      .bindPopup(`
        <div class="popup-title">${esc(depot.name)}</div>
        <div class="popup-meta">Depot · every route starts and ends here</div>
      `)
      .addTo(mapInstance);
  },

  /**
   * @param locations all collection points
   * @param assignment location_id -> { color, seq, vehicleId, driver } for planned stops
   */
  renderStops(locations, assignment = {}) {
    if (!stopsLayer) return;
    stopsLayer.clearLayers();
    stopMarkers = [];
    const hasPlan = Object.keys(assignment).length > 0;
    const surface = cssVar('--surface');
    const muted = cssVar('--muted');

    locations.forEach(loc => {
      const a = assignment[loc.id];
      const fill = a ? a.color : (hasPlan ? surface : cssVar('--p1'));
      const marker = L.circleMarker([loc.lat, loc.lon], {
        renderer: canvasRenderer,
        radius: 4.5,
        weight: 1.5,
        color: a || !hasPlan ? surface : muted,
        fillColor: fill,
        fillOpacity: 1
      }).bindPopup(`
        <div class="popup-title">${esc(loc.name)}</div>
        <div class="popup-meta">#${loc.id}${loc.status !== 'pending' ? ' · ' + esc(loc.status) : ''}</div>
        <div>${loc.demand_kg} kg · ${Math.round(loc.service_time_sec / 60 * 10) / 10} min service</div>
        ${a ? `<div class="popup-meta">Stop ${a.seq} for ${esc(a.driver || 'truck ' + a.vehicleId)}</div>` : (hasPlan ? '<div class="popup-meta">Not in this plan</div>' : '')}
      `);
      stopsLayer.addLayer(marker);
      stopMarkers.push({ marker, loc, vehicleId: a ? a.vehicleId : null });
    });

    if (focusedVehicle != null) this.applyFocusStyles();
  },

  fitToData(depot, locations) {
    if (!mapInstance) return;
    const pts = (locations || []).map(l => [l.lat, l.lon]);
    if (depot) pts.push([depot.lat, depot.lon]);
    if (pts.length > 1) mapInstance.fitBounds(pts, { padding: [48, 48] });
    else if (pts.length === 1) mapInstance.setView(pts[0], 14);
  },

  /* ---------- Fleet finder: roads fill in truck by truck ---------- */

  fleetClear() {
    fleetToken += 1;
    fleetLayer?.clearLayers();
  },

  /**
   * Draws every truck's route, filling in smoothly from the depot. Trucks start slightly one after
   * another so each road is seen being laid down. Resolves when finished or cancelled.
   * `duration: 0` draws the finished routes at once.
   */
  fleetPlay(routes, { duration = 3200, fit = true } = {}) {
    if (!fleetLayer) return Promise.resolve();
    this.fleetClear();
    const token = fleetToken;
    const casingColor = cssVar('--surface');

    const shapes = routes
      .map(r => ({ r, coords: latlngs(r.geometry) }))
      .filter(s => s.coords.length > 1)
      .map(s => {
        const casing = L.polyline([s.coords[0]], { color: casingColor, weight: 7, opacity: 0.9, lineCap: 'round', lineJoin: 'round', interactive: false });
        const line = L.polyline([s.coords[0]], { color: s.r.color, weight: 3.5, opacity: 0.95, lineCap: 'round', lineJoin: 'round', interactive: false });
        const head = L.circleMarker(s.coords[0], { radius: 5, weight: 2, color: casingColor, fillColor: s.r.color, fillOpacity: 1, interactive: false });
        fleetLayer.addLayer(casing); fleetLayer.addLayer(line); fleetLayer.addLayer(head);
        return { ...s, casing, line, head, cum: cumulative(s.coords) };
      });

    if (fit && shapes.length) {
      mapInstance.fitBounds(shapes.flatMap(s => s.coords), { padding: [48, 48], animate: false });
    }

    const setProgress = (s, frac) => {
      const pts = partial(s.coords, s.cum, frac);
      s.casing.setLatLngs(pts);
      s.line.setLatLngs(pts);
      s.head.setLatLng(pts[pts.length - 1]);
      s.head.setStyle({ opacity: frac >= 1 ? 0 : 1, fillOpacity: frac >= 1 ? 0 : 1 });
    };

    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (!duration || reduced || !shapes.length) {
      shapes.forEach(s => setProgress(s, 1));
      return Promise.resolve();
    }

    const n = shapes.length;
    const stagger = n > 1 ? Math.min(0.12, 0.5 / (n - 1)) : 0; // fraction of total time between starts
    const window_ = 1 - stagger * (n - 1);
    return new Promise(resolve => {
      const t0 = performance.now();
      const frame = (now) => {
        if (token !== fleetToken) { resolve(); return; }
        const t = Math.min(1, (now - t0) / duration);
        shapes.forEach((s, i) => {
          const local = Math.min(1, Math.max(0, (t - i * stagger) / window_));
          setProgress(s, easeInOut(local));
        });
        if (t < 1) requestAnimationFrame(frame); else resolve();
      };
      requestAnimationFrame(frame);
    });
  },

  /* ---------- Routes ---------- */

  setRouteClickHandler(fn) { onRouteClick = fn; },

  clearRoutes() {
    routeLayer?.clearLayers();
    baselineLayer?.clearLayers();
    trafficLayer?.clearLayers();
    focusLayer?.clearLayers();
    this.fleetClear();
    routeShapes = new Map();
    focusedVehicle = null;
  },

  renderRoutes(routes, { fit = true } = {}) {
    if (!routeLayer) return;
    this.fleetClear();
    routeLayer.clearLayers();
    routeShapes = new Map();
    const casingColor = cssVar('--surface');

    routes.forEach(r => {
      const coords = latlngs(r.geometry);
      if (coords.length < 2) return;
      const casing = L.polyline(coords, { color: casingColor, weight: 7, opacity: 0.9, lineCap: 'round', lineJoin: 'round', interactive: false });
      const line = L.polyline(coords, { color: r.color, weight: 3.5, opacity: 0.95, lineCap: 'round', lineJoin: 'round' });
      line.on('click', (e) => { L.DomEvent.stopPropagation(e); onRouteClick?.(r.vehicle_id); });
      line.on('mouseover', () => { if (focusedVehicle == null) line.setStyle({ weight: 5.5 }); });
      line.on('mouseout', () => { if (focusedVehicle == null) line.setStyle({ weight: 3.5 }); });
      line.bindTooltip(esc(r.driver || r.vehicle_name), { sticky: true, direction: 'top', className: 'tip' });
      routeLayer.addLayer(casing);
      routeLayer.addLayer(line);
      routeShapes.set(r.vehicle_id, { casing, line, route: r });
    });

    if (focusedVehicle != null && routeShapes.has(focusedVehicle)) {
      this.focusRoute(focusedVehicle, { fit: false });
    } else {
      focusedVehicle = null;
      focusLayer.clearLayers();
      if (fit) {
        const all = routes.flatMap(r => latlngs(r.geometry));
        if (all.length > 1) mapInstance.fitBounds(all, { padding: [48, 48] });
      }
    }
  },

  /** Highlight one driver's route; null clears focus. */
  focusRoute(vehicleId, { fit = true, padRight = 0 } = {}) {
    focusedVehicle = vehicleId;
    focusLayer.clearLayers();

    const shape = vehicleId != null ? routeShapes.get(vehicleId) : null;
    if (!shape) {
      focusedVehicle = null;
      this.applyFocusStyles();
      return;
    }

    // Numbered markers for this route's collection stops, in visiting order.
    let n = 0;
    shape.route.stops.forEach(s => {
      if (s.is_depot) return;
      n += 1;
      const seq = n;
      focusLayer.addLayer(L.marker([s.lat, s.lon], {
        icon: L.divIcon({
          className: 'mk',
          html: `<div class="mk-seq" style="--c:${shape.route.color}">${seq}</div>`,
          iconSize: [20, 20],
          iconAnchor: [10, 10]
        }),
        zIndexOffset: 600
      }).on('click', () => this.onSeqClick?.(seq)));
    });

    this.applyFocusStyles();
    shape.casing.bringToFront();
    shape.line.bringToFront();

    if (fit) {
      mapInstance.fitBounds(shape.line.getBounds(), {
        paddingTopLeft: [48, 48],
        paddingBottomRight: [48 + padRight, 80]
      });
    }
  },

  applyFocusStyles() {
    const f = focusedVehicle;
    routeShapes.forEach((s, id) => {
      const on = f == null || id === f;
      s.line.setStyle({ opacity: on ? 0.95 : 0.12, weight: f != null && id === f ? 5 : 3.5 });
      s.casing.setStyle({ opacity: on ? 0.9 : 0 });
    });
    stopMarkers.forEach(({ marker, vehicleId }) => {
      const on = f == null;
      const hidden = f != null && vehicleId === f; // replaced by numbered markers
      marker.setStyle({ opacity: hidden ? 0 : (on ? 1 : 0.25), fillOpacity: hidden ? 0 : (on ? 1 : 0.25) });
    });
  },

  panTo(lat, lon) {
    mapInstance.panTo([lat, lon], { animate: true });
  },

  renderBaselineOverlay(baselineRoutes) {
    if (!baselineLayer) return;
    baselineLayer.clearLayers();
    const color = cssVar('--muted');
    baselineRoutes.forEach(r => {
      const coords = latlngs(r.geometry);
      if (coords.length < 2) return;
      baselineLayer.addLayer(L.polyline(coords, { color, weight: 2, dashArray: '4 6', opacity: 0.8, interactive: false }));
    });
  },

  renderTraffic(hotspots, overlaySegments) {
    if (!trafficLayer) return;
    trafficLayer.clearLayers();
    const hot = cssVar('--p4');

    (hotspots || []).forEach(spot => {
      trafficLayer.addLayer(L.circle([spot.lat, spot.lon], {
        radius: spot.radius_m,
        color: hot, fillColor: hot, fillOpacity: 0.05, weight: 1, opacity: 0.5, dashArray: '2 4'
      }).bindPopup(`
        <div class="popup-title">${esc(spot.name)}</div>
        <div class="popup-meta">Congestion hotspot · ${spot.base_congestion}× slower</div>
      `));
    });

    // overlaySegments are straight stop-to-stop matrix pairs, not roads; drawing them
    // produces an unreadable starburst, so only the hotspot zones are shown.
    void overlaySegments;
  },

  renderIncidents(incidents) {
    if (!incidentLayer) return;
    incidentLayer.clearLayers();
    (incidents || []).forEach(inc => {
      if (inc.active === false || inc.lat == null || inc.lon == null) return;
      if (inc.incident_type === 'missed_stop') return; // shown on the stop itself
      const style = INCIDENT_STYLE[inc.incident_type] || { color: '--p3', letter: '?' };
      incidentLayer.addLayer(L.marker([inc.lat, inc.lon], {
        icon: L.divIcon({
          html: `<div class="mk-incident" style="background:${cssVar(style.color)}"><span>${style.letter}</span></div>`,
          className: 'mk', iconSize: [18, 18], iconAnchor: [9, 9]
        }),
        zIndexOffset: 900
      }).bindPopup(`
        <div class="popup-title">${esc(labelForIncident(inc.incident_type))}</div>
        <div>${esc(inc.description)}</div>
        <div class="popup-meta">${esc(inc.timestamp || '')}</div>
      `));
    });
  },

  toggleBaseline(show) {
    if (!mapInstance) return;
    show ? mapInstance.addLayer(baselineLayer) : mapInstance.removeLayer(baselineLayer);
  },

  toggleTraffic(show) {
    if (!mapInstance) return;
    show ? mapInstance.addLayer(trafficLayer) : mapInstance.removeLayer(trafficLayer);
  }
};

export function labelForIncident(type) {
  return ({
    road_closure: 'Road closure',
    traffic_jam: 'Traffic jam',
    urgent_pickup: 'Overflowing bin',
    emergency_pickup: 'Overflowing bin',
    missed_stop: 'Missed stop'
  })[type] || String(type || '').replace(/_/g, ' ');
}
