import os
import csv
import io
import time
import json
import logging
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from backend.config import settings
from backend.services.routing_matrix import routing_service
from backend.services.baseline_solver import baseline_solver
from backend.services.vrp_solver import vrp_solver

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("EcoRouteAI")

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Software-only AI-Powered Municipal Waste Collection Route Optimization Platform",
    version="1.0.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory working state for fast MVP presentation
SAMPLE_CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "sample_dataset.csv")

DEFAULT_DEPOT = {
    "id": 0,
    "name": "Central Municipal Solid Waste Depot",
    "lat": 12.9716,
    "lon": 77.5946,
    "address": "Depot #1, Cubbon Road, Central Ward"
}

DEFAULT_VEHICLES = [
    {"id": 1, "name": "Waste Compactor Truck #1", "capacity_kg": 1500.0, "max_duration_sec": 28800, "status": "available", "color": "#10b981"},
    {"id": 2, "name": "Waste Compactor Truck #2", "capacity_kg": 1400.0, "max_duration_sec": 28800, "status": "available", "color": "#06b6d4"},
    {"id": 3, "name": "Refuse Collection Truck #3", "capacity_kg": 1600.0, "max_duration_sec": 28800, "status": "available", "color": "#8b5cf6"},
    {"id": 4, "name": "Electric Refuse Truck #4", "capacity_kg": 1300.0, "max_duration_sec": 28800, "status": "available", "color": "#f59e0b"}
]

current_locations: List[Dict[str, Any]] = []
current_depot = dict(DEFAULT_DEPOT)
current_vehicles = list(DEFAULT_VEHICLES)
current_incidents: List[Dict[str, Any]] = []

