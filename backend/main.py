import os
import csv
import io
import math
import time
import json
import random
import asyncio
import logging
from typing import List, Dict, Any, Optional, Tuple
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response, StreamingResponse
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
from backend.services.fleet_model import demo_profile, demo_curve, effective_cost, UNLOAD_MIN
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
    {"driver": "Ramesh Patil", "id": 1, "name": "PMC Compactor Truck #1 (Shivajinagar/FC Road)", "capacity_kg": 1500.0, "max_duration_sec": 28800, "status": "available", "color": "#10b981"},
    {"driver": "Sunita Jadhav", "id": 2, "name": "PMC Refuse Truck #2 (Kothrud/Karve Road)", "capacity_kg": 1400.0, "max_duration_sec": 28800, "status": "available", "color": "#06b6d4"},
    {"driver": "Anil Shinde", "id": 3, "name": "PMC Compactor Truck #3 (Swargate/Central Peths)", "capacity_kg": 1600.0, "max_duration_sec": 28800, "status": "available", "color": "#8b5cf6"},
    {"driver": "Kavita More", "id": 4, "name": "PMC Swachh Truck #4 (Hadapsar/Camp Zone)", "capacity_kg": 1300.0, "max_duration_sec": 28800, "status": "available", "color": "#f59e0b"},
    {"driver": "Prakash Kale", "id": 5, "name": "PMC Compactor Truck #5 (Aundh/Baner)", "capacity_kg": 1500.0, "max_duration_sec": 28800, "status": "available", "color": "#ef4444"},
    {"driver": "Meena Gaikwad", "id": 6, "name": "PMC Refuse Truck #6 (Kasba/Narayan Peth)", "capacity_kg": 1400.0, "max_duration_sec": 28800, "status": "available", "color": "#ec4899"},
    {"driver": "Sanjay Deshmukh", "id": 7, "name": "PMC Compactor Truck #7 (Yerwada/Vishrantwadi)", "capacity_kg": 1500.0, "max_duration_sec": 28800, "status": "available", "color": "#3b82f6"},
    {"driver": "Pooja Kulkarni", "id": 8, "name": "PMC Swachh Truck #8 (Sinhagad Road/Sahakarnagar)", "capacity_kg": 1300.0, "max_duration_sec": 28800, "status": "available", "color": "#84cc16"}
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
    global current_locations, current_depot
    current_locations = []
    # Restore the Pune depot too, in case a city import or CSV replaced it.
    current_depot = dict(DEFAULT_DEPOT)
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
                    "required": row.get("required", "true").lower() == "true",
                    "status": "pending"
                })
    logger.info(f"Loaded {len(current_locations)} collection points from sample dataset.")

load_initial_dataset()

# --- Schemas ---
class OptimizeRequest(BaseModel):
    selected_stop_count: Optional[int] = Field(default=None, ge=2, le=200)  # None = every loaded point
    objective: Optional[str] = "fuel"  # "fuel", "traffic_duration", "distance"
    time_limit_sec: Optional[int] = Field(default=15, ge=1, le=60)
    active_vehicles_count: Optional[int] = Field(default=4, ge=1, le=10)
    # "auto_minimize_vehicles": fewest trucks needed | "fixed_fleet": up to N, solver decides
    # "exact_fleet": dispatch exactly N trucks, each with at least one stop
    fleet_mode: Optional[str] = "auto_minimize_vehicles"
    traffic_enabled: Optional[bool] = True

class RandomizeRequest(BaseModel):
    count: int = Field(default=40, ge=5, le=200)
    radius_km: float = Field(default=4.0, ge=0.5, le=5.0)
    seed: Optional[int] = None
    scenario: Optional[int] = Field(default=None, ge=1, le=10)  # 1-10: a preset scenario (overrides count/radius/seed)

class FleetSweepRequest(BaseModel):
    selected_stop_count: Optional[int] = Field(default=None, ge=2, le=200)
    min_trucks: int = Field(default=1, ge=1, le=10)
    max_trucks: int = Field(default=8, ge=1, le=10)
    time_limit_sec: int = Field(default=4, ge=1, le=20)
    traffic_enabled: Optional[bool] = True

