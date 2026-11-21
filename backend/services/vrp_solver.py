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
    logger.warning("Google OR-Tools not yet found in current environment. Using Clarke-Wright + Local Search CVRP engine.")

def compute_route_fuel(
    stops: List[Dict[str, Any]],
    distance_matrix: List[List[float]],
    duration_matrix: List[List[float]],
    vehicle_capacity_kg: float
) -> Dict[str, float]:
    """
    Computes load-dependent and traffic-congestion-aware diesel fuel consumption
    according to the European Environmental Agency (EEA) MEET Refuse Truck Model.
    """
    cruising_fuel = 0.0
    idling_delay_fuel = 0.0

    for i in range(len(stops) - 1):
        u = stops[i]["node_idx"]
        v = stops[i+1]["node_idx"]
        dist_km = distance_matrix[u][v] / 1000.0
        dur_s = duration_matrix[u][v]
        curr_load = stops[i].get("cumulative_kg", 0.0)
        load_ratio = min(1.0, max(0.0, curr_load / max(1.0, vehicle_capacity_kg)))

        # 1. Load-dependent consumption: 0.36 L/km empty up to 0.54 L/km fully loaded
        rate_l_per_km = 0.36 + (0.18 * load_ratio)
        cruising_fuel += dist_km * rate_l_per_km

        # 2. Traffic congestion delay idling:
        # Free flow urban truck speed ~ 32 km/h (8.88 m/s). Any duration above free-flow is traffic delay
        free_flow_s = (dist_km / 32.0) * 3600.0 if dist_km > 0 else 0.0
        delay_hrs = max(0.0, (dur_s - free_flow_s) / 3600.0)
        idling_delay_fuel += delay_hrs * settings.TRUCK_IDLE_FUEL_L_PER_HR

    # 3. Hydraulic packer compaction PTO fuel: ~0.035 Liters per collection stop
    actual_collection_stops = max(0, len(stops) - 2)
    pto_compaction_fuel = actual_collection_stops * settings.COMPACTOR_PTO_L_PER_STOP

    total_fuel = cruising_fuel + idling_delay_fuel + pto_compaction_fuel
    return {
        "cruising_fuel_liters": round(cruising_fuel, 2),
        "traffic_idling_fuel_liters": round(idling_delay_fuel, 2),
        "compactor_pto_fuel_liters": round(pto_compaction_fuel, 2),
        "total_fuel_liters": round(total_fuel, 2)
    }

