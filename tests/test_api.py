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
    assert "EcoRoute AI" in index_resp.text
    assert "Pune" in index_resp.text

    # Test CSS file
    css_resp = client.get("/static/css/styles.css")
    assert css_resp.status_code == 200
    assert "--accent-primary" in css_resp.text

    # Test JS files
    js_app = client.get("/static/js/app.js")
    assert js_app.status_code == 200
    assert "EcoRouteApp" in js_app.text

    js_map = client.get("/static/js/map.js")
    assert js_map.status_code == 200
    assert "mapManager" in js_map.text