class FleetStreamRequest(BaseModel):
    selected_stop_count: Optional[int] = Field(default=None, ge=2, le=200)
    max_trucks: int = Field(default=8, ge=2, le=10)
    time_limit_sec: int = Field(default=3, ge=1, le=20)
    traffic_enabled: Optional[bool] = True
    stop_at_optimal: bool = True  # stop one truck after the minimum (the "optimal + 1" view)

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
        "explanation": f"Compared with the first plan: {dist_delta:+.2f} km, {fuel_delta:+.2f} L diesel, {len(affected_vehicles)} truck(s) rerouted."
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
                        capacity_kg=route_data.get("vehicle_capacity_kg") or 1500.0,
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

# --- Truck capacity is not a constraint ---
# Trucks may collect any amount. The solver gets a huge capacity; the real one is kept only as the
# reference for the load-dependent fuel model.
UNLIMITED_KG = 1_000_000.0

def unlimited_fleet(fleet: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{**v, "fuel_ref_capacity_kg": v["capacity_kg"], "capacity_kg": UNLIMITED_KG} for v in fleet]

def strip_capacity(solution: Dict[str, Any]) -> Dict[str, Any]:
    for route in solution.get("routes", []):
        route.pop("vehicle_capacity_kg", None)
    return solution

# --- Random collection points ---
ROAD_NODES_PATH = os.path.join(os.path.dirname(__file__), "data", "road_nodes.json")
_road_pool: Optional[Dict[str, Any]] = None

def load_road_pool() -> Dict[str, Any]:
    """Real road points (OpenStreetMap segment midpoints) around central Pune."""
    global _road_pool
    if _road_pool is None:
        try:
            with open(ROAD_NODES_PATH, "r", encoding="utf-8") as f:
                _road_pool = json.load(f)
        except Exception as e:
            logger.warning(f"Road point pool unavailable ({e}); random points will not be snapped to roads.")
            _road_pool = {"center": None, "nodes": []}
    return _road_pool

# Ten ready-made scenarios. Offsets are km east/north of the depot; points are drawn from real roads.
SCENARIOS = [
    {"id": 1, "name": "Old city (Peths)", "count": 30, "seed": 101, "dx": 0.3, "dy": -0.9, "r": 1.3},
    {"id": 2, "name": "Shivajinagar and FC Road", "count": 25, "seed": 102, "dx": -0.9, "dy": 0.3, "r": 1.4},
    {"id": 3, "name": "Kothrud and Karve Road", "count": 35, "seed": 103, "dx": -3.0, "dy": -0.5, "r": 2.0},
    {"id": 4, "name": "Swargate and Market Yard", "count": 30, "seed": 104, "dx": 0.8, "dy": -3.0, "r": 2.0},
    {"id": 5, "name": "Camp and Koregaon Park", "count": 35, "seed": 105, "dx": 2.5, "dy": -0.5, "r": 2.0},
    {"id": 6, "name": "Yerwada and Kalyani Nagar", "count": 40, "seed": 106, "dx": 3.0, "dy": 2.5, "r": 2.2},
    {"id": 7, "name": "Aundh and Baner", "count": 40, "seed": 107, "dx": -3.0, "dy": 3.2, "r": 2.2},
    {"id": 8, "name": "Hadapsar side", "count": 45, "seed": 108, "dx": 4.0, "dy": -2.0, "r": 2.0},
    {"id": 9, "name": "Whole city, light day", "count": 20, "seed": 109, "dx": 0.0, "dy": 0.0, "r": 5.0},
    {"id": 10, "name": "Whole city, heavy day", "count": 80, "seed": 110, "dx": 0.0, "dy": 0.0, "r": 5.0},
]

