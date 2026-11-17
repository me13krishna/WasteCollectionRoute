import os
import csv
import io
import time
import json
import logging
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from pydantic import BaseModel, Field

from backend.config import settings
from backend.database import init_db, SessionLocal
from backend.models.db_models import (
    OptimizationRun, RoutePlan, RouteStop, CollectionLocation, Vehicle, Incident, RouteMetric, Depot
)
from backend.services.routing_matrix import routing_service
from backend.services.baseline_solver import baseline_solver
from backend.services.vrp_solver import vrp_solver
from backend.services.osm_extractor import osm_extractor
from backend.services.traffic_service import traffic_engine, PUNE_TRAFFIC_HOTSPOTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
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
    description="Software-only AI-Powered Municipal Waste Collection Route Optimization Platform for Pune Smart City",
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

# Global Error Handler for standardized JSON responses
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": str(exc),
                "path": request.url.path
            }
        }
    )

# --- In-memory Working State for Rapid Interactive Operation ---
SAMPLE_CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "sample_dataset.csv")

DEFAULT_DEPOT = {
    "id": 0,
    "name": "PMC Central Solid Waste Transfer Depot",
    "lat": 18.5284,
    "lon": 73.8480,
    "address": "Ghole Road, Shivajinagar, Pune Municipal Corporation (PMC)"
}

DEFAULT_VEHICLES = [
    {"id": 1, "name": "PMC Compactor Truck #1 (Shivajinagar/FC Road)", "capacity_kg": 1500.0, "max_duration_sec": 28800, "status": "available", "color": "#10b981"},
    {"id": 2, "name": "PMC Refuse Truck #2 (Kothrud/Karve Road)", "capacity_kg": 1400.0, "max_duration_sec": 28800, "status": "available", "color": "#06b6d4"},
    {"id": 3, "name": "PMC Compactor Truck #3 (Swargate/Central Peths)", "capacity_kg": 1600.0, "max_duration_sec": 28800, "status": "available", "color": "#8b5cf6"},
    {"id": 4, "name": "PMC Swachh Truck #4 (Hadapsar/Camp Zone)", "capacity_kg": 1300.0, "max_duration_sec": 28800, "status": "available", "color": "#f59e0b"}
]

current_locations: List[Dict[str, Any]] = []
current_depot = dict(DEFAULT_DEPOT)
current_vehicles = [dict(v) for v in DEFAULT_VEHICLES]
current_incidents: List[Dict[str, Any]] = []

# Persistent solutions & diff tracking
initial_reference_solution: Optional[Dict[str, Any]] = None
last_solution: Optional[Dict[str, Any]] = None
last_optimize_request: Optional[Dict[str, Any]] = None
incident_history_stack: List[Dict[str, Any]] = []

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

# --- Schemas ---
class OptimizeRequest(BaseModel):
    selected_stop_count: Optional[int] = Field(default=50, ge=2, le=200)
    objective: Optional[str] = "fuel"  # "fuel", "traffic_duration", "distance"
    time_limit_sec: Optional[int] = Field(default=15, ge=1, le=60)
    active_vehicles_count: Optional[int] = Field(default=4, ge=1, le=10)
    fleet_mode: Optional[str] = "auto_minimize_vehicles"  # "auto_minimize_vehicles", "fixed_fleet"
    traffic_enabled: Optional[bool] = True

class IncidentRequest(BaseModel):
    incident_type: str = Field(..., description="road_closure, traffic_jam, missed_stop, emergency_pickup")
    description: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    stop_id: Optional[int] = None
    auto_reoptimize: Optional[bool] = True

class OsmExtractRequest(BaseModel):
    city: Optional[str] = "Pune"
    lat: Optional[float] = None
    lon: Optional[float] = None
    radius_meters: Optional[int] = 5000
    limit: Optional[int] = 60

