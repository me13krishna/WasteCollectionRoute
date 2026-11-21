/**
 * EcoRoute - charts
 * Baseline vs optimized comparison and per-truck load.
 * Values are shown relative to the baseline (=100) so km, litres, kg and rupees share one axis.
 */

import { cssVar } from './util.js';

let comparisonChartInstance = null;
let workloadChartInstance = null;
let fleetChartInstance = null;
let uCurveInstance = null;

function theme() {
  return {
    text: cssVar('--muted'),
    grid: cssVar('--border'),
    base: cssVar('--border'),
    accent: cssVar('--accent'),
    font: { family: cssVar('--font') || 'system-ui', size: 11 }
  };
}

export const charts = {
  renderComparisonChart(canvasId, baseline, optimized, currencySymbol = '₹') {
    const ctx = document.getElementById(canvasId)?.getContext('2d');
    if (!ctx) return;
    comparisonChartInstance?.destroy();
    const t = theme();

    const keys = [
      ['Distance', 'total_distance_km', 'km'],
      ['Diesel', 'est_fuel_liters', 'L'],
      ['CO₂', 'est_co2_kg', 'kg'],
      ['Cost', 'est_total_cost', currencySymbol]
    ];
    const raw = keys.map(([, k]) => [baseline[k] || 0, optimized[k] || 0]);
    const rel = raw.map(([b, o]) => (b > 0 ? (o / b) * 100 : 0));

    comparisonChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: keys.map(k => k[0]),
        datasets: [
          { label: 'Nearest-neighbor', data: keys.map(() => 100), backgroundColor: t.base, borderRadius: 3, barPercentage: 0.7 },
          { label: 'Optimized', data: rel, backgroundColor: t.accent, borderRadius: 3, barPercentage: 0.7 }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        plugins: {
          legend: { position: 'bottom', align: 'start', labels: { color: t.text, font: t.font, boxWidth: 8, boxHeight: 8, useBorderRadius: true, borderRadius: 2 } },
          tooltip: {
            callbacks: {
              label: (c) => {
                const [b, o] = raw[c.dataIndex];
                const unit = keys[c.dataIndex][2];
                const v = c.datasetIndex === 0 ? b : o;
                return ` ${c.dataset.label}: ${v.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${unit}`;
              }
            }
          }
        },
        scales: {
          x: { ticks: { color: t.text, font: t.font }, grid: { display: false }, border: { color: t.grid } },
          y: {
            min: 0, max: 110,
            ticks: { color: t.text, font: t.font, stepSize: 25, callback: v => (v <= 100 ? `${v}%` : '') },
            grid: { color: t.grid }, border: { display: false }
          }
        }
      }
    });
  },

  /** Total distance per truck count (bars) against when the last truck is home (line). */
  renderFleetChart(canvasId, runs, bestTrucks) {
    const ctx = document.getElementById(canvasId)?.getContext('2d');
    if (!ctx) return;
    fleetChartInstance?.destroy();
    const t = theme();
    const line = cssVar('--p3');

    fleetChartInstance = new Chart(ctx, {
      data: {
        labels: runs.map(r => String(r.trucks)),
        datasets: [
          {
            type: 'bar',
            label: 'Total distance (km)',
            data: runs.map(r => r.total_distance_km),
            backgroundColor: runs.map(r => (r.trucks === bestTrucks ? t.accent : r.feasible ? t.text : t.base)),
            borderRadius: 3,
            barPercentage: 0.6,
            yAxisID: 'y',
            order: 2
          },
          {
            type: 'line',
            label: 'Finish time (min)',
            data: runs.map(r => r.longest_route_min),
            borderColor: line,
            backgroundColor: line,
            borderWidth: 2,
            pointRadius: 3,
            tension: 0.2,
            spanGaps: false,
            yAxisID: 'y2',
            order: 1
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        plugins: {
          legend: { position: 'bottom', align: 'start', labels: { color: t.text, font: t.font, boxWidth: 8, boxHeight: 8, useBorderRadius: true, borderRadius: 2 } },
          tooltip: {
            callbacks: {
              title: (items) => `${items[0].label} truck${items[0].label === '1' ? '' : 's'}`,
              afterBody: (items) => {
                const r = runs[items[0].dataIndex];
                return r.feasible ? '' : (r.reason || 'Not every stop is served');
              }
            }
          }
        },
        scales: {
          x: { title: { display: true, text: 'Trucks dispatched', color: t.text, font: t.font }, ticks: { color: t.text, font: t.font }, grid: { display: false }, border: { color: t.grid } },
          y: { beginAtZero: true, ticks: { color: t.text, font: t.font }, grid: { color: t.grid }, border: { display: false } },
          y2: { position: 'right', beginAtZero: true, ticks: { color: line, font: t.font }, grid: { display: false }, border: { display: false } }
        }
      }
    });
  },

  /**
   * The U-shaped trade-off, filled in live as each truck count finishes.
   * Solid line: effective distance (driving plus the penalty for too few trucks).
   * Dashed line: driving only.
   * Pass `best` once known to mark the optimum. Pass `reset` to start a fresh chart.
   */
  renderUCurve(canvasId, runs, best, maxTrucks, { reset = false, selected = null } = {}) {
    const canvas = document.getElementById(canvasId);
    const ctx = canvas?.getContext('2d');
    if (!ctx) return;
    const t = theme();
    const labels = Array.from({ length: maxTrucks }, (_, i) => String(i + 1));
    const byK = new Map(runs.map(r => [r.trucks, r]));
    const total = labels.map((_, i) => byK.get(i + 1)?.total_distance_km ?? null);
    const barColors = labels.map((_, i) => (i + 1 === best ? t.accent : i + 1 === selected ? cssVar('--text') : t.text + '70'));
    const borders = labels.map((_, i) => (i + 1 === selected ? cssVar('--text') : 'transparent'));

    if (reset) { uCurveInstance?.destroy(); uCurveInstance = null; }
    if (uCurveInstance && uCurveInstance.canvas === canvas) {
      const [a] = uCurveInstance.data.datasets;
      uCurveInstance.data.labels = labels;
      a.data = total;
      a.backgroundColor = barColors; a.borderColor = borders;
      uCurveInstance.update();
      return;
    }

    uCurveInstance = new Chart(ctx, {
      data: {
        labels,
        datasets: [
          {
            type: 'bar', label: 'Total distance (km)', data: total, backgroundColor: barColors,
            borderColor: borders, borderWidth: 2, borderRadius: 4, barPercentage: 0.7, order: 2
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 450 },
        interaction: { mode: 'index', intersect: false },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : ''; },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => `${items[0].label} truck${items[0].label === '1' ? '' : 's'}`,
              label: (c) => (c.parsed.y == null ? '' : ` ${c.dataset.label}: ${c.parsed.y.toLocaleString(undefined, { maximumFractionDigits: 1 })} km`)
            }
          }
        },
        scales: {
          x: { title: { display: true, text: 'Trucks dispatched', color: t.text, font: t.font }, ticks: { color: t.text, font: t.font }, grid: { display: false }, border: { color: t.grid } },
          y: { beginAtZero: true, title: { display: true, text: 'km', color: t.text, font: t.font }, ticks: { color: t.text, font: t.font }, grid: { color: t.grid }, border: { display: false } }
        },
        onClick: (_e, els) => { if (els.length) canvas.dispatchEvent(new CustomEvent('ucurve-pick', { detail: els[0].index + 1 })); }
      }
    });
  },

  renderWorkloadChart(canvasId, routes) {
    const ctx = document.getElementById(canvasId)?.getContext('2d');
    if (!ctx) return;
    workloadChartInstance?.destroy();
    const t = theme();

    workloadChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: routes.map(r => r.vehicle_name.split('(')[0].trim()),
        datasets: [
          {
            label: 'Load (kg)',
            data: routes.map(r => r.total_demand_kg),
            backgroundColor: routes.map(r => r.color || t.accent),
            borderRadius: 3,
            barPercentage: 0.6,
            grouped: false,
            order: 1
          }
        ]
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: t.text, font: t.font }, grid: { color: t.grid }, border: { display: false } },
          y: { ticks: { color: t.text, font: t.font }, grid: { display: false }, border: { color: t.grid } }
        }
      }
    });
  }
};
