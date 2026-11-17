/**
 * EcoRoute AI - Chart.js Visualization Module
 * Handles executive KPI comparison charts and vehicle workload balance charts.
 */

let comparisonChartInstance = null;
let workloadChartInstance = null;

export const charts = {
  renderComparisonChart(canvasId, baseline, optimized, currencySymbol = '₹') {
    const ctx = document.getElementById(canvasId)?.getContext('2d');
    if (!ctx) return;

    if (comparisonChartInstance) {
      comparisonChartInstance.destroy();
    }

    const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
    const textColor = isDark ? '#94a3b8' : '#475569';
    const gridColor = isDark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.08)';

    const labels = ['Distance (km)', 'Fuel (L)', 'CO₂ (kg)', `Cost (${currencySymbol})`];
    const baselineData = [
      baseline.total_distance_km || 0,
      baseline.est_fuel_liters || 0,
      baseline.est_co2_kg || 0,
      baseline.est_total_cost || 0
    ];
    const optimizedData = [
      optimized.total_distance_km || 0,
      optimized.est_fuel_liters || 0,
      optimized.est_co2_kg || 0,
      optimized.est_total_cost || 0
    ];

    comparisonChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [
          {
            label: 'Nearest-Neighbor Baseline',
            data: baselineData,
            backgroundColor: isDark ? 'rgba(148, 163, 184, 0.5)' : 'rgba(100, 116, 139, 0.5)',
            borderColor: isDark ? '#94a3b8' : '#64748b',
            borderWidth: 1,
            borderRadius: 6
          },
          {
            label: 'Google OR-Tools CVRP',
            data: optimizedData,
            backgroundColor: 'rgba(16, 185, 129, 0.8)',
            borderColor: '#10b981',
            borderWidth: 1,
            borderRadius: 6
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: 'top',
            labels: { color: textColor, font: { family: 'Inter', size: 11 } }
          },
          tooltip: {
            callbacks: {
              label: function(context) {
                return ` ${context.dataset.label}: ${context.raw.toLocaleString()}`;
              }
            }
          }
        },
        scales: {
          x: {
            ticks: { color: textColor, font: { family: 'Inter', size: 11 } },
            grid: { display: false }
          },
          y: {
            ticks: { color: textColor, font: { family: 'Inter', size: 10 } },
            grid: { color: gridColor }
          }
        }
      }
    });
  },

  renderWorkloadChart(canvasId, routes) {
    const ctx = document.getElementById(canvasId)?.getContext('2d');
    if (!ctx) return;

    if (workloadChartInstance) {
      workloadChartInstance.destroy();
    }

    const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
    const textColor = isDark ? '#94a3b8' : '#475569';
    const gridColor = isDark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.08)';

    const labels = routes.map(r => r.vehicle_name.split('(')[0].trim());
    const payloadData = routes.map(r => r.total_demand_kg);
    const capacityData = routes.map(r => r.vehicle_capacity_kg);

    workloadChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [
          {
            label: 'Payload (kg)',
            data: payloadData,
            backgroundColor: 'rgba(6, 182, 212, 0.8)',
            borderColor: '#06b6d4',
            borderWidth: 1,
            borderRadius: 4
          },
          {
            label: 'Max Capacity (kg)',
            data: capacityData,
            backgroundColor: isDark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.1)',
            borderColor: isDark ? 'rgba(255, 255, 255, 0.2)' : 'rgba(0, 0, 0, 0.2)',
            borderWidth: 1,
            borderRadius: 4
          }
        ]
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: 'top',
            labels: { color: textColor, font: { family: 'Inter', size: 10 } }
          }
        },
        scales: {
          x: {
            ticks: { color: textColor, font: { family: 'Inter', size: 10 } },
            grid: { color: gridColor }
          },
          y: {
            ticks: { color: textColor, font: { family: 'Inter', size: 10 } },
            grid: { display: false }
          }
        }
      }
    });
  }
};
