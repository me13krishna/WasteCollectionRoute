import time
import math
import logging
from typing import List, Dict, Any, Tuple, Optional
from backend.config import settings

logger = logging.getLogger(__name__)

# Try importing Google OR-Tools
ORTOOLS_AVAILABLE = False
try:
    from ortools.constraint_solver import routing_enums_pb2
    from ortools.constraint_solver import pywrapcp
    ORTOOLS_AVAILABLE = True
    logger.info("Google OR-Tools is installed and available.")
except ImportError:
    logger.warning("Google OR-Tools not yet found in current environment. Using high-performance Clarke-Wright + Local Search CVRP engine.")

class VrpSolverEngine:
    """
    Capacitated Vehicle Routing Problem (CVRP) optimization engine.
    Uses Google OR-Tools (PATH_CHEAPEST_ARC + GUIDED_LOCAL_SEARCH) when available,
    with an optimized Clarke-Wright Savings + 2-Opt local search fallback.
    """

    def solve_cvrp(
        self,
        depot_index: int,
        locations: List[Dict[str, Any]],  # includes depot at depot_index
        vehicles: List[Dict[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        time_limit_sec: int = 30,
        objective: str = "distance"
    ) -> Dict[str, Any]:
        start_time = time.time()
        
        # Check if ortools is available at runtime
        global ORTOOLS_AVAILABLE
        if not ORTOOLS_AVAILABLE:
            try:
                from ortools.constraint_solver import routing_enums_pb2
                from ortools.constraint_solver import pywrapcp
                ORTOOLS_AVAILABLE = True
            except ImportError:
                ORTOOLS_AVAILABLE = False

        if ORTOOLS_AVAILABLE:
            try:
                return self._solve_with_ortools(
                    depot_index=depot_index,
                    locations=locations,
                    vehicles=vehicles,
                    distance_matrix=distance_matrix,
                    duration_matrix=duration_matrix,
                    time_limit_sec=time_limit_sec,
                    objective=objective,
                    start_time=start_time
                )
            except Exception as e:
                logger.error(f"OR-Tools solver execution failed: {e}. Falling back to internal CVRP engine.")

        return self._solve_with_clark_wright(
            depot_index=depot_index,
            locations=locations,
            vehicles=vehicles,
            distance_matrix=distance_matrix,
            duration_matrix=duration_matrix,
            start_time=start_time
        )

    def _solve_with_ortools(
        self,
        depot_index: int,
        locations: List[Dict[str, Any]],
        vehicles: List[Dict[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        time_limit_sec: int,
        objective: str,
        start_time: float
    ) -> Dict[str, Any]:
        num_nodes = len(locations)
        num_vehicles = len(vehicles)
        
        # Create Routing Index Manager
        manager = pywrapcp.RoutingIndexManager(
            num_nodes,
            num_vehicles,
            depot_index
        )
        routing = pywrapcp.RoutingModel(manager)

        # Scale float matrix to integers for OR-Tools solver
        scale = 1.0  # distances in meters
        int_distance_matrix = [
            [int(d * scale) for d in row] for row in distance_matrix
        ]
        int_duration_matrix = [
            [int(d) for d in row] for row in duration_matrix
        ]

        # 1. Cost Evaluator
        if objective == "duration":
            def transit_callback(from_index, to_index):
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                return int_duration_matrix[from_node][to_node]
        else:
            def transit_callback(from_index, to_index):
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                return int_distance_matrix[from_node][to_node]

        transit_callback_index = routing.RegisterTransitCallback(transit_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

        # 2. Capacity Constraint Dimension
        demands = [int(float(loc.get("demand_kg", 0.0))) for loc in locations]
        vehicle_capacities = [int(float(v.get("capacity_kg", 1500.0))) for v in vehicles]

        def demand_callback(from_index):
            from_node = manager.IndexToNode(from_index)
            return demands[from_node]

        demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
        routing.AddDimensionWithVehicleCapacity(
            demand_callback_index,
            0,  # null capacity slack
            vehicle_capacities,  # vehicle maximum capacities
            True,  # start cumul to zero
            "Capacity"
        )

        # 3. Time / Shift Duration Dimension (optional constraint)
        def time_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            travel_time = int_duration_matrix[from_node][to_node]
            service_time = int(locations[from_node].get("service_time_sec", 90)) if from_node != depot_index else 0
            return travel_time + service_time

        time_callback_index = routing.RegisterTransitCallback(time_callback)
        max_durations = [int(v.get("max_duration_sec", 28800)) for v in vehicles]
        routing.AddDimensionWithVehicleCapacity(
            time_callback_index,
            3600,  # allow 1 hour slack for waiting
            max_durations,
            False,
            "Time"
        )

        # 4. Search Parameters as specified in Blueprint Section 4.3 & 5.5:
        # PATH_CHEAPEST_ARC + GUIDED_LOCAL_SEARCH
        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        search_parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_parameters.time_limit.seconds = min(time_limit_sec, 60)

        # Solve
        solution = routing.SolveWithParameters(search_parameters)

        if not solution:
            logger.warning("OR-Tools could not find a complete solution. Falling back to Clarke-Wright.")
            return self._solve_with_clark_wright(
                depot_index, locations, vehicles, distance_matrix, duration_matrix, start_time
            )

        # Extract solution
        routes: List[Dict[str, Any]] = []
        total_distance_m = 0.0
        total_travel_time_s = 0.0
        total_service_time_s = 0.0
        served_nodes = set()
        vehicle_distances: List[float] = []

        capacity_dimension = routing.GetDimensionOrDie("Capacity")
        time_dimension = routing.GetDimensionOrDie("Time")

        for vehicle_id_idx in range(num_vehicles):
            index = routing.Start(vehicle_id_idx)
            route_dist = 0.0
            route_stops: List[Dict[str, Any]] = []
            stop_indices: List[int] = []

            v_info = vehicles[vehicle_id_idx]

            while not routing.IsEnd(index):
                node_index = manager.IndexToNode(index)
                stop_indices.append(node_index)
                if node_index != depot_index:
                    served_nodes.add(node_index)

                load = solution.Value(capacity_dimension.CumulVar(index))
                arr_time = solution.Value(time_dimension.CumulVar(index))
                svc = float(locations[node_index].get("service_time_sec", 90)) if node_index != depot_index else 0
                dep_time = arr_time + svc

                stop_info = {
                    "node_idx": node_index,
                    "is_depot": (node_index == depot_index),
                    "location_id": locations[node_index].get("id") if node_index != depot_index else None,
                    "location_name": locations[node_index].get("name", "Depot"),
                    "lat": locations[node_index].get("lat"),
                    "lon": locations[node_index].get("lon"),
                    "arrival_sec": int(arr_time),
                    "departure_sec": int(dep_time),
                    "collected_kg": float(locations[node_index].get("demand_kg", 0.0)) if node_index != depot_index else 0.0,
                    "cumulative_kg": float(load),
                    "priority": locations[node_index].get("priority", 1) if node_index != depot_index else 0,
                }
                route_stops.append(stop_info)
                total_service_time_s += svc

                previous_index = index
                index = solution.Value(routing.NextVar(index))
                next_node = manager.IndexToNode(index)
                route_dist += distance_matrix[node_index][next_node]
                total_travel_time_s += duration_matrix[node_index][next_node]

            # Add final depot return
            end_node = manager.IndexToNode(index)
            stop_indices.append(end_node)
            final_load = solution.Value(capacity_dimension.CumulVar(index))
            final_time = solution.Value(time_dimension.CumulVar(index))
            route_stops.append({
                "node_idx": end_node,
                "is_depot": True,
                "location_id": None,
                "location_name": "Depot (Return)",
                "lat": locations[depot_index].get("lat"),
                "lon": locations[depot_index].get("lon"),
                "arrival_sec": int(final_time),
                "departure_sec": int(final_time),
                "collected_kg": 0.0,
                "cumulative_kg": float(final_load),
            })

            if len(stop_indices) > 2:  # More than just start depot -> end depot
                total_distance_m += route_dist
                vehicle_distances.append(route_dist / 1000.0)
                routes.append({
                    "vehicle_id": v_info["id"],
                    "vehicle_name": v_info.get("name"),
                    "vehicle_capacity_kg": v_info.get("capacity_kg"),
                    "total_distance_m": round(route_dist, 1),
                    "total_duration_sec": int(final_time),
                    "total_demand_kg": round(float(final_load), 1),
                    "stops": route_stops,
                    "stop_indices": stop_indices,
                })

        runtime_ms = int((time.time() - start_time) * 1000)
        total_dist_km = total_distance_m / 1000.0
        est_fuel_liters = total_dist_km * settings.FUEL_L_PER_KM
        est_co2_kg = est_fuel_liters * settings.CO2_KG_PER_L
        driver_hours = (total_travel_time_s + total_service_time_s) / 3600.0
        est_total_cost = (est_fuel_liters * settings.FUEL_COST_PER_L) + (driver_hours * settings.DRIVER_HOURLY_RATE)

        # Workload balance standard deviation
        if vehicle_distances:
            mean_dist = sum(vehicle_distances) / len(vehicle_distances)
            var_dist = sum((d - mean_dist) ** 2 for d in vehicle_distances) / len(vehicle_distances)
            std_dist = math.sqrt(var_dist)
        else:
            std_dist = 0.0

        all_target_nodes = set(i for i in range(num_nodes) if i != depot_index)
        unserved_indices = list(all_target_nodes - served_nodes)

        return {
            "algorithm": "Google OR-Tools CVRP (Guided Local Search)",
            "runtime_ms": runtime_ms,
            "routes": routes,
            "metrics": {
                "total_distance_km": round(total_dist_km, 2),
                "total_travel_time_sec": int(total_travel_time_s),
                "total_service_time_sec": int(total_service_time_s),
                "est_fuel_liters": round(est_fuel_liters, 2),
                "est_co2_kg": round(est_co2_kg, 2),
                "est_total_cost": round(est_total_cost, 2),
                "vehicles_used": len(routes),
                "stops_served": len(served_nodes),
                "unserved_count": len(unserved_indices),
                "workload_balance_std_km": round(std_dist, 2),
            },
            "unserved_indices": unserved_indices
        }

    def _solve_with_clark_wright(
        self,
        depot_index: int,
        locations: List[Dict[str, Any]],
        vehicles: List[Dict[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        start_time: float
    ) -> Dict[str, Any]:
        """
        Clarke-Wright Savings Algorithm with 2-Opt route improvements.
        Produces near-optimal multi-vehicle CVRP routes.
        """
        n = len(locations)
        customers = [i for i in range(n) if i != depot_index]
        
        # Calculate savings: s(i, j) = c(0, i) + c(0, j) - c(i, j)
        savings = []
        for idx_i in range(len(customers)):
            i = customers[idx_i]
            for idx_j in range(idx_i + 1, len(customers)):
                j = customers[idx_j]
                sav = distance_matrix[depot_index][i] + distance_matrix[depot_index][j] - distance_matrix[i][j]
                savings.append((sav, i, j))

        savings.sort(key=lambda x: x[0], reverse=True)

        # Initially each customer is on its own route: 0 -> i -> 0
        node_routes: Dict[int, List[int]] = {c: [c] for c in customers}
        route_demands: Dict[int, float] = {
            c: float(locations[c].get("demand_kg", 0.0)) for c in customers
        }
        
        # Max capacity across available vehicles
        vehicle_caps = sorted([float(v.get("capacity_kg", 1500.0)) for v in vehicles], reverse=True)
        max_cap = vehicle_caps[0] if vehicle_caps else 1500.0

        for sav, i, j in savings:
            route_i = node_routes.get(i)
            route_j = node_routes.get(j)

            if route_i is None or route_j is None or route_i is route_j:
                continue

            combined_demand = route_demands[route_i[0]] + route_demands[route_j[0]]
            if combined_demand > max_cap:
                continue

            # Merge conditions: i is end of route_i and j is start of route_j (or vice versa)
            if route_i[-1] == i and route_j[0] == j:
                merged = route_i + route_j
            elif route_j[-1] == j and route_i[0] == i:
                merged = route_j + route_i
            elif route_i[0] == i and route_j[0] == j:
                merged = list(reversed(route_i)) + route_j
            elif route_i[-1] == i and route_j[-1] == j:
                merged = route_i + list(reversed(route_j))
            else:
                continue

            # Update maps
            first_node = merged[0]
            route_demands[first_node] = combined_demand
            for node in merged:
                node_routes[node] = merged

        # Collect distinct routes
        unique_route_lists: List[List[int]] = []
        seen_refs = set()
        for c in customers:
            r = node_routes[c]
            r_id = id(r)
            if r_id not in seen_refs:
                seen_refs.add(r_id)
                unique_route_lists.append(r)

        # 2-Opt local search improvement on each route
        def two_opt(route: List[int]) -> List[int]:
            best = list(route)
            improved = True
            while improved:
                improved = False
                for a in range(1, len(best) - 1):
                    for b in range(a + 1, len(best)):
                        if b - a == 1:
                            continue
                        # Current distance
                        curr_d = (
                            distance_matrix[best[a-1]][best[a]] +
                            distance_matrix[best[b-1]][best[b if b < len(best) else depot_index]]
                        )
                        # New distance if reversed
                        rev_d = (
                            distance_matrix[best[a-1]][best[b-1]] +
                            distance_matrix[best[a]][best[b if b < len(best) else depot_index]]
                        )
                        if rev_d < curr_d - 1e-4:
                            best[a:b] = reversed(best[a:b])
                            improved = True
            return best

        improved_routes = [two_opt(r) for r in unique_route_lists]

        # Assign routes to vehicles
        assigned_routes: List[Dict[str, Any]] = []
        vehicle_distances: List[float] = []
        total_distance_m = 0.0
        total_travel_time_s = 0.0
        total_service_time_s = 0.0
        served_nodes = set()

        for v_idx, v in enumerate(vehicles):
            if v_idx >= len(improved_routes):
                break
            
            r_nodes = improved_routes[v_idx]
            v_id = v["id"]
            
            route_dist = 0.0
            route_time = 0.0
            curr_load = 0.0
            curr_node = depot_index

            stop_details: List[Dict[str, Any]] = []
            stop_indices = [depot_index]

            stop_details.append({
                "node_idx": depot_index,
                "is_depot": True,
                "arrival_sec": 0,
                "departure_sec": 0,
                "collected_kg": 0.0,
                "cumulative_kg": 0.0,
            })

            for node in r_nodes:
                travel_m = distance_matrix[curr_node][node]
                travel_s = duration_matrix[curr_node][node]
                svc_s = float(locations[node].get("service_time_sec", 90))
                demand = float(locations[node].get("demand_kg", 0.0))

                route_dist += travel_m
                route_time += travel_s
                arr = int(route_time)
                route_time += svc_s
                dep = int(route_time)
                curr_load += demand

                stop_indices.append(node)
                served_nodes.add(node)
                total_service_time_s += svc_s

                stop_details.append({
                    "node_idx": node,
                    "is_depot": False,
                    "location_id": locations[node].get("id"),
                    "location_name": locations[node].get("name"),
                    "lat": locations[node].get("lat"),
                    "lon": locations[node].get("lon"),
                    "arrival_sec": arr,
                    "departure_sec": dep,
                    "collected_kg": demand,
                    "cumulative_kg": round(curr_load, 1),
                    "priority": locations[node].get("priority", 1),
                })
                curr_node = node

            # Return to depot
            ret_m = distance_matrix[curr_node][depot_index]
            ret_s = duration_matrix[curr_node][depot_index]
            route_dist += ret_m
            route_time += ret_s
            stop_indices.append(depot_index)
            stop_details.append({
                "node_idx": depot_index,
                "is_depot": True,
                "arrival_sec": int(route_time),
                "departure_sec": int(route_time),
                "collected_kg": 0.0,
                "cumulative_kg": round(curr_load, 1),
            })

            total_distance_m += route_dist
            total_travel_time_s += (route_time - len(r_nodes) * 90)
            vehicle_distances.append(route_dist / 1000.0)

            assigned_routes.append({
                "vehicle_id": v_id,
                "vehicle_name": v.get("name"),
                "vehicle_capacity_kg": v.get("capacity_kg"),
                "total_distance_m": round(route_dist, 1),
                "total_duration_sec": int(route_time),
                "total_demand_kg": round(curr_load, 1),
                "stops": stop_details,
                "stop_indices": stop_indices,
            })

        runtime_ms = int((time.time() - start_time) * 1000)
        total_dist_km = total_distance_m / 1000.0
        est_fuel_liters = total_dist_km * settings.FUEL_L_PER_KM
        est_co2_kg = est_fuel_liters * settings.CO2_KG_PER_L
        driver_hours = (total_travel_time_s + total_service_time_s) / 3600.0
        est_total_cost = (est_fuel_liters * settings.FUEL_COST_PER_L) + (driver_hours * settings.DRIVER_HOURLY_RATE)

        if vehicle_distances:
            mean_dist = sum(vehicle_distances) / len(vehicle_distances)
            var_dist = sum((d - mean_dist) ** 2 for d in vehicle_distances) / len(vehicle_distances)
            std_dist = math.sqrt(var_dist)
        else:
            std_dist = 0.0

        all_target_nodes = set(customers)
        unserved_indices = list(all_target_nodes - served_nodes)

        return {
            "algorithm": "Clarke-Wright Savings CVRP + 2-Opt Local Search",
            "runtime_ms": runtime_ms,
            "routes": assigned_routes,
            "metrics": {
                "total_distance_km": round(total_dist_km, 2),
                "total_travel_time_sec": int(total_travel_time_s),
                "total_service_time_sec": int(total_service_time_s),
                "est_fuel_liters": round(est_fuel_liters, 2),
                "est_co2_kg": round(est_co2_kg, 2),
                "est_total_cost": round(est_total_cost, 2),
                "vehicles_used": len(assigned_routes),
                "stops_served": len(served_nodes),
                "unserved_count": len(unserved_indices),
                "workload_balance_std_km": round(std_dist, 2),
            },
            "unserved_indices": unserved_indices
        }

vrp_solver = VrpSolverEngine()