# --- Helper Functions ---
def compute_route_diff(base_sol: Optional[Dict[str, Any]], current_sol: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Computes before-and-after route plan diff for reoptimization transparency."""
    if not base_sol or not current_sol:
        return {
            "has_diff": False,
            "distance_delta_km": 0.0,
            "fuel_delta_liters": 0.0,
            "cost_delta": 0.0,
            "vehicles_delta": 0,
            "affected_vehicles": []
        }

    b_m = base_sol["metrics"]
    c_m = current_sol["metrics"]

    dist_delta = round(c_m["total_distance_km"] - b_m["total_distance_km"], 2)
    fuel_delta = round(c_m["est_fuel_liters"] - b_m["est_fuel_liters"], 2)
    cost_delta = round(c_m["est_total_cost"] - b_m["est_total_cost"], 2)
    veh_delta = c_m["vehicles_used"] - b_m["vehicles_used"]

    # Check vehicle route changes
    base_routes_map = {r["vehicle_id"]: [s["node_idx"] for s in r["stops"]] for r in base_sol.get("routes", [])}
    curr_routes_map = {r["vehicle_id"]: [s["node_idx"] for s in r["stops"]] for r in current_sol.get("routes", [])}

    affected_vehicles = []
    all_veh_ids = set(base_routes_map.keys()) | set(curr_routes_map.keys())
    for vid in all_veh_ids:
        if base_routes_map.get(vid) != curr_routes_map.get(vid):
            affected_vehicles.append(vid)

    return {
        "has_diff": True,
        "distance_delta_km": dist_delta,
        "fuel_delta_liters": fuel_delta,
        "cost_delta": cost_delta,
        "vehicles_delta": veh_delta,
        "affected_vehicles": affected_vehicles,
        "explanation": f"Plan updated: {abs(dist_delta):+.2f} km distance, {abs(fuel_delta):+.2f} L fuel, {len(affected_vehicles)} vehicles rerouted."
    }

def persist_run_to_db(solution: Dict[str, Any], fleet: List[Dict[str, Any]], req_data: Dict[str, Any]) -> Optional[int]:
    """Persists optimization run, vehicles, route plans, and metrics into SQLite database."""
    try:
        db = SessionLocal()
        try:
            # 1. Ensure depot exists
            depot_rec = db.query(Depot).first()
            if not depot_rec:
                depot_rec = Depot(
                    name=current_depot["name"],
                    lat=current_depot["lat"],
                    lon=current_depot["lon"],
                    address=current_depot.get("address", "Pune")
                )
                db.add(depot_rec)
                db.flush()

            # 2. Record run
            run_rec = OptimizationRun(
                name=f"Run-{int(time.time())} ({req_data.get('objective', 'fuel')})",
                status="completed",
                objective=req_data.get("objective", "fuel"),
                config_json=req_data,
                runtime_ms=solution.get("runtime_ms", 0),
                solver_algorithm=solution.get("algorithm", "OR-Tools CVRP")
            )
            db.add(run_rec)
            db.flush()

            # 3. Record routes & stops
            for route_data in solution.get("routes", []):
                # Ensure vehicle exists
                v_id = route_data["vehicle_id"]
                veh_rec = db.query(Vehicle).filter(Vehicle.id == v_id).first()
                if not veh_rec:
                    veh_rec = Vehicle(
                        id=v_id,
                        name=route_data.get("vehicle_name", f"Truck #{v_id}"),
                        capacity_kg=route_data.get("vehicle_capacity_kg", 1500.0),
                        status="available"
                    )
                    db.add(veh_rec)
                    db.flush()

                plan_rec = RoutePlan(
                    run_id=run_rec.id,
                    vehicle_id=veh_rec.id,
                    total_distance_m=route_data.get("total_distance_m", 0.0),
                    total_duration_sec=route_data.get("total_duration_sec", 0),
                    total_demand_kg=route_data.get("total_demand_kg", 0.0),
                    geometry_geojson=route_data.get("geometry", []),
                    is_active=True
                )
                db.add(plan_rec)
                db.flush()

                for seq, stop in enumerate(route_data.get("stops", [])):
                    r_stop = RouteStop(
                        plan_id=plan_rec.id,
                        location_id=stop.get("location_id"),
                        is_depot=stop.get("is_depot", False),
                        sequence_order=seq,
                        arrival_time_est_sec=stop.get("arrival_sec", 0),
                        departure_time_est_sec=stop.get("departure_sec", 0),
                        demand_collected_kg=stop.get("collected_kg", 0.0),
                        cumulative_load_kg=stop.get("cumulative_kg", 0.0),
                        status="planned"
                    )
                    db.add(r_stop)

            # 4. Record metrics
            m = solution["metrics"]
            metric_rec = RouteMetric(
                run_id=run_rec.id,
                total_distance_km=m.get("total_distance_km", 0.0),
                total_travel_time_sec=m.get("total_travel_time_sec", 0),
                total_service_time_sec=m.get("total_service_time_sec", 0),
                est_fuel_liters=m.get("est_fuel_liters", 0.0),
                est_co2_kg=m.get("est_co2_kg", 0.0),
                est_total_cost=m.get("est_total_cost", 0.0),
                vehicles_used=m.get("vehicles_used", 0),
                stops_served=m.get("stops_served", 0),
                unserved_count=m.get("unserved_count", 0),
                workload_balance_std_km=m.get("workload_balance_std_km", 0.0)
            )
            db.add(metric_rec)
            db.commit()
            return run_rec.id
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Could not persist run to database: {e}")
        return None

# --- Core Optimization Execution Engine ---
async def execute_optimization(req: OptimizeRequest) -> Dict[str, Any]:
    global last_solution, initial_reference_solution, last_optimize_request
    last_optimize_request = req.model_dump()

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
    traffic_distances = traffic_data["traffic_distance_matrix"]
    traffic_durations = traffic_data["traffic_duration_matrix"]
    traffic_fuel_matrix = traffic_data["traffic_fuel_matrix"]

    # 3. Solve CVRP with Google OR-Tools (Guided Local Search)
    time_limit = req.time_limit_sec or settings.DEFAULT_OPT_TIME_LIMIT_SEC
    solution = vrp_solver.solve_cvrp(
        depot_index=0,
        locations=all_nodes,
        vehicles=fleet,
        distance_matrix=traffic_distances if any(inc.get("active") and inc.get("incident_type") == "road_closure" for inc in current_incidents) else base_distances,
        duration_matrix=traffic_durations,
        fuel_matrix=traffic_fuel_matrix,
        time_limit_sec=time_limit,
        objective=req.objective or "fuel",
        optimize_fleet_size=(req.fleet_mode == "auto_minimize_vehicles")
    )

    # 4. Attach full road geometry to each vehicle route
    for route in solution.get("routes", []):
        pts = [(s["lat"], s["lon"]) for s in route["stops"]]
        geometry = await routing_service.get_route_geometry(pts)
        route["geometry"] = geometry
        veh_match = next((v for v in fleet if v["id"] == route["vehicle_id"]), None)
        route["color"] = veh_match["color"] if veh_match else "#10b981"

    # 5. Baseline Comparison
    base_sol = baseline_solver.solve(
        depot_index=0,
        locations=all_nodes,
        vehicles=fleet,
        distance_matrix=base_distances,
        duration_matrix=traffic_durations
    )

    # Attach geometry to baseline routes too
    for b_route in base_sol.get("routes", []):
        pts = [(s["lat"], s["lon"]) for s in b_route["stops"]]
        b_route["geometry"] = await routing_service.get_route_geometry(pts)

    # Compute comparison savings deltas
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
        "baseline_routes": base_sol.get("routes", []),
        "savings": {
            "distance_km": dist_diff,
            "distance_percent": dist_pct,
            "fuel_liters": fuel_diff,
            "fuel_percent": fuel_pct,
            "co2_kg": co2_diff,
            "co2_percent": co2_pct,
            "cost_currency": cost_diff,
            "cost_percent": cost_pct,
            "currency_symbol": settings.CURRENCY_SYMBOL,
            "currency_code": settings.CURRENCY_CODE,
            "vehicles_saved": vehicles_saved,
            "vehicles_dispatched": opt_m["vehicles_used"],
            "fleet_available": len(fleet)
        }
    }

    # Track initial reference solution on first run
    if initial_reference_solution is None:
        initial_reference_solution = {
            "solution": solution,
            "comparison": comparison,
            "metrics": opt_m
        }

    diff = compute_route_diff(
        initial_reference_solution.get("solution") if initial_reference_solution else None,
        solution
    )

    last_solution = {
        "solution": solution,
        "comparison": comparison,
        "fleet": fleet,
        "stops_count": len(active_stops),
        "depot": current_depot
    }

    # Persist in SQLite
    run_db_id = persist_run_to_db(solution, fleet, req.model_dump())

    return {
        "success": True,
        "run_id": run_db_id,
        "solution": solution,
        "comparison": comparison,
        "fleet": fleet,
        "diff_from_initial": diff,
        "traffic": traffic_data["summary"],
        "traffic_overlay": traffic_data["overlay_segments"],
        "routing_status": routing_service.status_summary,
        "stops_count": len(active_stops),
        "units": {
            "currency": settings.CURRENCY_SYMBOL,
            "distance": settings.DISTANCE_UNIT,
            "volume": settings.VOLUME_UNIT,
            "mass": settings.MASS_UNIT
        }
    }

# --- API Endpoints ---

@app.get("/api/v1/config")
async def get_system_config():
    """Returns platform configuration, constants, SI units, and Pune pricing models."""
    return {
        "project_name": settings.PROJECT_NAME,
        "city": "Pune, Maharashtra, India",
        "currency_symbol": settings.CURRENCY_SYMBOL,
        "currency_code": settings.CURRENCY_CODE,
        "distance_unit": settings.DISTANCE_UNIT,
        "volume_unit": settings.VOLUME_UNIT,
        "mass_unit": settings.MASS_UNIT,
        "constants": {
            "fuel_l_per_km": settings.FUEL_L_PER_KM,
            "co2_kg_per_l": settings.CO2_KG_PER_L,
            "fuel_cost_per_l": settings.FUEL_COST_PER_L,
            "driver_hourly_rate": settings.DRIVER_HOURLY_RATE,
            "truck_idle_fuel_l_per_hr": settings.TRUCK_IDLE_FUEL_L_PER_HR,
            "compactor_pto_l_per_stop": settings.COMPACTOR_PTO_L_PER_STOP,
            "circuity_factor": settings.CIRCUITY_FACTOR,
            "truck_avg_speed_kmh": settings.TRUCK_AVG_SPEED_KMH
        },
        "routing": routing_service.status_summary
    }

@app.get("/api/v1/routing/status")
async def get_routing_status():
    """Returns status of OSRM routing engine (Live vs Fallback Mode)."""
    return routing_service.status_summary

@app.get("/api/v1/dataset")
async def get_dataset():
    """Returns current working collection stops, depot, vehicles, and active incidents."""
    return {
        "depot": current_depot,
        "vehicles": current_vehicles,
        "locations": current_locations,
        "incidents": current_incidents,
        "total_stops": len(current_locations),
        "active_incidents_count": len([i for i in current_incidents if i.get("active", True)])
    }

@app.get("/api/v1/traffic/status")
async def get_traffic_status():
    """Returns real-time urban traffic congestion conditions and active bottleneck hotspots."""
    base_factor, period_name, period_desc = traffic_engine.get_time_of_day_congestion_factor()
    return {
        "success": True,
        "current_time": time.strftime("%H:%M:%S"),
        "period_name": period_name,
        "period_desc": period_desc,
        "base_congestion_factor": base_factor,
        "hotspots": PUNE_TRAFFIC_HOTSPOTS,
        "active_incidents_count": len([i for i in current_incidents if i.get("active", True)])
    }

@app.post("/api/v1/optimize")
async def run_optimization(req: OptimizeRequest):
    """
    Runs Google OR-Tools CVRP solver with Guided Local Search.
    Enriched with real-time dynamic traffic congestion, fleet size minimization,
    and load-dependent fuel & emissions optimization.
    """
    return await execute_optimization(req)

@app.post("/api/v1/incidents")
async def report_incident(inc: IncidentRequest):
    """
    Registers an operational event (road closure, traffic jam, missed stop, emergency pickup)
    and executes dynamic software-driven reoptimization.
    """
    global current_locations, current_incidents, incident_history_stack

    inc_id = len(current_incidents) + 1
    incident_obj = {
        "id": inc_id,
        "incident_type": inc.incident_type,
        "description": inc.description,
        "lat": inc.lat or (current_depot["lat"] + 0.008),
        "lon": inc.lon or (current_depot["lon"] + 0.008),
        "stop_id": inc.stop_id,
        "active": True,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    inserted_stop_id = None
    prior_stop_status = None

    # 1. Missed stop handling
    if inc.incident_type == "missed_stop" and inc.stop_id:
        for s in current_locations:
            if s["id"] == inc.stop_id:
                prior_stop_status = s.get("status", "pending")
                s["status"] = "missed"
                s["priority"] = 4  # Boost priority for re-visit

    # 2. Urgent pickup handling
    elif inc.incident_type in ["urgent_pickup", "emergency_pickup"]:
        new_id = max(s["id"] for s in current_locations) + 1 if current_locations else 1
        inserted_stop_id = new_id
        new_stop = {
            "id": new_id,
            "name": f"URGENT: {inc.description}",
            "lat": inc.lat or (current_depot["lat"] + 0.012),
            "lon": inc.lon or (current_depot["lon"] + 0.015),
            "demand_kg": 150.0,
            "service_time_sec": 180,
            "priority": 4,  # Highest urgency
            "required": True,
            "status": "pending"
        }
        current_locations.insert(0, new_stop)

    current_incidents.append(incident_obj)

    # Save to undo stack
    incident_history_stack.append({
        "incident": incident_obj,
        "inserted_stop_id": inserted_stop_id,
        "modified_stop_id": inc.stop_id,
        "prior_stop_status": prior_stop_status
    })

    # Auto-reoptimize if requested
    reopt_result = None
    if inc.auto_reoptimize:
        req = OptimizeRequest(**(last_optimize_request or {}))
        reopt_result = await execute_optimization(req)

    return {
        "success": True,
        "incident": incident_obj,
        "total_incidents": len(current_incidents),
        "reoptimized": reopt_result is not None,
        "reopt_data": reopt_result
    }

@app.post("/api/v1/incidents/undo")
async def undo_last_incident():
    """
    Reverts the last registered operational incident and restores previous plan.
    """
    global current_locations, current_incidents, incident_history_stack

    if not incident_history_stack:
        raise HTTPException(status_code=400, detail="No incidents to undo.")

    last_action = incident_history_stack.pop()
    inc = last_action["incident"]

    # Remove from active incidents
    current_incidents = [i for i in current_incidents if i["id"] != inc["id"]]

    # Revert stop changes
    if last_action["inserted_stop_id"]:
        current_locations = [s for s in current_locations if s["id"] != last_action["inserted_stop_id"]]

    if last_action["modified_stop_id"] and last_action["prior_stop_status"]:
        for s in current_locations:
            if s["id"] == last_action["modified_stop_id"]:
                s["status"] = last_action["prior_stop_status"]

    # Re-run optimization with restored state
    req = OptimizeRequest(**(last_optimize_request or {}))
    reopt_result = await execute_optimization(req)

    return {
        "success": True,
        "undone_incident": inc,
        "remaining_incidents": len(current_incidents),
        "reopt_data": reopt_result
    }

@app.post("/api/v1/incidents/reset")
async def reset_incidents():
    """
    Clears all reported incidents, resets dataset to clean state,
    and returns to the original pristine plan.
    """
    global current_incidents, incident_history_stack, initial_reference_solution
    current_incidents = []
    incident_history_stack = []
    load_initial_dataset()
    initial_reference_solution = None

    req = OptimizeRequest(**(last_optimize_request or {}))
    reopt_result = await execute_optimization(req)

    return {
        "success": True,
        "message": "All incidents cleared and plan reset to initial baseline.",
        "reopt_data": reopt_result
    }

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
        "name": f"{city_name} Municipal Central Solid Waste Depot",
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
    """Uploads custom municipal collection stops CSV (name, lat, lon, demand_kg, etc.)."""
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

@app.get("/api/v1/export/template")
async def download_csv_template():
    """Provides a downloadable CSV template with Pune sample points for user uploads."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "lat", "lon", "demand_kg", "service_time_sec", "priority", "required"])
    writer.writerow(["Fergusson College Main Gate Bin", "18.5204", "73.8425", "65.0", "90", "2", "true"])
    writer.writerow(["Deccan Gymkhana Bus Stop Point", "18.5175", "73.8415", "85.0", "120", "3", "true"])
    writer.writerow(["Laxmi Road Wholesale Market", "18.5165", "73.8530", "140.0", "150", "4", "true"])
    writer.writerow(["Swargate Chowk Transit Point", "18.4990", "73.8570", "110.0", "120", "3", "true"])
    
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ecoroute_collection_stops_template.csv"}
    )

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
            coords = [[pt[0], pt[1]] for pt in r["geometry"]]  # GeoJSON expects [lon, lat]
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
            "platform": "EcoRoute AI Pune Smart City",
            "algorithm": sol.get("algorithm"),
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")
        },
        "features": features
    }

    return Response(
        content=json.dumps(geojson_data, indent=2),
        media_type="application/geo+json",
        headers={"Content-Disposition": "attachment; filename=ecoroute_pune_optimized_plan.geojson"}
    )

