import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.config import settings

client = TestClient(app)

def test_api_config():
    resp = client.get("/api/v1/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["currency_symbol"] == "₹"
    assert data["currency_code"] == "INR"
    assert "Pune" in data["city"]
    assert "constants" in data

def test_api_routing_status():
    resp = client.get("/api/v1/routing/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "provider" in data
    assert "fallback_mode" in data

def test_api_dataset():
    resp = client.get("/api/v1/dataset")
    assert resp.status_code == 200
    data = resp.json()
    assert "depot" in data
    assert "vehicles" in data
    assert "locations" in data
    assert len(data["locations"]) > 0

def test_api_traffic_status():
    resp = client.get("/api/v1/traffic/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "hotspots" in data
    assert len(data["hotspots"]) > 0

def test_api_optimize_and_incidents():
    # 1. Optimize 10 stops
    opt_resp = client.post("/api/v1/optimize", json={
        "selected_stop_count": 10,
        "objective": "fuel",
        "time_limit_sec": 3,
        "active_vehicles_count": 2,
        "fleet_mode": "auto_minimize_vehicles",
        "traffic_enabled": True
    })
    assert opt_resp.status_code == 200
    opt_data = opt_resp.json()
    assert opt_data["success"] is True
    assert "solution" in opt_data
    assert "comparison" in opt_data
    assert opt_data["solution"]["metrics"]["total_distance_km"] > 0

    # 2. Report road closure incident
    inc_resp = client.post("/api/v1/incidents", json={
        "incident_type": "road_closure",
        "description": "FC Road Metro drainage work",
        "lat": 18.5204,
        "lon": 73.8425,
        "auto_reoptimize": True
    })
    assert inc_resp.status_code == 200
    inc_data = inc_resp.json()
    assert inc_data["success"] is True
    assert inc_data["incident"]["incident_type"] == "road_closure"
    assert inc_data["reoptimized"] is True

    # 3. Undo incident
    undo_resp = client.post("/api/v1/incidents/undo")
    assert undo_resp.status_code == 200
    undo_data = undo_resp.json()
    assert undo_data["success"] is True

    # 4. Reset dataset
    reset_resp = client.post("/api/v1/incidents/reset")
    assert reset_resp.status_code == 200
    reset_data = reset_resp.json()
    assert reset_data["success"] is True

def test_randomize_gives_different_points_each_time_and_is_seedable():
    a = client.post("/api/v1/dataset/randomize", json={"count": 30}).json()
    pts_a = client.get("/api/v1/dataset").json()["locations"]
    client.post("/api/v1/dataset/randomize", json={"count": 30})
    pts_b = client.get("/api/v1/dataset").json()["locations"]
    assert a["success"] is True and a["total_stops"] == 30
    assert len(pts_a) == len(pts_b) == 30
    assert [(p["lat"], p["lon"]) for p in pts_a] != [(p["lat"], p["lon"]) for p in pts_b]

    # Same seed -> same layout
    client.post("/api/v1/dataset/randomize", json={"count": 12, "seed": 7})
    s1 = client.get("/api/v1/dataset").json()["locations"]
    client.post("/api/v1/dataset/randomize", json={"count": 12, "seed": 7})
    s2 = client.get("/api/v1/dataset").json()["locations"]
    assert s1 == s2

    # Points sit within the requested radius of the depot (4 km default, loose bound for lon scaling)
    depot = client.get("/api/v1/dataset").json()["depot"]
    for p in s1:
        assert abs(p["lat"] - depot["lat"]) < 0.05 and abs(p["lon"] - depot["lon"]) < 0.05
        assert p["demand_kg"] > 0

    client.post("/api/v1/reset-dataset")


def test_optimize_exact_fleet_dispatches_requested_trucks():
    client.post("/api/v1/dataset/randomize", json={"count": 20, "seed": 3})
    resp = client.post("/api/v1/optimize", json={
        "selected_stop_count": 20, "objective": "distance", "time_limit_sec": 2,
        "active_vehicles_count": 3, "fleet_mode": "exact_fleet"
    })
    assert resp.status_code == 200
    m = resp.json()["solution"]["metrics"]
    assert m["vehicles_used"] == 3
    assert m["unserved_count"] == 0
    client.post("/api/v1/reset-dataset")


def test_fleet_sweep_compares_truck_counts_on_same_points():
    client.post("/api/v1/dataset/randomize", json={"count": 24, "seed": 11})
    resp = client.post("/api/v1/optimize/fleet-sweep", json={
        "selected_stop_count": 24, "min_trucks": 1, "max_trucks": 5, "time_limit_sec": 1
    })
    assert resp.status_code == 200
    data = resp.json()
    assert [r["trucks"] for r in data["runs"]] == [1, 2, 3, 4, 5]
    feasible = [r for r in data["runs"] if r["feasible"]]
    assert len(feasible) == 5
    for r in feasible:
        assert r["trucks_dispatched"] == r["trucks"]
        assert r["stops_served"] == 24
        assert r["total_distance_km"] > 0
        assert len(r["routes"]) == r["trucks"]
    # Recommendation is the feasible count with the least distance
    best = min(feasible, key=lambda r: r["total_distance_km"])
    assert data["best_trucks"] == best["trucks"]
    assert next(r for r in data["runs"] if r["trucks"] == data["best_trucks"])["extra_km_vs_best"] == 0
    # Truck capacity is not a constraint: even one truck covers everything (within the shift limit)
    assert data["runs"][0]["feasible"] is True
    client.post("/api/v1/reset-dataset")


def test_fleet_stream_finds_u_shaped_optimum_and_stops_one_past_it():
    import json
    client.post("/api/v1/dataset/randomize", json={"scenario": 3})
    events = []
    with client.stream("POST", "/api/v1/optimize/fleet-sweep/stream", json={
        "max_trucks": 6, "time_limit_sec": 1, "traffic_enabled": False
    }) as resp:
        assert resp.status_code == 200
        events = [json.loads(line) for line in resp.iter_lines() if line]
    assert events[0]["type"] == "start" and events[-1]["type"] == "done"
    runs = [e["run"] for e in events if e["type"] == "run"]
    best = events[-1]["best_trucks"]
    # counts are tried in order and the search ends exactly one truck after the minimum
    assert [r["trucks"] for r in runs] == list(range(1, len(runs) + 1))
    assert len(runs) in (best + 2, 8)  # two trucks past the minimum (or the fleet limit)
    dist = {r["trucks"]: r["total_distance_km"] for r in runs}
    assert best > 1 and dist[best] == min(dist.values())
    assert dist[1] > dist[best] and dist[best + 1] > dist[best] and dist[best + 2 if best + 2 in dist else best + 1] >= dist[best + 1]  # the U: down, then back up
    for r in runs:
        assert len(r["routes"]) == r["trucks"]
        assert all(len(t["geometry"]) > 1 for t in r["routes"])
    client.post("/api/v1/reset-dataset")


def test_api_exports():
    # Run a small optimization first to ensure last_solution exists
    client.post("/api/v1/optimize", json={"selected_stop_count": 6, "time_limit_sec": 2})

    # Test GeoJSON export
    geo_resp = client.get("/api/v1/export/geojson")
    assert geo_resp.status_code == 200
    assert geo_resp.headers["content-type"].startswith("application/geo+json")
    geo_data = geo_resp.json()
    assert geo_data["type"] == "FeatureCollection"
    assert len(geo_data["features"]) > 0

    # Test Manifest CSV export
    man_resp = client.get("/api/v1/export/manifest")
    assert man_resp.status_code == 200
    assert man_resp.headers["content-type"].startswith("text/csv")
    assert "Vehicle ID" in man_resp.text

    # Test Template CSV
    tpl_resp = client.get("/api/v1/export/template")
    assert tpl_resp.status_code == 200
    assert "name,lat,lon" in tpl_resp.text

    # Test Printable HTML Report
    html_resp = client.get("/api/v1/export/report-html")
    assert html_resp.status_code == 200
    assert "EcoRoute AI Driver Dispatch Manifest" in html_resp.text

def test_static_files():
    # Test index HTML
    index_resp = client.get("/")
    assert index_resp.status_code == 200
    assert "EcoRoute" in index_resp.text
    assert "Pune" in index_resp.text

    # Test CSS file
    css_resp = client.get("/static/css/styles.css")
    assert css_resp.status_code == 200
    assert "--accent" in css_resp.text

    js_util = client.get("/static/js/util.js")
    assert js_util.status_code == 200

    # Test JS files
    js_app = client.get("/static/js/app.js")
    assert js_app.status_code == 200
    assert "EcoRouteApp" in js_app.text

    js_map = client.get("/static/js/map.js")
    assert js_map.status_code == 200
    assert "mapManager" in js_map.text