class VrpSolverEngine:
    """
    Capacitated Vehicle Routing Problem (CVRP) optimization engine.
    Uses Google OR-Tools (PATH_CHEAPEST_ARC + GUIDED_LOCAL_SEARCH) when available,
    with an optimized Clarke-Wright Savings + 2-Opt local search fallback.
    """

    def validate_inputs(
        self,
        depot_index: int,
        locations: List[Dict[str, Any]],
        vehicles: List[Dict[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]]
    ) -> Tuple[bool, Optional[str]]:
        """Validates all input dimensions, bounds, and values before running solver."""
        if not locations:
            return False, "Locations list is empty."
        if depot_index < 0 or depot_index >= len(locations):
            return False, f"Invalid depot_index: {depot_index}. Must be between 0 and {len(locations)-1}."
        if not vehicles:
            return False, "At least one vehicle must be provided."
        
        n = len(locations)
        if len(distance_matrix) != n or any(len(row) != n for row in distance_matrix):
            return False, f"distance_matrix dimensions ({len(distance_matrix)}x?) do not match locations count ({n})."
        if len(duration_matrix) != n or any(len(row) != n for row in duration_matrix):
            return False, f"duration_matrix dimensions ({len(duration_matrix)}x?) do not match locations count ({n})."
        
        for idx, loc in enumerate(locations):
            if "lat" not in loc or "lon" not in loc:
                return False, f"Location at index {idx} missing lat or lon."
            if float(loc.get("demand_kg", 0.0)) < 0:
                loc["demand_kg"] = 0.0

        for v in vehicles:
            if float(v.get("capacity_kg", 0.0)) <= 0:
                v["capacity_kg"] = 1500.0
            if int(v.get("max_duration_sec", 0)) <= 0:
                v["max_duration_sec"] = 28800

        return True, None

    def solve_cvrp(
        self,
        depot_index: int,
        locations: List[Dict[str, Any]],  # includes depot at depot_index
        vehicles: List[Dict[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        fuel_matrix: Optional[List[List[float]]] = None,
        time_limit_sec: int = 15,
        objective: str = "fuel",
        optimize_fleet_size: bool = True,
        force_all_vehicles: bool = False
    ) -> Dict[str, Any]:
        """
        force_all_vehicles=True dispatches *every* vehicle passed in (each gets at least
        one stop). Used for "what if I send exactly N trucks?" comparisons.
        """
        start_time = time.time()
        
        # 1. Validation
        valid, err = self.validate_inputs(
            depot_index=depot_index,
            locations=locations,
            vehicles=vehicles,
            distance_matrix=distance_matrix,
            duration_matrix=duration_matrix
        )
        if not valid:
            logger.error(f"CVRP input validation error: {err}")
            return {
                "algorithm": "Validation Error",
                "runtime_ms": int((time.time() - start_time) * 1000),
                "routes": [],
                "metrics": {
                    "total_distance_km": 0.0,
                    "total_travel_time_sec": 0,
                    "total_service_time_sec": 0,
                    "est_fuel_liters": 0.0,
                    "fuel_breakdown": {"cruising_fuel_liters": 0.0, "traffic_idling_fuel_liters": 0.0, "compactor_pto_fuel_liters": 0.0},
                    "est_co2_kg": 0.0,
                    "est_total_cost": 0.0,
                    "vehicles_used": 0,
                    "vehicles_idle": len(vehicles),
                    "vehicles_available": len(vehicles),
                    "fleet_utilization_pct": 0.0,
                    "stops_served": 0,
                    "unserved_count": len(locations) - 1,
                    "workload_balance_std_km": 0.0,
                },
                "unserved_indices": [i for i in range(len(locations)) if i != depot_index],
                "unserved_stops": [
                    {
                        "location_id": locations[i].get("id"),
                        "name": locations[i].get("name", f"Stop #{i}"),
                        "lat": locations[i].get("lat"),
                        "lon": locations[i].get("lon"),
                        "demand_kg": locations[i].get("demand_kg", 0.0),
                        "priority": locations[i].get("priority", 1),
                        "reason": err
                    }
                    for i in range(len(locations)) if i != depot_index
                ],
                "error": err
            }

        # Clamp time limit between 1 and 60 seconds
        time_limit_sec = max(1, min(time_limit_sec, 60))

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
                    fuel_matrix=fuel_matrix,
                    time_limit_sec=time_limit_sec,
                    objective=objective,
                    optimize_fleet_size=optimize_fleet_size,
                    force_all_vehicles=force_all_vehicles,
                    start_time=start_time
                )
            except Exception as e:
                logger.error(f"OR-Tools solver execution failed: {e}. Falling back to internal CVRP engine.")

        if force_all_vehicles:
            return self._solve_exact_fleet_heuristic(
                depot_index=depot_index,
                locations=locations,
                vehicles=vehicles,
                distance_matrix=distance_matrix,
                duration_matrix=duration_matrix,
                start_time=start_time
            )

        return self._solve_with_clark_wright(
            depot_index=depot_index,
            locations=locations,
            vehicles=vehicles,
            distance_matrix=distance_matrix,
            duration_matrix=duration_matrix,
            start_time=start_time
        )

    def _diagnose_unserved_stops(
        self,
        unserved_indices: List[int],
        locations: List[Dict[str, Any]],
        vehicles: List[Dict[str, Any]],
        duration_matrix: List[List[float]],
        depot_index: int,
        total_served_demand: float
    ) -> List[Dict[str, Any]]:
        """Determines the specific operational reason each unserved stop could not be scheduled."""
        total_fleet_capacity = sum(float(v.get("capacity_kg", 1500.0)) for v in vehicles)
        remaining_fleet_cap = max(0.0, total_fleet_capacity - total_served_demand)
        max_shift = max(float(v.get("max_duration_sec", 28800)) for v in vehicles) if vehicles else 28800

        unserved_details = []
        for idx in unserved_indices:
            loc = locations[idx]
            demand = float(loc.get("demand_kg", 0.0))
            priority = int(loc.get("priority", 1))
            svc_s = float(loc.get("service_time_sec", 90))
            roundtrip_s = duration_matrix[depot_index][idx] + duration_matrix[idx][depot_index] + svc_s

            if demand > remaining_fleet_cap:
                reason = "capacity_exceeded"
            elif roundtrip_s > max_shift:
                reason = "shift_time_exceeded"
            else:
                reason = "fleet_exhausted"

            unserved_details.append({
                "location_id": loc.get("id"),
                "node_idx": idx,
                "name": loc.get("name", f"Stop #{idx}"),
                "lat": loc.get("lat"),
                "lon": loc.get("lon"),
                "demand_kg": demand,
                "priority": priority,
                "reason": reason
            })
        return unserved_details

    def _solve_with_ortools(
        self,
        depot_index: int,
        locations: List[Dict[str, Any]],
        vehicles: List[Dict[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        fuel_matrix: Optional[List[List[float]]],
        time_limit_sec: int,
        objective: str,
        optimize_fleet_size: bool,
        start_time: float,
        force_all_vehicles: bool = False
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
        if objective == "fuel" and fuel_matrix:
            int_fuel_matrix = [
                [int(f * 1000.0) for f in row] for row in fuel_matrix
            ]
            def transit_callback(from_index, to_index):
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                return int_fuel_matrix[from_node][to_node]
        elif objective in ["duration", "traffic_duration"]:
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

        # 2. Dynamic Fleet Sizing: Minimize number of vehicles used
        if optimize_fleet_size and not force_all_vehicles:
            fixed_cost = 35000 if (objective == "fuel" and fuel_matrix) else 30000
            routing.SetFixedCostOfAllVehicles(fixed_cost)

        # 3. Capacity Constraint Dimension
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

        # 4. Time / Shift Duration Dimension
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
            3600,  # 1 hour slack for waiting
            max_durations,
            False,
            "Time"
        )

        # 5. Disjunctions & Penalty-Weighted Drop Handling.
        # A stop is only dropped when serving it is impossible (capacity / shift time), never
        # because the detour looks expensive: penalties are floored at a multiple of the most
        # expensive arc (an insertion never costs more than ~2x that).
        if objective == "fuel" and fuel_matrix:
            cost_rows = int_fuel_matrix
        elif objective in ["duration", "traffic_duration"]:
            cost_rows = int_duration_matrix
        else:
            cost_rows = int_distance_matrix
        max_arc = max((max(row) for row in cost_rows if row), default=0)
        penalty = max(50000, 3 * max_arc)  # every stop counts the same
        for node in range(num_nodes):
            if node != depot_index:
                routing.AddDisjunction([manager.NodeToIndex(node)], penalty)

        # 5b. Exact fleet: every dispatched truck must leave the depot with at least one stop.
        if force_all_vehicles:
            for v in range(num_vehicles):
                routing.solver().Add(routing.NextVar(routing.Start(v)) != routing.End(v))

        # 6. Search Parameters
        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        search_parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_parameters.time_limit.seconds = time_limit_sec

        # Solve
        solution = None
        if force_all_vehicles:
            # Sequential first-solution strategies cannot satisfy "every truck is used", so seed
            # the search with our own balanced k-route plan and let guided local search refine it.
            seed = self._build_k_routes(
                k=num_vehicles,
                depot_index=depot_index,
                locations=locations,
                vehicles=vehicles,
                distance_matrix=distance_matrix,
                duration_matrix=duration_matrix
            )
            routing.CloseModelWithParameters(search_parameters)
            initial = routing.ReadAssignmentFromRoutes(seed, True)
            if initial is not None:
                solution = routing.SolveFromAssignmentWithParameters(initial, search_parameters)
            if not solution:
                logger.warning("OR-Tools exact-fleet seeding failed; using the built-in k-route solver.")
                return self._solve_exact_fleet_heuristic(
                    depot_index, locations, vehicles, distance_matrix, duration_matrix, start_time
                )
        else:
            solution = routing.SolveWithParameters(search_parameters)

        if not solution:
            logger.warning("OR-Tools could not find a solution. Falling back to Clarke-Wright.")
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

            # Final depot return
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

            if len(stop_indices) > 2:  # Active vehicle route
                total_distance_m += route_dist
                vehicle_distances.append(route_dist / 1000.0)
                v_cap = float(v_info.get("capacity_kg", 1500.0))
                r_fuel = compute_route_fuel(route_stops, distance_matrix, duration_matrix, float(v_info.get("fuel_ref_capacity_kg", v_cap)))
                routes.append({
                    "vehicle_id": v_info["id"],
                    "vehicle_name": v_info.get("name"),
                    "vehicle_capacity_kg": v_cap,
                    "total_distance_m": round(route_dist, 1),
                    "total_duration_sec": int(final_time),
                    "total_demand_kg": round(float(final_load), 1),
                    "stops": route_stops,
                    "stop_indices": stop_indices,
                    "fuel": r_fuel,
                })

        runtime_ms = int((time.time() - start_time) * 1000)
        total_dist_km = total_distance_m / 1000.0
        
        total_fuel_liters = sum(r["fuel"]["total_fuel_liters"] for r in routes)
        total_cruise_fuel = sum(r["fuel"]["cruising_fuel_liters"] for r in routes)
        total_idle_fuel = sum(r["fuel"]["traffic_idling_fuel_liters"] for r in routes)
        total_pto_fuel = sum(r["fuel"]["compactor_pto_fuel_liters"] for r in routes)

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

        all_target_nodes = set(i for i in range(num_nodes) if i != depot_index)
        unserved_indices = sorted(list(all_target_nodes - served_nodes))

        total_served_demand = sum(r["total_demand_kg"] for r in routes)
        unserved_stops_info = self._diagnose_unserved_stops(
            unserved_indices=unserved_indices,
            locations=locations,
            vehicles=vehicles,
            duration_matrix=duration_matrix,
            depot_index=depot_index,
            total_served_demand=total_served_demand
        )

        vehicles_used = len(routes)
        vehicles_idle = max(0, num_vehicles - vehicles_used)
        fleet_util_pct = round((vehicles_used / max(1, num_vehicles)) * 100.0, 1)

        return {
            "algorithm": "Google OR-Tools CVRP (Guided Local Search)",
            "runtime_ms": runtime_ms,
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
                "vehicles_available": num_vehicles,
                "fleet_utilization_pct": fleet_util_pct,
                "stops_served": len(served_nodes),
                "unserved_count": len(unserved_indices),
                "workload_balance_std_km": round(std_dist, 2),
            },
            "unserved_indices": unserved_indices,
            "unserved_stops": unserved_stops_info
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

        # 2-Opt local search improvement
        def two_opt(route: List[int]) -> List[int]:
            best = list(route)
            improved = True
            while improved:
                improved = False
                for a in range(1, len(best) - 1):
                    for b in range(a + 1, len(best)):
                        if b - a == 1:
                            continue
                        curr_d = (
                            distance_matrix[best[a-1]][best[a]] +
                            distance_matrix[best[b-1]][best[b if b < len(best) else depot_index]]
                        )
                        rev_d = (
                            distance_matrix[best[a-1]][best[b-1]] +
                            distance_matrix[best[a]][best[b if b < len(best) else depot_index]]
                        )
                        if rev_d < curr_d - 1e-4:
                            best[a:b] = reversed(best[a:b])
                            improved = True
            return best

        improved_routes = [two_opt(r) for r in unique_route_lists]

        return self._assemble_from_routes(
            algorithm="Clarke-Wright Savings CVRP + 2-Opt Local Search",
            route_node_lists=improved_routes,
            depot_index=depot_index,
            locations=locations,
            vehicles=vehicles,
            distance_matrix=distance_matrix,
            duration_matrix=duration_matrix,
            start_time=start_time
        )

    # ------------------------------------------------------------------
    # Exact-fleet (dispatch exactly k trucks) heuristic
    # ------------------------------------------------------------------
    def _build_k_routes(
        self,
        k: int,
        depot_index: int,
        locations: List[Dict[str, Any]],
        vehicles: List[Dict[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        max_seconds: float = 1.0
    ) -> List[List[int]]:
        """
        Builds one route per vehicle (list aligned with `vehicles`, depot excluded) using exactly
        the first `k` vehicles, each with at least one stop when there are enough stops.

        1. Farthest-point seeding gives every truck its own area.
        2. Remaining stops go in by cheapest feasible insertion (capacity + shift time).
        3. Local search (2-opt, relocate, swap across trucks) shortens total distance.
        """
        D = distance_matrix
        T = duration_matrix
        dep = depot_index
        customers = [i for i in range(len(locations)) if i != dep]
        routes: List[List[int]] = [[] for _ in vehicles]
        if not customers:
            return routes

        k = max(1, min(k, len(vehicles), len(customers)))
        caps = [float(v.get("capacity_kg", 1500.0)) for v in vehicles]
        shifts = [float(v.get("max_duration_sec", 28800)) for v in vehicles]
        dem = {c: float(locations[c].get("demand_kg", 0.0)) for c in customers}
        svc = {c: float(locations[c].get("service_time_sec", 90)) for c in customers}
        active = list(range(k))

        def route_time(r: List[int]) -> float:
            if not r:
                return 0.0
            t = T[dep][r[0]] + sum(svc[n] for n in r)
            for a, b in zip(r, r[1:]):
                t += T[a][b]
            return t + T[r[-1]][dep]

        # 1. Seeds (k-center on the distance matrix, starting from the stop farthest from the depot)
        seeds = [max(customers, key=lambda c: D[dep][c])]
        while len(seeds) < k:
            rest = [c for c in customers if c not in seeds]
            seeds.append(max(rest, key=lambda c: min(D[c][s] for s in seeds)))
        by_cap = sorted(active, key=lambda v: -caps[v])
        for v, seed in zip(by_cap, sorted(seeds, key=lambda c: -dem[c])):
            routes[v] = [seed]
        loads = [sum(dem[n] for n in r) for r in routes]
        times = [route_time(r) for r in routes]

        # 2. Cheapest feasible insertion. Big stops first when the fleet is nearly full.
        placed = set(seeds)
        remaining = [c for c in customers if c not in placed]
        tight = sum(dem.values()) > 0.85 * sum(caps[v] for v in active)
        remaining.sort(key=(lambda c: -dem[c]) if tight else (lambda c: -D[dep][c]))
        unplaced: List[int] = []
        for c in remaining:
            best = None
            for v in active:
                if loads[v] + dem[c] > caps[v]:
                    continue
                r = routes[v]
                for pos in range(len(r) + 1):
                    prev = r[pos - 1] if pos > 0 else dep
                    nxt = r[pos] if pos < len(r) else dep
                    if times[v] + T[prev][c] + T[c][nxt] - T[prev][nxt] + svc[c] > shifts[v]:
                        continue
                    cost = D[prev][c] + D[c][nxt] - D[prev][nxt]
                    if best is None or cost < best[0]:
                        best = (cost, v, pos)
            if best is None:
                unplaced.append(c)
                continue
            _, v, pos = best
            routes[v].insert(pos, c)
            loads[v] += dem[c]
            times[v] = route_time(routes[v])

        # 3. Local search
        def path_len(r: List[int]) -> float:
            if not r:
                return 0.0
            return D[dep][r[0]] + sum(D[a][b] for a, b in zip(r, r[1:])) + D[r[-1]][dep]

        def two_opt(v: int) -> bool:
            r = routes[v]
            improved_any = False
            improved = True
            while improved:
                improved = False
                path = [dep] + r + [dep]
                for a in range(1, len(path) - 2):
                    for b in range(a + 1, len(path) - 1):
                        delta = (D[path[a - 1]][path[b]] + D[path[a]][path[b + 1]]
                                 - D[path[a - 1]][path[a]] - D[path[b]][path[b + 1]])
                        if delta < -1e-6:
                            # Road distances are asymmetric: reversing a stretch also changes the
                            # cost of its inner arcs, so confirm with the exact directed cost
                            # (otherwise a "gain" can be a loss and the loop never ends).
                            seg = path[a - 1:b + 2]
                            rev = path[a - 1:a] + path[b:a - 1:-1] + path[b + 1:b + 2]
                            old_c = sum(D[x][y] for x, y in zip(seg, seg[1:]))
                            new_c = sum(D[x][y] for x, y in zip(rev, rev[1:]))
                            if new_c < old_c - 1e-6:
                                path[a:b + 1] = path[a:b + 1][::-1]
                                improved = improved_any = True
                r[:] = path[1:-1]
            if improved_any:
                times[v] = route_time(r)
            return improved_any

        deadline = time.time() + max_seconds
        moved = True
        while moved and time.time() < deadline:
            moved = False
            for v in active:
                moved |= two_opt(v)

            # relocate one stop to another truck
            for a in active:
                if len(routes[a]) < 2:
                    continue
                done = False
                for i, c in enumerate(routes[a]):
                    ra = routes[a]
                    p = ra[i - 1] if i > 0 else dep
                    n = ra[i + 1] if i + 1 < len(ra) else dep
                    gain = D[p][c] + D[c][n] - D[p][n]
                    for b in active:
                        if b == a or loads[b] + dem[c] > caps[b]:
                            continue
                        rb = routes[b]
                        for pos in range(len(rb) + 1):
                            q = rb[pos - 1] if pos > 0 else dep
                            m = rb[pos] if pos < len(rb) else dep
                            add = D[q][c] + D[c][m] - D[q][m]
                            if add - gain < -1e-6 and times[b] + T[q][c] + T[c][m] - T[q][m] + svc[c] <= shifts[b]:
                                rb.insert(pos, c)
                                del ra[i]
                                loads[a] -= dem[c]
                                loads[b] += dem[c]
                                times[a] = route_time(ra)
                                times[b] = route_time(rb)
                                moved = done = True
                                break
                        if done:
                            break
                    if done:
                        break

            # swap two stops between trucks
            for a in active:
                for b in active:
                    if b <= a:
                        continue
                    done = False
                    for i, c in enumerate(routes[a]):
                        for j, e in enumerate(routes[b]):
                            if loads[a] - dem[c] + dem[e] > caps[a] or loads[b] - dem[e] + dem[c] > caps[b]:
                                continue
                            ra, rb = routes[a], routes[b]
                            pa = ra[i - 1] if i > 0 else dep
                            na = ra[i + 1] if i + 1 < len(ra) else dep
                            pb = rb[j - 1] if j > 0 else dep
                            nb = rb[j + 1] if j + 1 < len(rb) else dep
                            old = D[pa][c] + D[c][na] + D[pb][e] + D[e][nb]
                            new = D[pa][e] + D[e][na] + D[pb][c] + D[c][nb]
                            if new - old < -1e-6:
                                ra[i], rb[j] = e, c
                                new_ta, new_tb = route_time(ra), route_time(rb)
                                if new_ta > shifts[a] or new_tb > shifts[b]:
                                    ra[i], rb[j] = c, e  # undo
                                    continue
                                loads[a] += dem[e] - dem[c]
                                loads[b] += dem[c] - dem[e]
                                times[a], times[b] = new_ta, new_tb
                                moved = done = True
                                break
                        if done:
                            break

        return routes

    def _solve_exact_fleet_heuristic(
        self,
        depot_index: int,
        locations: List[Dict[str, Any]],
        vehicles: List[Dict[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        start_time: float
    ) -> Dict[str, Any]:
        """Pure-Python 'dispatch every truck given' solver (no OR-Tools needed)."""
        routes = self._build_k_routes(
            k=len(vehicles),
            depot_index=depot_index,
            locations=locations,
            vehicles=vehicles,
            distance_matrix=distance_matrix,
            duration_matrix=duration_matrix,
            max_seconds=2.0
        )
        return self._assemble_from_routes(
            algorithm="Balanced k-route heuristic (seeding + 2-opt / relocate / swap)",
            route_node_lists=routes,
            depot_index=depot_index,
            locations=locations,
            vehicles=vehicles,
            distance_matrix=distance_matrix,
            duration_matrix=duration_matrix,
            start_time=start_time
        )

    def _assemble_from_routes(
        self,
        algorithm: str,
        route_node_lists: List[List[int]],
        depot_index: int,
        locations: List[Dict[str, Any]],
        vehicles: List[Dict[str, Any]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        start_time: float
    ) -> Dict[str, Any]:
        """
        Turns per-vehicle node lists (aligned with `vehicles`, depot excluded) into the standard
        solution payload: stop timeline, per-route fuel and fleet-level metrics.
        """
        customers = [i for i in range(len(locations)) if i != depot_index]
        assigned_routes: List[Dict[str, Any]] = []
        vehicle_distances: List[float] = []
        total_distance_m = 0.0
        total_travel_time_s = 0.0
        total_service_time_s = 0.0
        served_nodes = set()

        for v_idx, v in enumerate(vehicles):
            if v_idx >= len(route_node_lists) or not route_node_lists[v_idx]:
                continue

            r_nodes = route_node_lists[v_idx]
            v_cap = float(v.get("capacity_kg", 1500.0))

            route_dist = 0.0
            route_travel = 0.0
            route_time = 0.0
            curr_load = 0.0
            curr_node = depot_index

            stop_indices = [depot_index]
            stop_details: List[Dict[str, Any]] = [{
                "node_idx": depot_index,
                "is_depot": True,
                "location_id": None,
                "location_name": "Depot",
                "lat": locations[depot_index].get("lat"),
                "lon": locations[depot_index].get("lon"),
                "arrival_sec": 0,
                "departure_sec": 0,
                "collected_kg": 0.0,
                "cumulative_kg": 0.0,
            }]

            for node in r_nodes:
                travel_m = distance_matrix[curr_node][node]
                travel_s = duration_matrix[curr_node][node]
                svc_s = float(locations[node].get("service_time_sec", 90))
                demand = float(locations[node].get("demand_kg", 0.0))

                route_dist += travel_m
                route_travel += travel_s
                route_time += travel_s
                arr = int(route_time)
                route_time += svc_s
                dep_t = int(route_time)
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
                    "departure_sec": dep_t,
                    "collected_kg": demand,
                    "cumulative_kg": round(curr_load, 1),
                    "priority": locations[node].get("priority", 1),
                })
                curr_node = node

            route_dist += distance_matrix[curr_node][depot_index]
            route_travel += duration_matrix[curr_node][depot_index]
            route_time += duration_matrix[curr_node][depot_index]
            stop_indices.append(depot_index)
            stop_details.append({
                "node_idx": depot_index,
                "is_depot": True,
                "location_id": None,
                "location_name": "Depot (Return)",
                "lat": locations[depot_index].get("lat"),
                "lon": locations[depot_index].get("lon"),
                "arrival_sec": int(route_time),
                "departure_sec": int(route_time),
                "collected_kg": 0.0,
                "cumulative_kg": round(curr_load, 1),
            })

            total_distance_m += route_dist
            total_travel_time_s += route_travel
            vehicle_distances.append(route_dist / 1000.0)

            assigned_routes.append({
                "vehicle_id": v["id"],
                "vehicle_name": v.get("name"),
                "vehicle_capacity_kg": v_cap,
                "total_distance_m": round(route_dist, 1),
                "total_duration_sec": int(route_time),
                "total_demand_kg": round(curr_load, 1),
                "stops": stop_details,
                "stop_indices": stop_indices,
                "fuel": compute_route_fuel(stop_details, distance_matrix, duration_matrix, float(v.get("fuel_ref_capacity_kg", v_cap))),
            })

        runtime_ms = int((time.time() - start_time) * 1000)
        total_dist_km = total_distance_m / 1000.0

        total_fuel_liters = sum(r["fuel"]["total_fuel_liters"] for r in assigned_routes)
        total_cruise_fuel = sum(r["fuel"]["cruising_fuel_liters"] for r in assigned_routes)
        total_idle_fuel = sum(r["fuel"]["traffic_idling_fuel_liters"] for r in assigned_routes)
        total_pto_fuel = sum(r["fuel"]["compactor_pto_fuel_liters"] for r in assigned_routes)

        est_co2_kg = total_fuel_liters * settings.CO2_KG_PER_L
        driver_hours = (total_travel_time_s + total_service_time_s) / 3600.0
        est_total_cost = (total_fuel_liters * settings.FUEL_COST_PER_L) + (driver_hours * settings.DRIVER_HOURLY_RATE)

        if vehicle_distances:
            mean_dist = sum(vehicle_distances) / len(vehicle_distances)
            std_dist = math.sqrt(sum((d - mean_dist) ** 2 for d in vehicle_distances) / len(vehicle_distances))
        else:
            std_dist = 0.0

        unserved_indices = sorted(set(customers) - served_nodes)
        unserved_stops_info = self._diagnose_unserved_stops(
            unserved_indices=unserved_indices,
            locations=locations,
            vehicles=vehicles,
            duration_matrix=duration_matrix,
            depot_index=depot_index,
            total_served_demand=sum(r["total_demand_kg"] for r in assigned_routes)
        )

        return {
            "algorithm": algorithm,
            "runtime_ms": runtime_ms,
            "routes": assigned_routes,
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
                "vehicles_used": len(assigned_routes),
                "vehicles_idle": max(0, len(vehicles) - len(assigned_routes)),
                "vehicles_available": len(vehicles),
                "fleet_utilization_pct": round((len(assigned_routes) / max(1, len(vehicles))) * 100.0, 1),
                "stops_served": len(served_nodes),
                "unserved_count": len(unserved_indices),
                "workload_balance_std_km": round(std_dist, 2),
            },
            "unserved_indices": unserved_indices,
            "unserved_stops": unserved_stops_info
        }

vrp_solver = VrpSolverEngine()
