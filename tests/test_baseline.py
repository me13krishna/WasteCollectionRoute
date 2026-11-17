import pytest
from backend.services.baseline_solver import baseline_solver
from backend.config import settings

def test_baseline_solver():
    locations = [
        {"id": 0, "name": "Depot", "lat": 18.5284, "lon": 73.8480, "demand_kg": 0.0, "service_time_sec": 0, "priority": 0},
        {"id": 1, "name": "Stop 1", "lat": 18.5300, "lon": 73.8500, "demand_kg": 80.0, "service_time_sec": 90, "priority": 1},
        {"id": 2, "name": "Stop 2", "lat": 18.5320, "lon": 73.8520, "demand_kg": 90.0, "service_time_sec": 90, "priority": 2},
    ]
    vehicles = [
        {"id": 1, "name": "Truck #1", "capacity_kg": 500.0, "max_duration_sec": 28800}
    ]
    n = 3
    dist_matrix = [[1000.0 if i != j else 0.0 for j in range(n)] for i in range(n)]
    dur_matrix = [[120.0 if i != j else 0.0 for j in range(n)] for i in range(n)]

    res = baseline_solver.solve(
        depot_index=0,
        locations=locations,
        vehicles=vehicles,
        distance_matrix=dist_matrix,
        duration_matrix=dur_matrix
    )

    assert res["algorithm"] == "Nearest-Neighbor Constructive Baseline"
    assert "metrics" in res
    assert res["metrics"]["stops_served"] == 2
    assert res["metrics"]["unserved_count"] == 0
    assert res["metrics"]["est_fuel_liters"] > 0
    assert res["metrics"]["est_total_cost"] > 0
    assert res["metrics"]["currency_symbol"] == settings.CURRENCY_SYMBOL
