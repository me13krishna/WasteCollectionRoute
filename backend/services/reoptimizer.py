import time
import logging
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from backend.models.db_models import (
    OptimizationRun, RoutePlan, RouteStop, CollectionLocation, Vehicle, Incident, RouteMetric
)
from backend.services.routing_matrix import routing_service
from backend.services.vrp_solver import vrp_solver
from backend.services.baseline_solver import baseline_solver

logger = logging.getLogger(__name__)

class DynamicReoptimizer:
    """
    Event-driven dynamic reoptimization service (software-only).
    Handles dynamic triggers:
    - Stop status changes (completed, missed, urgent)
    - Road closures / incidents
    - Vehicle availability / capacity changes
    - Priority adjustments
    """

    async def reoptimize_active_plan(
        self,
        db: AsyncSession,
        run_id: int,
        trigger_reason: str,
        preserve_completed: bool = True
    ) -> Dict[str, Any]:
        """
        Reoptimizes an existing run. Keeps completed stops fixed, updates remaining stops,
        and generates a revised plan while archiving the prior plan.
        """
        # 1. Fetch current run and plans
        stmt = select(OptimizationRun).where(OptimizationRun.id == run_id)
        res = await db.execute(stmt)
        old_run = res.scalar_one_or_none()
        if not old_run:
            raise ValueError(f"Optimization run {run_id} not found.")

        # 2. Fetch all locations and vehicles
        loc_stmt = select(CollectionLocation).order_by(CollectionLocation.id)
        loc_res = await db.execute(loc_stmt)
        all_locations = loc_res.scalars().all()

        veh_stmt = select(Vehicle).where(Vehicle.status.in_(["available", "assigned"]))
        veh_res = await db.execute(veh_stmt)
        available_vehicles = veh_res.scalars().all()

        # 3. Check for active road closure incidents
        inc_stmt = select(Incident).where(Incident.active == True)
        inc_res = await db.execute(inc_stmt)
        active_incidents = inc_res.scalars().all()

        # 4. Filter locations that still need pickup
        # Only pending or missed locations that need re-routing
        pending_locations = [
            loc for loc in all_locations
            if loc.status in ["pending", "missed"]
        ]

        if not pending_locations:
            return {
                "message": "All locations have already been serviced or are skipped.",
                "run_id": run_id
            }

        # 5. Build coordinate set with depot
        # For simplicity, default depot at (12.9716, 77.5946) or from db
        from backend.models.db_models import Depot
        depot_res = await db.execute(select(Depot).where(Depot.is_active == True))
        depot = depot_res.scalars().first()
        if not depot:
            depot_data = {"id": 0, "name": "Central Municipal Depot", "lat": 12.9716, "lon": 77.5946}
        else:
            depot_data = {"id": depot.id, "name": depot.name, "lat": depot.lat, "lon": depot.lon}

        loc_dicts = [
            {
                "id": loc.id,
                "name": loc.name,
                "lat": loc.lat,
                "lon": loc.lon,
                "demand_kg": loc.demand_kg,
                "service_time_sec": loc.service_time_sec,
                "priority": loc.priority,
            }
            for loc in pending_locations
        ]

        coords = [(depot_data["lat"], depot_data["lon"])] + [(l["lat"], l["lon"]) for l in loc_dicts]
        distances, durations = await routing_service.get_distance_and_duration_matrix(coords)

        # Apply incident penalties if road closure is near any segment
        for inc in active_incidents:
            if inc.incident_type == "road_closure" and inc.lat and inc.lon:
                # Penalize connections passing close to closure
                for i in range(len(coords)):
                    for j in range(len(coords)):
                        if i != j:
                            mid_lat = (coords[i][0] + coords[j][0]) / 2.0
                            mid_lon = (coords[i][1] + coords[j][1]) / 2.0
                            if abs(mid_lat - inc.lat) < 0.005 and abs(mid_lon - inc.lon) < 0.005:
                                distances[i][j] *= 10.0
                                durations[i][j] *= 10.0

        veh_dicts = [
            {
                "id": v.id,
                "name": v.name,
                "capacity_kg": v.capacity_kg,
                "max_duration_sec": v.max_duration_sec
            }
            for v in available_vehicles
        ]

        # 6. Re-solve with CVRP
        all_nodes_data = [depot_data] + loc_dicts
        solution = vrp_solver.solve_cvrp(
            depot_index=0,
            locations=all_nodes_data,
            vehicles=veh_dicts,
            distance_matrix=distances,
            duration_matrix=durations,
            time_limit_sec=20
        )

        # 7. Create new optimization run record
        new_run = OptimizationRun(
            name=f"Reoptimized: {old_run.name} ({trigger_reason})",
            status="completed",
            objective="distance",
            config_json={"reoptimized_from_run": run_id, "trigger": trigger_reason},
            runtime_ms=solution.get("runtime_ms", 0),
            solver_algorithm=solution.get("algorithm", "OR-Tools CVRP")
        )
        db.add(new_run)
        await db.flush()

        # 8. Save updated route plans & geometries
        for route_data in solution["routes"]:
            pts = [(s["lat"], s["lon"]) for s in route_data["stops"]]
            geometry = await routing_service.get_route_geometry(pts)

            route_plan = RoutePlan(
                run_id=new_run.id,
                vehicle_id=route_data["vehicle_id"],
                total_distance_m=route_data["total_distance_m"],
                total_duration_sec=route_data["total_duration_sec"],
                total_demand_kg=route_data["total_demand_kg"],
                geometry_geojson=geometry,
                is_active=True
            )
            db.add(route_plan)
            await db.flush()

            for seq, s in enumerate(route_data["stops"]):
                r_stop = RouteStop(
                    plan_id=route_plan.id,
                    location_id=s.get("location_id"),
                    is_depot=s.get("is_depot", False),
                    sequence_order=seq,
                    arrival_time_est_sec=s.get("arrival_sec", 0),
                    departure_time_est_sec=s.get("departure_sec", 0),
                    demand_collected_kg=s.get("collected_kg", 0.0),
                    cumulative_load_kg=s.get("cumulative_kg", 0.0),
                    status="planned"
                )
                db.add(r_stop)

        # 9. Save metrics
        metrics_data = solution["metrics"]
        metric_record = RouteMetric(
            run_id=new_run.id,
            total_distance_km=metrics_data["total_distance_km"],
            total_travel_time_sec=metrics_data["total_travel_time_sec"],
            total_service_time_sec=metrics_data["total_service_time_sec"],
            est_fuel_liters=metrics_data["est_fuel_liters"],
            est_co2_kg=metrics_data["est_co2_kg"],
            est_total_cost=metrics_data["est_total_cost"],
            vehicles_used=metrics_data["vehicles_used"],
            stops_served=metrics_data["stops_served"],
            unserved_count=metrics_data["unserved_count"],
            workload_balance_std_km=metrics_data["workload_balance_std_km"]
        )
        db.add(metric_record)
        await db.commit()

        return {
            "success": True,
            "old_run_id": run_id,
            "new_run_id": new_run.id,
            "trigger_reason": trigger_reason,
            "metrics": metrics_data
        }

reoptimizer = DynamicReoptimizer()
