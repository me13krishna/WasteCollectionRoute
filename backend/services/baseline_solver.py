import math
from typing import List, Dict, Any, Tuple
from backend.config import settings
from backend.services.vrp_solver import compute_route_fuel

class BaselineNearestNeighborSolver:
    """
    Constructive Nearest-Neighbor heuristic baseline for CVRP.
    Represents traditional dispatch heuristics used by municipal dispatchers.
    Uses identical stops, vehicle capacities, and cost/fuel formulas as OR-Tools.
    """

    def solve(
        self,
        depot_index: int,
        locations: List[Dict[str, Any]],  # includes depot at depot_index
        vehicles: List[Dict[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
    ) -> Dict[str, Any]:
        """
        Solves CVRP with nearest-neighbor heuristic:
        - Each vehicle starts at depot.
        - Greedily selects the nearest valid collection stop whose demand <= remaining capacity.
        - Returns to depot when full or max_duration reached.
        - Dispatches subsequent vehicles until all stops visited or fleet exhausted.
        """
        n = len(locations)
        if n <= 1 or not vehicles:
            return {
                "algorithm": "Nearest-Neighbor Constructive Baseline",
                "routes": [],
                "metrics": {
                    "total_distance_km": 0.0,
                    "total_travel_time_sec": 0,
                    "total_service_time_sec": 0,
                    "est_fuel_liters": 0.0,
                    "est_co2_kg": 0.0,
                    "est_total_cost": 0.0,
                    "currency_symbol": settings.CURRENCY_SYMBOL,
                    "currency_code": settings.CURRENCY_CODE,
                    "vehicles_used": 0,
                    "vehicles_idle": len(vehicles),
                    "vehicles_available": len(vehicles),
                    "fleet_utilization_pct": 0.0,
                    "stops_served": 0,
                    "unserved_count": max(0, n - 1),
                    "workload_balance_std_km": 0.0,
                },
                "unserved_indices": [i for i in range(n) if i != depot_index],
                "unserved_stops": []
            }

        unvisited = set(i for i in range(n) if i != depot_index)
        
        routes: List[Dict[str, Any]] = []
        total_distance_m = 0.0
        total_travel_time_s = 0.0
        total_service_time_s = 0.0
        
        vehicle_distances: List[float] = []

        for v in vehicles:
            if not unvisited:
                break

            v_id = v["id"]
            v_cap = float(v.get("capacity_kg", 1500.0))
            max_duration = float(v.get("max_duration_sec", 28800))
            
            curr_node = depot_index
            curr_load = 0.0
            curr_time = 0.0
            route_dist = 0.0
            
            route_stop_indices: List[int] = [depot_index]
            stop_details: List[Dict[str, Any]] = []

            # Add depot departure
            stop_details.append({
                "node_idx": depot_index,
                "is_depot": True,
                "location_id": None,
                "location_name": locations[depot_index].get("name", "Depot"),
                "lat": locations[depot_index].get("lat"),
                "lon": locations[depot_index].get("lon"),
                "arrival_sec": 0,
                "departure_sec": 0,
                "collected_kg": 0.0,
                "cumulative_kg": 0.0,
            })

            while unvisited:
                # Find nearest unvisited node that fits vehicle capacity & time
                best_node = None
                best_dist = float("inf")

                for cand in unvisited:
                    cand_demand = float(locations[cand].get("demand_kg", 0.0))
                    if curr_load + cand_demand <= v_cap:
                        travel_dist = distance_matrix[curr_node][cand]
                        travel_time = duration_matrix[curr_node][cand]
                        return_time = duration_matrix[cand][depot_index]
                        svc_time = float(locations[cand].get("service_time_sec", 90))

                        # Check if vehicle can make it to cand and back to depot within max_duration
                        if curr_time + travel_time + svc_time + return_time <= max_duration:
                            if travel_dist < best_dist:
                                best_dist = travel_dist
                                best_node = cand

                if best_node is None:
                    # Vehicle cannot serve any more locations; return to depot
                    break

                # Visit best_node
                travel_m = distance_matrix[curr_node][best_node]
                travel_s = duration_matrix[curr_node][best_node]
                svc_s = float(locations[best_node].get("service_time_sec", 90))
                demand = float(locations[best_node].get("demand_kg", 0.0))

                route_dist += travel_m
                curr_load += demand
                curr_time += travel_s
                arrival_s = int(curr_time)
                curr_time += svc_s
                departure_s = int(curr_time)

                route_stop_indices.append(best_node)
                stop_details.append({
                    "node_idx": best_node,
                    "is_depot": False,
                    "location_id": locations[best_node].get("id"),
                    "location_name": locations[best_node].get("name"),
                    "lat": locations[best_node].get("lat"),
                    "lon": locations[best_node].get("lon"),
                    "arrival_sec": arrival_s,
                    "departure_sec": departure_s,
                    "collected_kg": demand,
                    "cumulative_kg": round(curr_load, 1),
                    "priority": locations[best_node].get("priority", 1),
                })

                total_service_time_s += svc_s
                curr_node = best_node
                unvisited.remove(best_node)

            # Return to depot if left depot
            if len(route_stop_indices) > 1:
                return_dist = distance_matrix[curr_node][depot_index]
                return_dur = duration_matrix[curr_node][depot_index]
                route_dist += return_dist
                curr_time += return_dur

                route_stop_indices.append(depot_index)
                stop_details.append({
                    "node_idx": depot_index,
                    "is_depot": True,
                    "location_id": None,
                    "location_name": "Depot (Return)",
                    "lat": locations[depot_index].get("lat"),
                    "lon": locations[depot_index].get("lon"),
                    "arrival_sec": int(curr_time),
                    "departure_sec": int(curr_time),
                    "collected_kg": 0.0,
                    "cumulative_kg": round(curr_load, 1),
                })

                total_distance_m += route_dist
                total_travel_time_s += (curr_time - (len(route_stop_indices) - 2) * 90)
                vehicle_distances.append(route_dist / 1000.0)

                r_fuel = compute_route_fuel(stop_details, distance_matrix, duration_matrix, v_cap)
                routes.append({
                    "vehicle_id": v_id,
                    "vehicle_name": v.get("name"),
                    "vehicle_capacity_kg": v_cap,
                    "total_distance_m": round(route_dist, 1),
                    "total_duration_sec": int(curr_time),
                    "total_demand_kg": round(curr_load, 1),
                    "stops": stop_details,
                    "stop_indices": route_stop_indices,
                    "fuel": r_fuel,
                })

        # Calculate metrics
        total_dist_km = total_distance_m / 1000.0
        total_fuel_liters = sum(r["fuel"]["total_fuel_liters"] for r in routes) if routes else 0.0
        total_cruise_fuel = sum(r["fuel"]["cruising_fuel_liters"] for r in routes) if routes else 0.0
        total_idle_fuel = sum(r["fuel"]["traffic_idling_fuel_liters"] for r in routes) if routes else 0.0
        total_pto_fuel = sum(r["fuel"]["compactor_pto_fuel_liters"] for r in routes) if routes else 0.0

        est_co2_kg = total_fuel_liters * settings.CO2_KG_PER_L
        driver_hours = (total_travel_time_s + total_service_time_s) / 3600.0
        est_total_cost = (total_fuel_liters * settings.FUEL_COST_PER_L) + (driver_hours * settings.DRIVER_HOURLY_RATE)

        # Workload balance standard deviation
        if vehicle_distances:
            mean_dist = sum(vehicle_distances) / len(vehicle_distances)
            var_dist = sum((d - mean_dist) ** 2 for d in vehicle_distances) / len(vehicle_distances)
            std_dist = math.sqrt(var_dist)
        else:
            std_dist = 0.0

        unserved_indices = sorted(list(unvisited))
        unserved_stops_info = [
            {
                "location_id": locations[i].get("id"),
                "node_idx": i,
                "name": locations[i].get("name", f"Stop #{i}"),
                "lat": locations[i].get("lat"),
                "lon": locations[i].get("lon"),
                "demand_kg": locations[i].get("demand_kg", 0.0),
                "priority": locations[i].get("priority", 1),
                "reason": "fleet_exhausted" if len(routes) == len(vehicles) else "capacity_or_duration_limit"
            }
            for i in unserved_indices
        ]

        vehicles_used = len(routes)
        vehicles_idle = max(0, len(vehicles) - vehicles_used)
        fleet_util_pct = round((vehicles_used / max(1, len(vehicles))) * 100.0, 1)

        return {
            "algorithm": "Nearest-Neighbor Constructive Baseline",
            "routes": routes,
            "metrics": {
                "total_distance_km": round(total_dist_km, 2),
                "total_travel_time_sec": int(total_travel_time_s),
                "total_service_time_sec": int(total_service_time_s),
                "est_fuel_liters": round(total_fuel_liters, 2),
                "fuel_breakdown": {
                    "cruising_fuel_liters": round(total_cruise_fuel, 2),
                    "traffic_idling_fuel_liters": round(total_idle_fuel, 2),
                    "compactor_pto_fuel_liters": round(total_pto_fuel, 2),
                },
                "est_co2_kg": round(est_co2_kg, 2),
                "est_total_cost": round(est_total_cost, 2),
                "currency_symbol": settings.CURRENCY_SYMBOL,
                "currency_code": settings.CURRENCY_CODE,
                "vehicles_used": vehicles_used,
                "vehicles_idle": vehicles_idle,
                "vehicles_available": len(vehicles),
                "fleet_utilization_pct": fleet_util_pct,
                "stops_served": n - 1 - len(unvisited),
                "unserved_count": len(unvisited),
                "workload_balance_std_km": round(std_dist, 2),
            },
            "unserved_indices": unserved_indices,
            "unserved_stops": unserved_stops_info
        }

baseline_solver = BaselineNearestNeighborSolver()
