/**
 * EcoRoute AI - Map Management Module
 * Leaflet map setup, custom SVG markers, route polylines, layers & traffic visualization.
 */

let mapInstance = null;
let currentTileLayer = null;
let depotMarker = null;
let stopsClusterGroup = null;
let routeLayersGroup = null;
let baselineLayersGroup = null;
let trafficOverlayGroup = null;
let incidentMarkersGroup = null;

let clickMode = null; // 'road_closure' | 'urgent_pickup' | null
let onMapClickCallback = null;

export const mapManager = {
  init(containerId = 'map', defaultCenter = [18.5204, 73.8480], defaultZoom = 13) {
    if (mapInstance) return mapInstance;

    mapInstance = L.map(containerId, {
      zoomControl: false,
      attributionControl: false
    }).setView(defaultCenter, defaultZoom);

    // Zoom control in top right
    L.control.zoom({ position: 'topright' }).addTo(mapInstance);

    // Layer Groups
    stopsClusterGroup = L.markerClusterGroup({
      maxClusterRadius: 40,
      spiderfyOnMaxZoom: true,
      showCoverageOnHover: false
    });
    routeLayersGroup = L.layerGroup().addTo(mapInstance);
    baselineLayersGroup = L.layerGroup();
    trafficOverlayGroup = L.layerGroup().addTo(mapInstance);
    incidentMarkersGroup = L.layerGroup().addTo(mapInstance);

    mapInstance.addLayer(stopsClusterGroup);

    // Tile Layer based on theme
    this.updateTileLayer();

    // Map click handler for interactive incident placement
    mapInstance.on('click', (e) => {
      if (clickMode && onMapClickCallback) {
        onMapClickCallback(e.latlng.lat, e.latlng.lng, clickMode);
        this.exitClickMode();
      }
    });

    return mapInstance;
  },

  updateTileLayer() {
    const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
    const tileUrl = isDark
      ? 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'
      : 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png';

    if (currentTileLayer && mapInstance) {
      mapInstance.removeLayer(currentTileLayer);
    }

    currentTileLayer = L.tileLayer(tileUrl, {
      maxZoom: 19,
      subdomains: 'abcd'
    }).addTo(mapInstance);
  },

  setClickMode(mode, callback) {
    clickMode = mode;
    onMapClickCallback = callback;
    const banner = document.getElementById('placement-banner');
    const bannerText = document.getElementById('placement-banner-text');
    if (banner && bannerText) {
      banner.style.display = 'flex';
      bannerText.innerText = mode === 'road_closure'
        ? 'Click anywhere on the road to place a ROAD CLOSURE 🚧'
        : 'Click on the map to place an URGENT COLLECTION BIN 🚨';
    }
    if (mapInstance) {
      mapInstance.getContainer().style.cursor = 'crosshair';
    }
  },

  exitClickMode() {
    clickMode = null;
    onMapClickCallback = null;
    const banner = document.getElementById('placement-banner');
    if (banner) banner.style.display = 'none';
    if (mapInstance) {
      mapInstance.getContainer().style.cursor = '';
    }
  },

  renderDepot(depot) {
    if (!mapInstance || !depot) return;
    if (depotMarker) mapInstance.removeLayer(depotMarker);

    const iconHtml = `
      <div style="background:#0f172a; border:2px solid #10b981; width:34px; height:34px; border-radius:50%; display:flex; align-items:center; justify-content:center; box-shadow:0 0 16px rgba(16,185,129,0.8); color:#10b981; font-size:16px;">
        🏛️
      </div>
    `;
    const depotIcon = L.divIcon({
      html: iconHtml,
      className: 'depot-marker-wrapper',
      iconSize: [34, 34],
      iconAnchor: [17, 17]
    });

    depotMarker = L.marker([depot.lat, depot.lon], { icon: depotIcon })
      .bindPopup(`
        <div style="font-family:Inter,sans-serif; font-size:12px; color:#111;">
          <strong style="font-size:13px; color:#10b981;">${depot.name}</strong><br/>
          <span>${depot.address || 'Central Solid Waste Hub'}</span><br/>
          <span style="font-size:10px; color:#6b7280;">Depot Coordinates: ${depot.lat.toFixed(4)}, ${depot.lon.toFixed(4)}</span>
        </div>
      `)
      .addTo(mapInstance);
  },

  renderStops(locations, stopSequenceMap = {}) {
    if (!stopsClusterGroup) return;
    stopsClusterGroup.clearLayers();

    const priorityColors = {
      1: '#3b82f6', // Normal (Blue)
      2: '#10b981', // Moderate (Green)
      3: '#f59e0b', // High (Amber)
      4: '#ef4444'  // Urgent (Red)
    };

    locations.forEach(loc => {
      const pColor = priorityColors[loc.priority] || '#3b82f6';
      const seq = stopSequenceMap[loc.id] !== undefined ? `#${stopSequenceMap[loc.id]}` : '';

      const iconHtml = `
        <div class="custom-stop-marker" style="background:${pColor}; width:22px; height:22px; border-radius:50%; border:2px solid #fff; display:flex; align-items:center; justify-content:center; font-size:9px; font-weight:bold; box-shadow:0 2px 6px rgba(0,0,0,0.4);">
          ${seq || loc.priority}
        </div>
      `;

      const stopIcon = L.divIcon({
        html: iconHtml,
        className: 'stop-marker-wrapper',
        iconSize: [22, 22],
        iconAnchor: [11, 11]
      });

      const marker = L.marker([loc.lat, loc.lon], { icon: stopIcon })
        .bindPopup(`
          <div style="font-family:Inter,sans-serif; font-size:12px; color:#111; min-width:180px;">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #e5e7eb; padding-bottom:4px; margin-bottom:6px;">
              <strong style="color:#111827;">${loc.name}</strong>
              <span style="background:${pColor}20; color:${pColor}; padding:2px 6px; border-radius:10px; font-weight:bold; font-size:10px;">P${loc.priority}</span>
            </div>
            <div>📦 Demand: <strong>${loc.demand_kg} kg</strong></div>
            <div>⏱️ Service Time: <strong>${loc.service_time_sec}s</strong></div>
            <div>📍 Status: <strong>${loc.status}</strong></div>
            ${seq ? `<div style="margin-top:4px; color:#059669; font-weight:bold;">Dispatched Stop: ${seq}</div>` : ''}
          </div>
        `);

      stopsClusterGroup.addLayer(marker);
    });
  },

  renderRoutes(routes) {
    if (!routeLayersGroup) return;
    routeLayersGroup.clearLayers();

    routes.forEach(r => {
      const coords = (r.geometry || []).map(pt => [pt[1], pt[0]]);
      if (coords.length < 2) return;

      const polyline = L.polyline(coords, {
        color: r.color || '#10b981',
        weight: 4.5,
        opacity: 0.9,
        lineCap: 'round',
        lineJoin: 'round'
      }).bindPopup(`
        <div style="font-family:Inter,sans-serif; font-size:12px; color:#111;">
          <strong style="font-size:13px; color:${r.color};">${r.vehicle_name}</strong><br/>
          <span>Distance: <strong>${(r.total_distance_m / 1000.0).toFixed(2)} km</strong></span><br/>
          <span>Payload: <strong>${r.total_demand_kg} / ${r.vehicle_capacity_kg} kg</strong></span><br/>
          <span>Stops: <strong>${r.stops.length} collections</strong></span>
        </div>
      `);

      // Hover emphasis
      polyline.on('mouseover', () => polyline.setStyle({ weight: 7, opacity: 1.0 }));
      polyline.on('mouseout', () => polyline.setStyle({ weight: 4.5, opacity: 0.9 }));

      routeLayersGroup.addLayer(polyline);
    });

    if (routes.length > 0 && mapInstance) {
      const allCoords = routes.flatMap(r => (r.geometry || []).map(pt => [pt[1], pt[0]]));
      if (allCoords.length > 0) {
        mapInstance.fitBounds(allCoords, { padding: [40, 40] });
      }
    }
  },

  renderBaselineOverlay(baselineRoutes) {
    if (!baselineLayersGroup) return;
    baselineLayersGroup.clearLayers();

    baselineRoutes.forEach(r => {
      const coords = (r.geometry || []).map(pt => [pt[1], pt[0]]);
      if (coords.length < 2) return;

      const poly = L.polyline(coords, {
        color: '#94a3b8',
        weight: 3,
        dashArray: '6, 8',
        opacity: 0.65
      });
      baselineLayersGroup.addLayer(poly);
    });
  },

  renderTraffic(hotspots, overlaySegments) {
    if (!trafficOverlayGroup) return;
    trafficOverlayGroup.clearLayers();

    // Hotspot circles
    (hotspots || []).forEach(spot => {
      const circle = L.circle([spot.lat, spot.lon], {
        radius: spot.radius_m,
        color: '#f43f5e',
        fillColor: '#f43f5e',
        fillOpacity: 0.18,
        weight: 1.5,
        dashArray: '4, 4'
      }).bindPopup(`
        <div style="font-family:Inter,sans-serif; font-size:12px; color:#111;">
          <strong style="color:#dc2626;">Traffic Bottleneck Hotspot</strong><br/>
          <span>${spot.name}</span><br/>
          <span>Base Congestion: <strong>${spot.base_congestion}x delay</strong></span>
        </div>
      `);
      trafficOverlayGroup.addLayer(circle);
    });

    // Dynamic Congestion Segments
    (overlaySegments || []).forEach(seg => {
      const poly = L.polyline([seg.from_coord, seg.to_coord], {
        color: seg.color || '#f59e0b',
        weight: seg.level === 'closed' ? 5 : 3.5,
        opacity: 0.75,
        dashArray: seg.level === 'closed' ? '4, 6' : null
      });
      trafficOverlayGroup.addLayer(poly);
    });
  },

  renderIncidents(incidents) {
    if (!incidentMarkersGroup) return;
    incidentMarkersGroup.clearLayers();

    (incidents || []).forEach(inc => {
      if (!inc.active || !inc.lat || !inc.lon) return;

      const isClosure = inc.incident_type === 'road_closure';
      const iconChar = isClosure ? '🚧' : (inc.incident_type === 'traffic_jam' ? '🚗' : '🚨');
      const bgColor = isClosure ? '#dc2626' : '#f59e0b';

      const iconHtml = `
        <div style="background:${bgColor}; width:28px; height:28px; border-radius:50%; display:flex; align-items:center; justify-content:center; box-shadow:0 0 12px ${bgColor}; border:2px solid #fff; font-size:14px;">
          ${iconChar}
        </div>
      `;

      const marker = L.marker([inc.lat, inc.lon], {
        icon: L.divIcon({ html: iconHtml, className: 'incident-marker', iconSize: [28, 28], iconAnchor: [14, 14] })
      }).bindPopup(`
        <div style="font-family:Inter,sans-serif; font-size:12px; color:#111;">
          <strong style="color:${bgColor}; text-transform:uppercase;">${inc.incident_type.replace('_', ' ')}</strong><br/>
          <span>${inc.description}</span><br/>
          <span style="font-size:10px; color:#6b7280;">Reported: ${inc.timestamp}</span>
        </div>
      `);

      incidentMarkersGroup.addLayer(marker);
    });
  },

  toggleBaseline(show) {
    if (!mapInstance || !baselineLayersGroup) return;
    if (show) {
      mapInstance.addLayer(baselineLayersGroup);
    } else {
      mapInstance.removeLayer(baselineLayersGroup);
    }
  },

  toggleTraffic(show) {
    if (!mapInstance || !trafficOverlayGroup) return;
    if (show) {
      mapInstance.addLayer(trafficOverlayGroup);
    } else {
      mapInstance.removeLayer(trafficOverlayGroup);
    }
  }
};