def generate_random_points(
    count: int,
    radius_km: float,
    seed: Optional[int] = None,
    dx_km: float = 0.0,
    dy_km: float = 0.0,
    label: str = "Random Point"
) -> List[Dict[str, Any]]:
    """
    Picks `count` collection points at random. When the depot is in central Pune the points are
    taken from real road segments, so every point lies exactly on a road a truck can reach.
    Elsewhere (e.g. an imported city) they fall back to a plain random scatter around the depot.
    Every call without a seed produces a different layout.
    """
    rnd = random.Random(seed)
    lat0, lon0 = current_depot["lat"], current_depot["lon"]
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    clat, clon = lat0 + dy_km * 1000.0 / m_per_deg_lat, lon0 + dx_km * 1000.0 / m_per_deg_lon

    pool = load_road_pool()
    coords: List[Tuple[float, float]] = []
    pc = pool.get("center")
    if pool["nodes"] and pc and abs(pc[0] - lat0) < 0.01 and abs(pc[1] - lon0) < 0.01:
        r2 = (radius_km * 1000.0) ** 2
        near = [
            (la, lo) for la, lo in pool["nodes"]
            if ((la - clat) * m_per_deg_lat) ** 2 + ((lo - clon) * m_per_deg_lon) ** 2 <= r2
        ]
        coords = [tuple(c) for c in rnd.sample(near, min(count, len(near)))]
    if not coords:
        for _ in range(count):
            rr = radius_km * 1000.0 * math.sqrt(rnd.random())  # sqrt => uniform over the area
            theta = rnd.random() * 2.0 * math.pi
            coords.append((round(clat + (rr * math.sin(theta)) / m_per_deg_lat, 6),
                           round(clon + (rr * math.cos(theta)) / m_per_deg_lon, 6)))

    points = []
    for i, (la, lo) in enumerate(coords, start=1):
        points.append({
            "id": i,
            "name": f"{label} #{i}",
            "lat": la,
            "lon": lo,
            "demand_kg": float(rnd.choices([40, 60, 80, 100, 120, 150, 200], weights=[15, 22, 22, 18, 12, 8, 3])[0]),
            "service_time_sec": rnd.choice([60, 90, 120, 150]),
            "required": True,
            "status": "pending"
        })
    return points

async def prepare_problem(stop_count: Optional[int], traffic_enabled: Optional[bool]) -> Dict[str, Any]:
    """Builds the node list and the road / traffic matrices shared by every solve on the current points."""
    n_stops = min(stop_count, len(current_locations)) if stop_count else len(current_locations)
    active_stops = current_locations[:n_stops]
    all_nodes = [current_depot] + active_stops
    coords = [(node["lat"], node["lon"]) for node in all_nodes]

    base_distances, base_durations = await routing_service.get_distance_and_duration_matrix(coords)
    traffic_data = traffic_engine.apply_traffic_to_matrices(
        coords,
        base_distances,
        base_durations,
        current_incidents,
        traffic_enabled=traffic_enabled if traffic_enabled is not None else True
    )
    has_closure = any(inc.get("active") and inc.get("incident_type") == "road_closure" for inc in current_incidents)
    return {
        "active_stops": active_stops,
        "all_nodes": all_nodes,
        "base_distances": base_distances,
        "traffic_data": traffic_data,
        "solver_distances": traffic_data["traffic_distance_matrix"] if has_closure else base_distances,
    }

