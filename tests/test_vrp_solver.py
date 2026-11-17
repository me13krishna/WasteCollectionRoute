import pytest
from backend.services.vrp_solver import vrp_solver, compute_route_fuel
from backend.config import settings

def test_compute_route_fuel():
    # 3 stops (depot -> stop1 -> depot)
    stops = [
        {"node_idx": 0, "cumulative_kg": 0.0},
        {"node_idx": 1, "cumulative_kg": 200.0},
        {"node_idx": 0, "cumulative_kg": 200.0}
    ]
    # 5 km between depot and stop1
    dist_matrix = [
        [0.0, 5000.0],
        [5000.0, 0.0]
    ]
    # 600 seconds duration (approx 30 km/h)
    dur_matrix = [
        [0.0, 600.0],
        [600.0, 0.0]
    ]
    fuel = compute_route_fuel(stops, dist_matrix, dur_matrix, vehicle_capacity_kg=1500.0)
    assert "total_fuel_liters" in fuel
    assert "cruising_fuel_liters" in fuel
    assert "traffic_idling_fuel_liters" in fuel
    assert "compactor_pto_fuel_liters" in fuel
    assert fuel["total_fuel_liters"] > 0

def test_vrp_solver_validation_error():
    # Empty locations should return validation error without crashing
    res = vrp_solver.solve_cvrp(
        depot_index=0,
        locations=[],
        vehicles=[{"id": 1, "capacity_kg": 1500}],
        distance_matrix=[],
        duration_matrix=[]
    )
    assert res["algorithm"] == "Validation Error"
    assert "error" in res

def test_vrp_solver_small_instance():
    # 1 depot + 4 collection stops, 2 vehicles
    locations = [
        {"id": 0, "name": "Depot", "lat": 18.5284, "lon": 73.8480, "demand_kg": 0.0, "service_time_sec": 0, "priority": 0},
        {"id": 1, "name": "Stop A", "lat": 18.5300, "lon": 73.8500, "demand_kg": 100.0, "service_time_sec": 90, "priority": 1},
        {"id": 2, "name": "Stop B", "lat": 18.5320, "lon": 73.8520, "demand_kg": 150.0, "service_time_sec": 90, "priority": 2},
        {"id": 3, "name": "Stop C", "lat": 18.5250, "lon": 73.8450, "demand_kg": 200.0, "service_time_sec": 90, "priority": 3},
        {"id": 4, "name": "Stop D", "lat": 18.5220, "lon": 73.8420, "demand_kg": 120.0, "service_time_sec": 90, "priority": 4},
    ]
    vehicles = [
        {"id": 1, "name": "Truck #1", "capacity_kg": 400.0, "max_duration_sec": 28800},
        {"id": 2, "name": "Truck #2", "capacity_kg": 400.0, "max_duration_sec": 28800}
    ]
    n = len(locations)
    # Simple Euclidean-based matrix
    dist_matrix = [[0.0] * n for _ in range(n)]
    dur_matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                d = abs(locations[i]["lat"] - locations[j]["lat"]) * 111000 + abs(locations[i]["lon"] - locations[j]["lon"]) * 111000
                dist_matrix[i][j] = d
                dur_matrix[i][j] = d / 7.5

    res = vrp_solver.solve_cvrp(
        depot_index=0,
        locations=locations,
        vehicles=vehicles,
        distance_matrix=dist_matrix,
        duration_matrix=dur_matrix,
        time_limit_sec=2,
        objective="distance"
    )

    assert "routes" in res
    assert "metrics" in res
    assert res["metrics"]["stops_served"] > 0
    # Capacity check: each route load <= vehicle capacity
    for r in res["routes"]:
        assert r["total_demand_kg"] <= 400.0

def test_vrp_solver_infeasible_capacity_diagnosis():
    # Demand exceeds fleet capacity: total demand 1000kg, fleet capacity 200kg
    locations = [
        {"id": 0, "name": "Depot", "lat": 18.5284, "lon": 73.8480, "demand_kg": 0.0, "service_time_sec": 0, "priority": 0},
        {"id": 1, "name": "Heavy Stop 1", "lat": 18.5300, "lon": 73.8500, "demand_kg": 500.0, "service_time_sec": 90, "priority": 1},
        {"id": 2, "name": "Heavy Stop 2", "lat": 18.5320, "lon": 73.8520, "demand_kg": 500.0, "service_time_sec": 90, "priority": 4},
    ]
    vehicles = [
        {"id": 1, "name": "Small Van", "capacity_kg": 200.0, "max_duration_sec": 28800}
    ]
    n = 3
    dist_matrix = [[1000.0 if i != j else 0.0 for j in range(n)] for i in range(n)]
    dur_matrix = [[120.0 if i != j else 0.0 for j in range(n)] for i in range(n)]

    res = vrp_solver.solve_cvrp(
        depot_index=0,
        locations=locations,
        vehicles=vehicles,
        distance_matrix=dist_matrix,
        duration_matrix=dur_matrix,
        time_limit_sec=1
    )

    assert res["metrics"]["unserved_count"] > 0
    assert len(res["unserved_stops"]) > 0
    # The heavy stop should have diagnostic reason
    assert any(s["reason"] in ["capacity_exceeded", "priority_dropped", "fleet_exhausted"] for s in res["unserved_stops"])
