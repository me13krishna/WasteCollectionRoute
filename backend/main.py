import os
import csv
import io
import time
import json
import logging
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from pydantic import BaseModel

from backend.config import settings
from backend.database import init_db
from backend.services.routing_matrix import routing_service
from backend.services.baseline_solver import baseline_solver
from backend.services.vrp_solver import vrp_solver
from backend.services.osm_extractor import osm_extractor
from backend.services.traffic_service import traffic_engine, PUNE_TRAFFIC_HOTSPOTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("EcoRouteAI")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()
        logger.info("Database initialized successfully (SQLite / PostGIS models synced).")
    except Exception as e:
        logger.warning(f"Database initialization warning: {e}")
    yield

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Software-only AI-Powered Municipal Waste Collection Route Optimization Platform",
    version="1.0.0",
    lifespan=lifespan
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
    "name": "PMC Central Solid Waste Transfer Depot",
    "lat": 18.5284,
    "lon": 73.8480,
    "address": "Ghole Road, Shivajinagar, Pune Municipal Corporation (PMC)"
}

DEFAULT_VEHICLES = [
    {"id": 1, "name": "PMC Compactor Truck #1 (West Zone)", "capacity_kg": 1500.0, "max_duration_sec": 28800, "status": "available", "color": "#00E676"},
    {"id": 2, "name": "PMC Refuse Truck #2 (East Zone)", "capacity_kg": 1400.0, "max_duration_sec": 28800, "status": "available", "color": "#00E5FF"},
    {"id": 3, "name": "PMC Compactor Truck #3 (Central Peths)", "capacity_kg": 1600.0, "max_duration_sec": 28800, "status": "available", "color": "#D500F9"},
    {"id": 4, "name": "PMC Swachh Truck #4 (South Zone)", "capacity_kg": 1300.0, "max_duration_sec": 28800, "status": "available", "color": "#FF9100"}
]

current_locations: List[Dict[str, Any]] = []
current_depot = dict(DEFAULT_DEPOT)
current_vehicles = list(DEFAULT_VEHICLES)
current_incidents: List[Dict[str, Any]] = []
last_solution: Optional[Dict[str, Any]] = None

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
    objective: Optional[str] = "fuel"  # "fuel", "traffic_duration", "distance"
    time_limit_sec: Optional[int] = 10
    active_vehicles_count: Optional[int] = 4
    fleet_mode: Optional[str] = "auto_minimize_vehicles"  # "auto_minimize_vehicles", "fixed_fleet"
    traffic_enabled: Optional[bool] = True

class IncidentRequest(BaseModel):
    incident_type: str  # "road_closure", "traffic_jam", "missed_stop", "urgent_pickup"
    description: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    stop_id: Optional[int] = None

class OsmExtractRequest(BaseModel):
    city: Optional[str] = "Pune"
    lat: Optional[float] = None
    lon: Optional[float] = None
    radius_meters: Optional[int] = 5000
    limit: Optional[int] = 60

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

@app.get("/api/v1/traffic/status")
async def get_traffic_status():
    """
    Returns real-time urban traffic congestion conditions and active bottleneck hotspots.
    """
    base_factor, period_name, period_desc = traffic_engine.get_time_of_day_congestion_factor()
    return {
        "success": True,
        "current_time": time.strftime("%H:%M:%S"),
        "period_name": period_name,
        "period_desc": period_desc,
        "base_congestion_factor": base_factor,
        "hotspots": PUNE_TRAFFIC_HOTSPOTS,
        "active_incidents_count": len([i for i in current_incidents if i.get("active")])
    }