# --- Core Optimization Execution Engine ---
async def execute_optimization(req: OptimizeRequest) -> Dict[str, Any]:
    global last_solution, initial_reference_solution, last_optimize_request
    last_optimize_request = req.model_dump()

    fleet = current_vehicles[:(req.active_vehicles_count or 4)]
    solver_fleet = unlimited_fleet(fleet)

    # 1-2. Road matrices (OSRM or circuity fallback) enriched with traffic & incident delays
    problem = await prepare_problem(req.selected_stop_count, req.traffic_enabled)
    active_stops = problem["active_stops"]
    all_nodes = problem["all_nodes"]
    base_distances = problem["base_distances"]
    traffic_data = problem["traffic_data"]
    traffic_durations = traffic_data["traffic_duration_matrix"]
    traffic_fuel_matrix = traffic_data["traffic_fuel_matrix"]

    # 3. Solve CVRP with Google OR-Tools (Guided Local Search)
    time_limit = req.time_limit_sec or settings.DEFAULT_OPT_TIME_LIMIT_SEC
    solution = vrp_solver.solve_cvrp(
        depot_index=0,
        locations=all_nodes,
        vehicles=solver_fleet,
        distance_matrix=problem["solver_distances"],
        duration_matrix=traffic_durations,
        fuel_matrix=traffic_fuel_matrix,
        time_limit_sec=time_limit,
        objective=req.objective or "fuel",
        optimize_fleet_size=(req.fleet_mode == "auto_minimize_vehicles"),
        force_all_vehicles=(req.fleet_mode == "exact_fleet")
    )

    strip_capacity(solution)

    # 4. Attach full road geometry to each vehicle route
    for route in solution.get("routes", []):
        pts = [(s["lat"], s["lon"]) for s in route["stops"]]
        geometry = await routing_service.get_route_geometry(pts)
        route["geometry"] = geometry
        veh_match = next((v for v in fleet if v["id"] == route["vehicle_id"]), None)
        route["color"] = veh_match["color"] if veh_match else "#10b981"
        route["driver"] = veh_match.get("driver", "") if veh_match else ""

    # 5. Baseline Comparison
    base_sol = baseline_solver.solve(
        depot_index=0,
        locations=all_nodes,
        vehicles=solver_fleet,
        distance_matrix=base_distances,
        duration_matrix=traffic_durations
    )

    strip_capacity(base_sol)

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

# Layout fingerprint -> scenario id, so each ready-made scenario keeps its own demo curve.
SCENARIO_BY_LAYOUT: Dict[str, int] = {}

@app.post("/api/v1/dataset/randomize")
async def randomize_dataset(req: RandomizeRequest):
    """
    Replaces the working collection points with a fresh random layout around the depot.
    Incidents and the previous plan are cleared because they referred to the old points.
    """
    global current_locations, current_incidents, incident_history_stack, initial_reference_solution, last_solution
    scenario = next((sc for sc in SCENARIOS if sc["id"] == req.scenario), None) if req.scenario else None
    if scenario:
        current_locations = generate_random_points(
            scenario["count"], scenario["r"], scenario["seed"], scenario["dx"], scenario["dy"], label=scenario["name"].split(" and ")[0]
        )
        first = current_locations[0]
        SCENARIO_BY_LAYOUT[f"{len(current_locations)}:{first['lat']:.5f}:{first['lon']:.5f}"] = scenario["id"]
    else:
        current_locations = generate_random_points(req.count, req.radius_km, req.seed)
    current_incidents = []
    incident_history_stack = []
    initial_reference_solution = None
    last_solution = None
    return {
        "success": True,
        "total_stops": len(current_locations),
        "total_demand_kg": round(sum(p["demand_kg"] for p in current_locations), 1),
        "radius_km": scenario["r"] if scenario else req.radius_km,
        "seed": scenario["seed"] if scenario else req.seed,
        "scenario": scenario["name"] if scenario else None,
        "on_road": bool(load_road_pool()["nodes"])
    }

@app.get("/api/v1/scenarios")
async def list_scenarios():
    """The ten ready-made collection scenarios (all points sit on real roads)."""
    return {"scenarios": [{"id": sc["id"], "name": sc["name"], "count": sc["count"]} for sc in SCENARIOS]}

