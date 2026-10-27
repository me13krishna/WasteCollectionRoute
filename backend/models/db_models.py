import datetime
from sqlalchemy import Column, Integer, Float, String, Boolean, DateTime, ForeignKey, Text, JSON
from sqlalchemy.orm import relationship
from backend.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    role = Column(String(50), default="planner")  # admin, planner, driver, viewer
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class Depot(Base):
    __tablename__ = "depots"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    address = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    vehicles = relationship("Vehicle", back_populates="depot")

class CollectionLocation(Base):
    __tablename__ = "collection_locations"

    id = Column(Integer, primary_key=True, index=True)
    external_ref = Column(String(100), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    demand_kg = Column(Float, default=50.0)
    service_time_sec = Column(Integer, default=90)
    priority = Column(Integer, default=1)  # 1: Normal, 2: Moderate, 3: High, 4: Urgent
    required = Column(Boolean, default=True)
    status = Column(String(50), default="pending")  # pending, assigned, completed, missed, skipped
    address = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

class Vehicle(Base):
    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    license_plate = Column(String(50), nullable=True)
    capacity_kg = Column(Float, default=1500.0)
    max_duration_sec = Column(Integer, default=28800)  # 8 hours shift
    depot_id = Column(Integer, ForeignKey("depots.id"), nullable=True)
    status = Column(String(50), default="available")  # available, maintenance, disabled, busy
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    depot = relationship("Depot", back_populates="vehicles")
    route_plans = relationship("RoutePlan", back_populates="vehicle")

class OptimizationRun(Base):
    __tablename__ = "optimization_runs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    status = Column(String(50), default="completed")  # running, completed, failed
    objective = Column(String(50), default="distance")  # distance, duration, balanced
    config_json = Column(JSON, nullable=True)
    runtime_ms = Column(Integer, default=0)
    solver_algorithm = Column(String(100), default="OR-Tools CVRP (Guided Local Search)")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    route_plans = relationship("RoutePlan", back_populates="run", cascade="all, delete-orphan")
    metrics = relationship("RouteMetric", back_populates="run", uselist=False, cascade="all, delete-orphan")

class RoutePlan(Base):
    __tablename__ = "route_plans"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("optimization_runs.id"), nullable=False)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False)
    total_distance_m = Column(Float, default=0.0)
    total_duration_sec = Column(Integer, default=0)
    total_demand_kg = Column(Float, default=0.0)
    geometry_geojson = Column(JSON, nullable=True)  # LineString coordinates [[lon, lat], ...]
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    run = relationship("OptimizationRun", back_populates="route_plans")
    vehicle = relationship("Vehicle", back_populates="route_plans")
    stops = relationship("RouteStop", back_populates="route_plan", cascade="all, delete-orphan", order_by="RouteStop.sequence_order")

class RouteStop(Base):
    __tablename__ = "route_stops"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("route_plans.id"), nullable=False)
    location_id = Column(Integer, ForeignKey("collection_locations.id"), nullable=True)  # Null if depot
    is_depot = Column(Boolean, default=False)
    sequence_order = Column(Integer, nullable=False)
    arrival_time_est_sec = Column(Integer, default=0)
    departure_time_est_sec = Column(Integer, default=0)
    demand_collected_kg = Column(Float, default=0.0)
    cumulative_load_kg = Column(Float, default=0.0)
    status = Column(String(50), default="planned")  # planned, completed, missed, skipped

    route_plan = relationship("RoutePlan", back_populates="stops")
    location = relationship("CollectionLocation")

class Incident(Base):
    __tablename__ = "incidents"

    id = Column(Integer, primary_key=True, index=True)
    incident_type = Column(String(50), nullable=False)  # road_closure, missed_stop, vehicle_breakdown, emergency_pickup
    description = Column(String(500), nullable=False)
    lat = Column(Float, nullable=True)
    lon = Column(Float, nullable=True)
    affected_location_id = Column(Integer, ForeignKey("collection_locations.id"), nullable=True)
    affected_vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class RouteMetric(Base):
    __tablename__ = "route_metrics"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("optimization_runs.id"), nullable=False)
    total_distance_km = Column(Float, default=0.0)
    total_travel_time_sec = Column(Integer, default=0)
    total_service_time_sec = Column(Integer, default=0)
    est_fuel_liters = Column(Float, default=0.0)
    est_co2_kg = Column(Float, default=0.0)
    est_total_cost = Column(Float, default=0.0)
    vehicles_used = Column(Integer, default=0)
    stops_served = Column(Integer, default=0)
    unserved_count = Column(Integer, default=0)
    workload_balance_std_km = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    run = relationship("OptimizationRun", back_populates="metrics")