@app.get("/api/v1/export/manifest")
async def export_driver_manifest():
    """Exports a printable/downloadable CSV Driver Manifest for municipal truck operators."""
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
        headers={"Content-Disposition": "attachment; filename=driver_collection_manifest_pune.csv"}
    )

@app.get("/api/v1/export/report-html")
async def export_printable_report():
    """Generates a professional, printable HTML shift report per driver for paperless dispatch."""
    if not last_solution or not last_solution.get("solution"):
        raise HTTPException(status_code=400, detail="No optimization solution available. Run optimization first.")

    sol = last_solution["solution"]
    comp = last_solution.get("comparison", {})
    savings = comp.get("savings", {})
    routes = sol.get("routes", [])

    cards_html = ""
    for r in routes:
        stops_rows = ""
        for idx, s in enumerate(r.get("stops", [])):
            arr_s = s.get("arrival_sec", 0)
            hrs = arr_s // 3600
            mins = (arr_s % 3600) // 60
            time_str = f"{hrs:02d}:{mins:02d}"
            stops_rows += f"""
            <tr>
                <td style="padding:6px; border-bottom:1px solid #e5e7eb;">#{idx}</td>
                <td style="padding:6px; border-bottom:1px solid #e5e7eb;"><strong>{s.get('location_name', 'Stop')}</strong></td>
                <td style="padding:6px; border-bottom:1px solid #e5e7eb;">{time_str}</td>
                <td style="padding:6px; border-bottom:1px solid #e5e7eb;">{s.get('collected_kg', 0.0)} kg</td>
                <td style="padding:6px; border-bottom:1px solid #e5e7eb;">{s.get('cumulative_kg', 0.0)} kg</td>
            </tr>
            """

        cards_html += f"""
        <div style="page-break-after:always; margin-bottom:30px; border:1px solid #d1d5db; border-radius:8px; padding:20px; font-family:sans-serif;">
            <div style="display:flex; justify-content:space-between; border-bottom:2px solid #10b981; padding-bottom:10px;">
                <div>
                    <h2 style="margin:0; color:#111827;">{r.get('vehicle_name')}</h2>
                    <p style="margin:4px 0 0; color:#6b7280; font-size:14px;">EcoRoute AI Driver Dispatch Manifest • Pune Municipal Corporation (PMC)</p>
                </div>
                <div style="text-align:right;">
                    <span style="background:#d1fae5; color:#065f46; padding:4px 10px; border-radius:12px; font-weight:bold; font-size:12px;">ACTIVE ROUTE</span>
                </div>
            </div>
            <div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; margin:16px 0; background:#f9fafb; padding:12px; border-radius:6px;">
                <div><div style="font-size:12px; color:#6b7280;">Total Distance</div><div style="font-size:16px; font-weight:bold;">{round(r['total_distance_m']/1000.0, 2)} km</div></div>
                <div><div style="font-size:12px; color:#6b7280;">Est. Duration</div><div style="font-size:16px; font-weight:bold;">{round(r['total_duration_sec']/60.0, 1)} min</div></div>
                <div><div style="font-size:12px; color:#6b7280;">Payload Collected</div><div style="font-size:16px; font-weight:bold;">{r['total_demand_kg']} / {r.get('vehicle_capacity_kg')} kg</div></div>
                <div><div style="font-size:12px; color:#6b7280;">Stops Count</div><div style="font-size:16px; font-weight:bold;">{len(r['stops'])} stops</div></div>
            </div>
            <table style="width:100%; border-collapse:collapse; font-size:13px; text-align:left;">
                <thead>
                    <tr style="background:#f3f4f6; color:#374151;">
                        <th style="padding:8px;">Seq</th>
                        <th style="padding:8px;">Collection Point</th>
                        <th style="padding:8px;">ETA</th>
                        <th style="padding:8px;">Demand</th>
                        <th style="padding:8px;">Truck Load</th>
                    </tr>
                </thead>
                <tbody>
                    {stops_rows}
                </tbody>
            </table>
            <div style="margin-top:20px; display:flex; justify-content:space-between; font-size:12px; color:#6b7280;">
                <div>Driver Signature: _________________________</div>
                <div>PMC Supervisor Signature: _________________________</div>
            </div>
        </div>
        """

    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>EcoRoute AI - Driver Shift Report</title>
        <style>
            @media print {{
                .no-print {{ display: none; }}
                body {{ padding: 0; }}
            }}
            body {{ font-family: Inter, system-ui, -apple-system, sans-serif; background: #fff; color: #111; padding: 20px; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="margin-bottom:20px; display:flex; gap:10px;">
            <button onclick="window.print()" style="background:#10b981; color:#fff; border:none; padding:10px 18px; border-radius:6px; cursor:pointer; font-weight:bold;">🖨️ Print Driver Reports</button>
            <button onclick="window.close()" style="background:#6b7280; color:#fff; border:none; padding:10px 18px; border-radius:6px; cursor:pointer;">Close</button>
        </div>
        {cards_html}
    </body>
    </html>
    """
    return HTMLResponse(content=full_html)

@app.post("/api/v1/reset-dataset")
async def reset_data():
    """Resets dataset to default 80 PMC collection stops."""
    global current_incidents, incident_history_stack, initial_reference_solution
    current_incidents = []
    incident_history_stack = []
    initial_reference_solution = None
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