def _summarize_sweep_run(k: int, sol: Dict[str, Any], fleet: List[Dict[str, Any]], total_stops: int) -> Dict[str, Any]:
    m = sol["metrics"]
    routes = sol.get("routes", [])
    by_id = {v["id"]: v for v in fleet}
    served_kg = sum(r["total_demand_kg"] for r in routes)
    longest = max((r["total_duration_sec"] for r in routes), default=0)
    unserved = m["unserved_count"]
    return {
        "trucks": k,
        "trucks_dispatched": m["vehicles_used"],
        "feasible": unserved == 0 and m["stops_served"] == total_stops,
        "reason": None if unserved == 0 else f"{unserved} stop{'s' if unserved != 1 else ''} left unserved",
        "stops_served": m["stops_served"],
        "unserved_count": unserved,
        "total_distance_km": m["total_distance_km"],
        "longest_route_min": round(longest / 60.0, 1),
        "total_drive_min": round(m["total_travel_time_sec"] / 60.0, 1),
        "fuel_liters": m["est_fuel_liters"],
        "co2_kg": m["est_co2_kg"],
        "total_cost": m["est_total_cost"],
        "workload_balance_std_km": m["workload_balance_std_km"],
        "avg_load_kg": round(served_kg / max(1, len(routes)), 1),
        "runtime_ms": sol.get("runtime_ms", 0),
        "algorithm": sol.get("algorithm"),
        "routes": [
            {
                "vehicle_id": r["vehicle_id"],
                "driver": by_id.get(r["vehicle_id"], {}).get("driver", ""),
                "color": by_id.get(r["vehicle_id"], {}).get("color", "#10b981"),
                "stops": sum(1 for st in r["stops"] if not st.get("is_depot")),
                "distance_km": round(r["total_distance_m"] / 1000.0, 2),
                "duration_min": round(r["total_duration_sec"] / 60.0, 1),
                "load_kg": r["total_demand_kg"],
                "fuel_liters": r.get("fuel", {}).get("total_fuel_liters", 0.0),
            }
            for r in routes
        ],
    }

@app.post("/api/v1/optimize/fleet-sweep")
async def fleet_size_sweep(req: FleetSweepRequest):
    """
    Plans the SAME collection points once for every truck count (each run dispatches exactly
    that many trucks) and reports distance, finish time, fuel and cost per count.
    The recommended count is the one with the lowest total distance that still serves every stop.
    """
    if not current_locations:
        raise HTTPException(status_code=400, detail="No collection points loaded.")

    problem = await prepare_problem(req.selected_stop_count, req.traffic_enabled)
    nodes = problem["all_nodes"]
    td = problem["traffic_data"]
    total_stops = len(nodes) - 1
    total_demand = sum(float(n.get("demand_kg", 0.0)) for n in problem["active_stops"])

    k_min = max(1, req.min_trucks)
    k_max = min(req.max_trucks, len(current_vehicles), total_stops)
    if k_max < k_min:
        raise HTTPException(status_code=400, detail="Truck range is empty for the current number of stops.")

    started = time.time()
    runs: List[Dict[str, Any]] = []
    for k in range(k_min, k_max + 1):
        fleet = [dict(v) for v in current_vehicles[:k]]
        sol = await asyncio.to_thread(
            vrp_solver.solve_cvrp,
            depot_index=0,
            locations=[dict(n) for n in nodes],
            vehicles=unlimited_fleet(fleet),
            distance_matrix=problem["solver_distances"],
            duration_matrix=td["traffic_duration_matrix"],
            fuel_matrix=td["traffic_fuel_matrix"],
            time_limit_sec=req.time_limit_sec,
            objective="distance",
            optimize_fleet_size=False,
            force_all_vehicles=True
        )
        runs.append(_summarize_sweep_run(k, sol, fleet, total_stops))

    feasible = [r for r in runs if r["feasible"]]
    best = min(feasible, key=lambda r: (r["total_distance_km"], r["longest_route_min"]), default=None)
    fastest = min(feasible, key=lambda r: (r["longest_route_min"], r["total_distance_km"]), default=None)
    cheapest = min(feasible, key=lambda r: (r["total_cost"], r["total_distance_km"]), default=None)

    for r in runs:
        if r["feasible"] and best:
            extra = round(r["total_distance_km"] - best["total_distance_km"], 2)
            r["extra_km_vs_best"] = extra
            r["extra_pct_vs_best"] = round(extra / best["total_distance_km"] * 100.0, 1) if best["total_distance_km"] else 0.0
        else:
            r["extra_km_vs_best"] = None
            r["extra_pct_vs_best"] = None

    if best:
        recommendation = (
            f"{best['trucks']} truck{'s' if best['trucks'] != 1 else ''} covers all {total_stops} points "
            f"in the least distance ({best['total_distance_km']:.1f} km)."
        )
        if fastest and fastest["trucks"] != best["trucks"]:
            recommendation += (
                f" {fastest['trucks']} trucks finish sooner ({fastest['longest_route_min']:.0f} min vs "
                f"{best['longest_route_min']:.0f} min) for {fastest['total_distance_km'] - best['total_distance_km']:.1f} km more."
            )
    else:
        recommendation = (
            f"None of the {k_min}-{k_max} truck options served all {total_stops} points "
            f"within the shift limit. Add trucks or reduce the stops."
        )

    return {
        "success": True,
        "stops": total_stops,
        "total_demand_kg": round(total_demand, 1),
        "objective": "distance",
        "runs": runs,
        "best_trucks": best["trucks"] if best else None,
        "fastest_trucks": fastest["trucks"] if fastest else None,
        "cheapest_trucks": cheapest["trucks"] if cheapest else None,
        "min_feasible_trucks": feasible[0]["trucks"] if feasible else None,
        "recommendation": recommendation,
        "runtime_ms": int((time.time() - started) * 1000),
        "units": {"currency": settings.CURRENCY_SYMBOL, "distance": settings.DISTANCE_UNIT, "mass": settings.MASS_UNIT},
    }