@app.post("/api/v1/optimize")
async def run_optimization(req: OptimizeRequest):
    """
    Runs Google OR-Tools CVRP solver with Guided Local Search.
    Enriched with real-time dynamic traffic congestion, fleet size minimization,
    and load-dependent fuel & emissions optimization.
    """
    n_stops = min(req.selected_stop_count or 50, len(current_locations))
    active_stops = current_locations[:n_stops]
    fleet = current_vehicles[:(req.active_vehicles_count or 4)]

    # Depot + stops coordinates
    all_nodes = [current_depot] + active_stops
    coords = [(node["lat"], node["lon"]) for node in all_nodes]

    # 1. Base Distance & Duration Matrix via OSRM (or road-network topology fallback)
    base_distances, base_durations = await routing_service.get_distance_and_duration_matrix(coords)

    # 2. Enrich with Real-Time Traffic & Incident Delays
    is_traffic = req.traffic_enabled if req.traffic_enabled is not None else True
    traffic_data = traffic_engine.apply_traffic_to_matrices(
        coords,
        base_distances,
        base_durations,
        current_incidents,
        traffic_enabled=is_traffic
    )
    traffic_durations = traffic_data["traffic_duration_matrix"]
    traffic_fuel_matrix = traffic_data["traffic_fuel_matrix"]

    # 3. Solve CVRP with Google OR-Tools (Guided Local Search)
    solution = vrp_solver.solve_cvrp(
        depot_index=0,
        locations=all_nodes,
        vehicles=fleet,
        distance_matrix=base_distances,
        duration_matrix=traffic_durations,
        fuel_matrix=traffic_fuel_matrix,
        time_limit_sec=req.time_limit_sec or 10,
        objective=req.objective or "fuel",
        optimize_fleet_size=(req.fleet_mode == "auto_minimize_vehicles")
    )

    # 4. Attach full road geometry to each vehicle route
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
        distance_matrix=base_distances,
        duration_matrix=traffic_durations
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
    vehicles_saved = max(0, len(fleet) - opt_m["vehicles_used"])

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
            "cost_percent": cost_pct,
            "vehicles_saved": vehicles_saved,
            "vehicles_dispatched": opt_m["vehicles_used"],
            "fleet_available": len(fleet)
        }
    }

    global last_solution
    last_solution = {
        "solution": solution,
        "comparison": comparison,
        "fleet": fleet,
        "stops_count": len(active_stops),
        "depot": current_depot
    }

    return {
        "success": True,
        "solution": solution,
        "comparison": comparison,
        "fleet": fleet,
        "traffic": traffic_data["summary"],
        "traffic_overlay": traffic_data["overlay_segments"],
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
        "lat": inc.lat or current_depot["lat"],
        "lon": inc.lon or current_depot["lon"],
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

@app.post("/api/v1/osm/extract")
async def extract_osm_points(req: OsmExtractRequest):
    """
    Extracts real waste disposal, recycling, or market collection points
    for any global city using 100% free OpenStreetMap Overpass API (no API keys required).
    """
    global current_locations, current_depot
    center_lat = req.lat
    center_lon = req.lon
    city_name = req.city or "Pune"

    if center_lat is None or center_lon is None:
        geocoded = await osm_extractor.geocode_city(city_name)
        if geocoded:
            center_lat = geocoded["lat"]
            center_lon = geocoded["lon"]
            city_name = geocoded["name"].split(",")[0]
        else:
            center_lat = 18.5284
            center_lon = 73.8480

    points = await osm_extractor.fetch_city_collection_points(
        lat=center_lat,
        lon=center_lon,
        radius_meters=req.radius_meters or 5000,
        limit=req.limit or 60
    )

    if not points:
        raise HTTPException(status_code=502, detail="Could not retrieve points from OpenStreetMap Overpass.")

    current_locations = points
    current_depot = {
        "id": 0,
        "name": f"{city_name} Municipal Central Depot",
        "lat": round(center_lat, 6),
        "lon": round(center_lon, 6),
        "address": f"Central Solid Waste Operations Center, {city_name}"
    }

    return {
        "success": True,
        "city": city_name,
        "depot": current_depot,
        "total_stops": len(current_locations),
        "locations": current_locations
    }

@app.post("/api/v1/upload-csv")
async def upload_csv_dataset(file: UploadFile = File(...)):
    """
    Uploads custom municipal collection stops CSV (name, lat, lon, demand_kg, etc.).
    """
    global current_locations
    contents = await file.read()
    decoded = contents.decode("utf-8")
    reader = csv.DictReader(io.StringIO(decoded))
    
    new_locations = []
    idx = 1
    for row in reader:
        try:
            new_locations.append({
                "id": idx,
                "name": row.get("name", f"Collection Point #{idx}"),
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "demand_kg": float(row.get("demand_kg", 60.0)),
                "service_time_sec": int(row.get("service_time_sec", 90)),
                "priority": int(row.get("priority", 1)),
                "required": row.get("required", "true").lower() == "true",
                "status": "pending"
            })
            idx += 1
        except Exception as e:
            logger.warning(f"Skipping malformed row {row}: {e}")
            continue

    if not new_locations:
        raise HTTPException(status_code=400, detail="No valid collection points found in CSV.")

    current_locations = new_locations
    return {"success": True, "total_imported": len(current_locations)}

@app.get("/api/v1/export/geojson")
async def export_geojson():
    """
    Exports latest optimized multi-vehicle routes & collection stops as standard GeoJSON
    for direct integration into QGIS, ArcGIS, or Leaflet.
    """
    if not last_solution or not last_solution.get("solution"):
        raise HTTPException(status_code=400, detail="No optimization solution available. Run optimization first.")

    features = []
    sol = last_solution["solution"]

    # 1. Routes (LineStrings)
    for r in sol.get("routes", []):
        if r.get("geometry"):
            coords = [[pt[1], pt[0]] for pt in r["geometry"]]  # GeoJSON expects [lon, lat]
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": coords
                },
                "properties": {
                    "vehicle_id": r["vehicle_id"],
                    "vehicle_name": r["vehicle_name"],
                    "total_distance_km": round(r["total_distance_m"] / 1000.0, 2),
                    "total_duration_min": round(r["total_duration_sec"] / 60.0, 1),
                    "total_demand_kg": r["total_demand_kg"],
                    "stops_count": len(r["stops"])
                }
            })

    # 2. Stops (Points)
    for r in sol.get("routes", []):
        for s_idx, s in enumerate(r.get("stops", [])):
            if s.get("lat") and s.get("lon"):
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [s["lon"], s["lat"]]
                    },
                    "properties": {
                        "vehicle_name": r["vehicle_name"],
                        "stop_sequence": s_idx,
                        "location_name": s.get("location_name"),
                        "collected_kg": s.get("collected_kg", 0.0),
                        "arrival_sec": s.get("arrival_sec", 0),
                        "is_depot": s.get("is_depot", False)
                    }
                })

    geojson_data = {
        "type": "FeatureCollection",
        "metadata": {
            "platform": "EcoRoute AI",
            "algorithm": sol.get("algorithm"),
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")
        },
        "features": features
    }

    return Response(
        content=json.dumps(geojson_data, indent=2),
        media_type="application/geo+json",
        headers={"Content-Disposition": "attachment; filename=ecoroute_optimized_plan.geojson"}
    )

