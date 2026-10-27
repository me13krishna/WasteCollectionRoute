from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field
import datetime

# --- Auth Schemas ---
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    full_name: Optional[str] = None
    email: str

class TokenData(BaseModel):
    email: Optional[str] = None
    role: Optional[str] = None

class UserLogin(BaseModel):
    email: str
    password: str

class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str
    role: str = "planner"

class UserOut(BaseModel):
    id: int
    email: str
    full_name: Optional[str]
    role: str
    created_at: datetime.datetime

    class Config:
        from_attributes = True

# --- Depot Schemas ---
class DepotBase(BaseModel):
    name: str
    lat: float
    lon: float
    address: Optional[str] = None
    is_active: bool = True

class DepotCreate(DepotBase):
    pass

class DepotOut(DepotBase):
    id: int
    created_at: datetime.datetime

    class Config:
        from_attributes = True

# --- Location Schemas ---
class LocationBase(BaseModel):
    name: str
    lat: float
    lon: float
    demand_kg: float = 50.0
    service_time_sec: int = 90
    priority: int = 1  # 1 to 4
    required: bool = True
    address: Optional[str] = None
    external_ref: Optional[str] = None

class LocationCreate(LocationBase):
    pass

class LocationUpdate(BaseModel):
    name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    demand_kg: Optional[float] = None
    service_time_sec: Optional[int] = None
    priority: Optional[int] = None
    required: Optional[bool] = None
    status: Optional[str] = None
    address: Optional[str] = None

class LocationStatusUpdate(BaseModel):
    status: str  # pending, assigned, completed, missed, skipped

class LocationOut(LocationBase):
    id: int
    status: str
    created_at: datetime.datetime
    updated_at: datetime.datetime

    class Config:
        from_attributes = True

# --- Vehicle Schemas ---
class VehicleBase(BaseModel):
    name: str
    license_plate: Optional[str] = None
    capacity_kg: float = 1500.0
    max_duration_sec: int = 28800
    depot_id: Optional[int] = None
    status: str = "available"

class VehicleCreate(VehicleBase):
    pass

class VehicleUpdate(BaseModel):
    name: Optional[str] = None
    license_plate: Optional[str] = None
    capacity_kg: Optional[float] = None
    max_duration_sec: Optional[int] = None
    status: Optional[str] = None
    depot_id: Optional[int] = None

class VehicleOut(VehicleBase):
    id: int
    created_at: datetime.datetime

    class Config:
        from_attributes = True

# --- Stop & Route Schemas ---
class RouteStopOut(BaseModel):
    id: int
    location_id: Optional[int]
    is_depot: bool
    sequence_order: int
    arrival_time_est_sec: int
    departure_time_est_sec: int
    demand_collected_kg: float
    cumulative_load_kg: float
    status: str
    location_name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    priority: Optional[int] = None

    class Config:
        from_attributes = True

class RoutePlanOut(BaseModel):
    id: int
    vehicle_id: int
    vehicle_name: Optional[str] = None
    vehicle_capacity_kg: Optional[float] = None
    total_distance_m: float
    total_duration_sec: int
    total_demand_kg: float
    geometry_geojson: Optional[Any] = None
    stops: List[RouteStopOut] = []

    class Config:
        from_attributes = True

class RouteMetricOut(BaseModel):
    total_distance_km: float
    total_travel_time_sec: int
    total_service_time_sec: int
    est_fuel_liters: float
    est_co2_kg: float
    est_total_cost: float
    vehicles_used: int
    stops_served: int
    unserved_count: int
    workload_balance_std_km: float

    class Config:
        from_attributes = True

class OptimizationConfig(BaseModel):
    objective: str = "distance"  # distance, duration, balance
    time_limit_sec: int = 30
    allow_unserved: bool = False
    vehicle_ids: Optional[List[int]] = None
    depot_id: Optional[int] = None
    location_ids: Optional[List[int]] = None

class OptimizationRequest(BaseModel):
    name: str = "Municipal Waste Collection Shift"
    config: OptimizationConfig = Field(default_factory=OptimizationConfig)

class OptimizationRunOut(BaseModel):
    id: int
    name: str
    status: str
    objective: str
    runtime_ms: int
    solver_algorithm: str
    created_at: datetime.datetime
    metrics: Optional[RouteMetricOut] = None
    route_plans: List[RoutePlanOut] = []

    class Config:
        from_attributes = True

# --- Incident Schemas ---
class IncidentCreate(BaseModel):
    incident_type: str  # road_closure, missed_stop, vehicle_breakdown, emergency_pickup
    description: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    affected_location_id: Optional[int] = None
    affected_vehicle_id: Optional[int] = None

class IncidentOut(BaseModel):
    id: int
    incident_type: str
    description: str
    lat: Optional[float]
    lon: Optional[float]
    affected_location_id: Optional[int]
    affected_vehicle_id: Optional[int]
    active: bool
    created_at: datetime.datetime

    class Config:
        from_attributes = True

# --- Baseline Comparison ---
class ComparisonItem(BaseModel):
    metric_name: str
    baseline_value: float
    optimized_value: float
    difference: float
    percent_improvement: float
    unit: str

class ComparisonReport(BaseModel):
    run_id: int
    baseline_algorithm: str = "Nearest-Neighbor Constructive Heuristic (Greedy)"
    optimized_algorithm: str = "Google OR-Tools CVRP (Guided Local Search)"
    comparison: List[ComparisonItem]
    methodology_notes: str