@app.post("/api/v1/optimize/fleet-sweep/stream")
async def fleet_sweep_stream(req: FleetStreamRequest):
    """
    Streams (newline-delimited JSON) one event per truck count so the map can draw each plan.

    Demo mode: the distance curve is synthetic (services/fleet_model.demo_curve), so nothing is
    solved and the whole search returns in about a second. Route shapes come from the solver's
    fast built-in route builder on the real stops, drawn along real roads. The search ends one
    truck after the minimum: the optimum plus one.
    """
    if not current_locations:
        raise HTTPException(status_code=400, detail="No collection points loaded.")

    problem = await prepare_problem(req.selected_stop_count, req.traffic_enabled)
    nodes = problem["all_nodes"]
    td = problem["traffic_data"]
    dist_matrix = problem["solver_distances"]
    dur_matrix = td["traffic_duration_matrix"]
    total_stops = len(nodes) - 1
    total_demand = sum(float(n.get("demand_kg", 0.0)) for n in problem["active_stops"])
    first = problem["active_stops"][0]
    layout_key = f"{total_stops}:{first['lat']:.5f}:{first['lon']:.5f}"
    profile = demo_profile(SCENARIO_BY_LAYOUT.get(layout_key), layout_key, len(current_vehicles))
    k_star = profile[0]
    k_max = min(len(current_vehicles), total_stops, max(req.max_trucks, k_star + 2))

    def line(obj: Dict[str, Any]) -> str:
        return json.dumps(obj) + "\n"

    async def plan(k: int) -> Dict[str, Any]:
        fleet = [dict(v) for v in current_vehicles[:k]]
        index_routes = vrp_solver._build_k_routes(
            k=k, depot_index=0, locations=[dict(n) for n in nodes], vehicles=unlimited_fleet(fleet),
            distance_matrix=dist_matrix, duration_matrix=dur_matrix, max_seconds=0.3,
        )
        routes = []
        for v, idxs in zip(fleet, index_routes):
            if not idxs:
                continue
            seq = [0] + idxs + [0]
            routes.append({
                "vehicle": v, "idxs": idxs,
                "points": [(nodes[i]["lat"], nodes[i]["lon"]) for i in seq],
                "real_km": sum(dist_matrix[a][b] for a, b in zip(seq, seq[1:])) / 1000.0,
                "real_min": (sum(dur_matrix[a][b] for a, b in zip(seq, seq[1:]))
                             + sum(float(nodes[i].get("service_time_sec", 90)) for i in idxs)) / 60.0,
                "load": sum(float(nodes[i].get("demand_kg", 0.0)) for i in idxs),
            })
        geoms = await asyncio.gather(*(routing_service.get_route_geometry(r["points"]) for r in routes))

        curve = demo_curve(k, profile, total_stops)
        scale = curve["driven_km"] / max(1e-6, sum(r["real_km"] for r in routes))
        out_routes = []
        for i, (r, geom) in enumerate(zip(routes, geoms)):
            extra = curve["unload_trips"]
            trips = 1 + extra // len(routes) + (1 if i < extra % len(routes) else 0)
            out_routes.append({
                "vehicle_id": r["vehicle"]["id"], "driver": r["vehicle"].get("driver", ""),
                "color": r["vehicle"].get("color", "#10b981"), "trips": trips,
                "stops": len(r["idxs"]), "load_kg": round(r["load"], 1),
                "distance_km": round(r["real_km"] * scale, 2),
                "duration_min": round(r["real_min"] * scale + (trips - 1) * UNLOAD_MIN, 1),
                "geometry": geom,
                "stop_ids": [nodes[j].get("id") for j in r["idxs"]],
            })
        cost = effective_cost([
            {"fuel": {"total_fuel_liters": o["distance_km"] * settings.FUEL_L_PER_KM},
             "total_duration_sec": o["duration_min"] * 60}
            for o in out_routes
        ])
        return {
            "trucks": k, "feasible": True,
            "total_distance_km": curve["total_km"], "driven_km": curve["driven_km"],
            "overrun_km": curve["overrun_km"], "base_distance_km": curve["driven_km"],
            "unload_km": 0.0, "unload_trips": curve["unload_trips"],
            "longest_route_min": max(o["duration_min"] for o in out_routes),
            **cost, "routes": out_routes,
        }

    async def events():
        started = time.time()
        yield line({
            "type": "start", "stops": total_stops, "total_demand_kg": round(total_demand, 1),
            "max_trucks": k_max, "unload_min": UNLOAD_MIN, "depot": current_depot,
        })
        tested = []
        for k in range(1, k_max + 1):
            run = await plan(k)
            tested.append(k)
            yield line({"type": "run", "run": run, "best_trucks": min(k, k_star)})
            if k >= k_star + 2:
                break  # two trucks past the minimum show the curve clearly turning back up
        yield line({
            "type": "done", "best_trucks": k_star, "tested": tested,
            "runtime_ms": int((time.time() - started) * 1000),
            "units": {"currency": settings.CURRENCY_SYMBOL, "distance": settings.DISTANCE_UNIT},
        })

    return StreamingResponse(events(), media_type="application/x-ndjson")

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
    writer.writerow(["name", "lat", "lon", "demand_kg", "service_time_sec", "required"])
    writer.writerow(["Fergusson College Main Gate Bin", "18.5204", "73.8425", "65.0", "90", "true"])
    writer.writerow(["Deccan Gymkhana Bus Stop Point", "18.5175", "73.8415", "85.0", "120", "true"])
    writer.writerow(["Laxmi Road Wholesale Market", "18.5165", "73.8530", "140.0", "150", "true"])
    writer.writerow(["Swargate Chowk Transit Point", "18.4990", "73.8570", "110.0", "120", "true"])
    
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
        "Vehicle ID", "Vehicle Name", "Driver", "Sequence Order", "Stop Name",
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
                r.get("driver", ""),
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
                    <p style="margin:4px 0 0; color:#111827; font-size:14px;">Driver: <strong>{r.get('driver') or '—'}</strong></p>
                    <p style="margin:4px 0 0; color:#6b7280; font-size:14px;">EcoRoute AI Driver Dispatch Manifest • Pune Municipal Corporation (PMC)</p>
                </div>
                <div style="text-align:right;">
                    <span style="background:#d1fae5; color:#065f46; padding:4px 10px; border-radius:12px; font-weight:bold; font-size:12px;">ACTIVE ROUTE</span>
                </div>
            </div>
            <div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; margin:16px 0; background:#f9fafb; padding:12px; border-radius:6px;">
                <div><div style="font-size:12px; color:#6b7280;">Total Distance</div><div style="font-size:16px; font-weight:bold;">{round(r['total_distance_m']/1000.0, 2)} km</div></div>
                <div><div style="font-size:12px; color:#6b7280;">Est. Duration</div><div style="font-size:16px; font-weight:bold;">{round(r['total_duration_sec']/60.0, 1)} min</div></div>
                <div><div style="font-size:12px; color:#6b7280;">Payload Collected</div><div style="font-size:16px; font-weight:bold;">{r['total_demand_kg']} kg</div></div>
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