@app.get("/api/v1/export/manifest")
async def export_driver_manifest():
    """
    Exports a printable/downloadable CSV Driver Manifest for municipal truck operators.
    """
    if not last_solution or not last_solution.get("solution"):
        raise HTTPException(status_code=400, detail="No optimization solution available. Run optimization first.")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Vehicle ID", "Vehicle Name", "Sequence Order", "Stop Name",
        "Latitude", "Longitude", "Demand (kg)", "Cumulative Load (kg)",
        "Arrival Time (HH:MM:SS)", "Service Duration (sec)"
    ])

    sol = last_solution["solution"]
    for r in sol.get("routes", []):
        for s_idx, s in enumerate(r.get("stops", [])):
            arr_s = s.get("arrival_sec", 0)
            hrs = arr_s // 3600
            mins = (arr_s % 3600) // 60
            secs = arr_s % 60
            time_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"

            writer.writerow([
                r["vehicle_id"],
                r["vehicle_name"],
                s_idx,
                s.get("location_name", "Stop"),
                s.get("lat"),
                s.get("lon"),
                s.get("collected_kg", 0.0),
                s.get("cumulative_kg", 0.0),
                time_str,
                s.get("departure_sec", 0) - arr_s
            ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=driver_collection_manifest.csv"}
    )

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