def load_initial_dataset():
    global current_locations
    current_locations = []
    if os.path.exists(SAMPLE_CSV_PATH):
        with open(SAMPLE_CSV_PATH, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                current_locations.append({
                    "id": int(row["id"]),
                    "name": row["name"],
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                    "demand_kg": float(row["demand_kg"]),
                    "service_time_sec": int(row["service_time_sec"]),
                    "priority": int(row["priority"]),
                    "required": row.get("required", "true").lower() == "true",
                    "status": "pending"
                })
    logger.info(f"Loaded {len(current_locations)} collection points from sample dataset.")

load_initial_dataset()

# Schemas
class OptimizeRequest(BaseModel):
    selected_stop_count: Optional[int] = 50  # for demo speed / responsiveness
    objective: Optional[str] = "distance"
    time_limit_sec: Optional[int] = 10
    active_vehicles_count: Optional[int] = 4

class IncidentRequest(BaseModel):
    incident_type: str  # "road_closure", "missed_stop", "urgent_pickup"
    description: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    stop_id: Optional[int] = None

# --- API Endpoints ---

@app.get("/api/v1/dataset")
async def get_dataset():
    return {
        "depot": current_depot,
        "vehicles": current_vehicles,
        "locations": current_locations,
        "incidents": current_incidents,
        "total_stops": len(current_locations)
    }

@app.post("/api/v1/optimize")
async def run_optimization(req: OptimizeRequest):
    """
    Runs Google OR-Tools CVRP solver with Guided Local Search.
    Uses OSRM road-network distance matrix and generates road geometries.
    """
    n_stops = min(req.selected_stop_count or 50, len(current_locations))
    active_stops = current_locations[:n_stops]
    fleet = current_vehicles[:(req.active_vehicles_count or 4)]

    # Depot + stops coordinates
    all_nodes = [current_depot] + active_stops
    coords = [(node["lat"], node["lon"]) for node in all_nodes]

    # 1. Distance & Duration Matrix via OSRM (or road-network topology fallback)
    distances, durations = await routing_service.get_distance_and_duration_matrix(coords)

    # Apply road closures if active
    for inc in current_incidents:
        if inc.get("active") and inc.get("incident_type") == "road_closure" and inc.get("lat"):
            inc_lat = inc["lat"]
            inc_lon = inc["lon"]
            for i in range(len(coords)):
                for j in range(len(coords)):
                    if i != j:
                        mid_lat = (coords[i][0] + coords[j][0]) / 2.0
                        mid_lon = (coords[i][1] + coords[j][1]) / 2.0
                        if abs(mid_lat - inc_lat) < 0.008 and abs(mid_lon - inc_lon) < 0.008:
                            distances[i][j] *= 8.0
                            durations[i][j] *= 8.0

    # 2. Solve CVRP with Google OR-Tools (Guided Local Search)
    solution = vrp_solver.solve_cvrp(
        depot_index=0,
        locations=all_nodes,
        vehicles=fleet,
        distance_matrix=distances,
        duration_matrix=durations,
        time_limit_sec=req.time_limit_sec or 10,
        objective=req.objective or "distance"
    )

    # 3. Attach full road geometry to each vehicle route
    for route in solution["routes"]:
        pts = [(s["lat"], s["lon"]) for s in route["stops"]]
        geometry = await routing_service.get_route_geometry(pts)
        route["geometry"] = geometry
        # assign vehicle color
        veh_match = next((v for v in fleet if v["id"] == route["vehicle_id"]), None)
        route["color"] = veh_match["color"] if veh_match else "#3b82f6"

    # Also compute baseline for immediate side-by-side comparison
    base_sol = baseline_solver.solve(
        depot_index=0,
        locations=all_nodes,
        vehicles=fleet,
        distance_matrix=distances,
        duration_matrix=durations
    )

    # Compute comparison deltas
    opt_m = solution["metrics"]
    base_m = base_sol["metrics"]

    def calc_delta(b_val, o_val):
        diff = b_val - o_val
        pct = (diff / b_val * 100.0) if b_val > 0 else 0.0
        return round(diff, 2), round(pct, 1)

    dist_diff, dist_pct = calc_delta(base_m["total_distance_km"], opt_m["total_distance_km"])
    fuel_diff, fuel_pct = calc_delta(base_m["est_fuel_liters"], opt_m["est_fuel_liters"])
    co2_diff, co2_pct = calc_delta(base_m["est_co2_kg"], opt_m["est_co2_kg"])
    cost_diff, cost_pct = calc_delta(base_m["est_total_cost"], opt_m["est_total_cost"])

    comparison = {
        "baseline": base_m,
        "optimized": opt_m,
        "savings": {
            "distance_km": dist_diff,
            "distance_percent": dist_pct,
            "fuel_liters": fuel_diff,
            "fuel_percent": fuel_pct,
            "co2_kg": co2_diff,
            "co2_percent": co2_pct,
            "cost_dollars": cost_diff,
            "cost_percent": cost_pct
        }
    }

    return {
        "success": True,
        "solution": solution,
        "comparison": comparison,
        "fleet": fleet,
        "stops_count": len(active_stops)
    }

@app.post("/api/v1/incidents")
async def report_incident(inc: IncidentRequest):
    """
    Registers an operational event (road closure, missed stop, urgent demand)
    triggering software-driven dynamic reoptimization.
    """
    incident_obj = {
        "id": len(current_incidents) + 1,
        "incident_type": inc.incident_type,
        "description": inc.description,
        "lat": inc.lat or 12.9750,
        "lon": inc.lon or 77.6010,
        "stop_id": inc.stop_id,
        "active": True,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    current_incidents.append(incident_obj)

    # If missed stop, mark stop status
    if inc.incident_type == "missed_stop" and inc.stop_id:
        for s in current_locations:
            if s["id"] == inc.stop_id:
                s["status"] = "missed"

    # If urgent pickup, insert new stop
    if inc.incident_type == "urgent_pickup":
        new_id = max(s["id"] for s in current_locations) + 1 if current_locations else 1
        new_stop = {
            "id": new_id,
            "name": f"URGENT: {inc.description}",
            "lat": inc.lat or (current_depot["lat"] + 0.012),
            "lon": inc.lon or (current_depot["lon"] + 0.015),
            "demand_kg": 120.0,
            "service_time_sec": 180,
            "priority": 4,  # Urgent
            "required": True,
            "status": "pending"
        }
        current_locations.insert(0, new_stop)

    return {"success": True, "incident": incident_obj, "total_incidents": len(current_incidents)}

@app.post("/api/v1/reset-dataset")
async def reset_data():
    global current_incidents
    current_incidents = []
    load_initial_dataset()
    return {"success": True, "message": "Dataset reset to clean 80 municipal collection points."}

# Mount static frontend directory
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
async def serve_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>EcoRoute AI Backend is running</h1>")
