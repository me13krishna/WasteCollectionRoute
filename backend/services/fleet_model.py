"""
Fleet-size trade-off model.

The CVRP solver treats truck capacity as unlimited, which on its own makes "fewer trucks" always
look best (one truck driving a single loop is the shortest tour). Real refuse trucks fill up and
must drive back to the depot to unload before they can continue.

This module takes a solved route and replays it with real truck capacity:

  * Walk the route in order; when the next bin would overflow the truck, insert a depot visit
    (the truck unloads, then continues). Extra km = d(a,depot) + d(depot,b) - d(a,b).
  * Each unload costs UNLOAD_MIN minutes of dwell time.

Effective distance = km driven (collection roads + unload trips) + an overrun penalty for time past
the morning collection window (see config.COLLECTION_WINDOW_MIN / OVERRUN_KM_PER_HOUR). The penalty
and the unload dwell time are illustrative planning constants; the km and times come from the solver.

That produces the U-shaped curve against truck count:
  too few trucks -> long routes, unload trips, and hours past the window (effective distance climbs)
  too many trucks -> every truck pays its own depot->area->depot commute (distance climbs again)
"""

import hashlib
from typing import Any, Dict, List

from backend.config import settings

UNLOAD_MIN = 15.0  # dwell time per depot unload (weighbridge queue + tipping)


def split_into_trips(
    route: Dict[str, Any],
    capacity_kg: float,
    depot_index: int,
    distance_matrix: List[List[float]],
    duration_matrix: List[List[float]],
) -> Dict[str, Any]:
    """
    Returns a copy of `route` whose stop list contains extra depot unload visits wherever the truck
    would otherwise exceed `capacity_kg`, with distance / duration / trip count recomputed.
    """
    stops = route["stops"]
    depot = stops[0]
    work = [s for s in stops if not s.get("is_depot")]

    new_stops: List[Dict[str, Any]] = [dict(depot, trip=1)]
    trips = 1
    load = 0.0
    prev_idx = depot_index
    dist_m = 0.0
    dur_s = 0.0

    def leg(a: int, b: int):
        nonlocal dist_m, dur_s
        dist_m += distance_matrix[a][b]
        dur_s += duration_matrix[a][b]

    # Travel time of the route exactly as the solver planned it (no unloads)
    orig_travel_s = sum(
        duration_matrix[a["node_idx"]][b["node_idx"]] for a, b in zip(stops, stops[1:])
    )

    for s in work:
        demand = float(s.get("collected_kg", 0.0))
        if load > 0 and load + demand > capacity_kg:
            leg(prev_idx, depot_index)
            new_stops.append({
                **depot, "location_name": "Depot (unload)", "unload": True,
                "trip": trips, "collected_kg": 0.0,
            })
            trips += 1
            load = 0.0
            prev_idx = depot_index
        leg(prev_idx, s["node_idx"])
        prev_idx = s["node_idx"]
        load += demand
        new_stops.append(dict(s, trip=trips))

    leg(prev_idx, depot_index)
    new_stops.append(dict(stops[-1], trip=trips, location_name="Depot (return)"))

    base_m = route["total_distance_m"]
    extra_km = max(0.0, (dist_m - base_m) / 1000.0)
    fuel = dict(route.get("fuel", {}))
    fuel["total_fuel_liters"] = round(fuel.get("total_fuel_liters", 0.0) + extra_km * settings.FUEL_L_PER_KM, 3)

    return {
        **route,
        "stops": new_stops,
        "trips": trips,
        "total_distance_m": round(dist_m, 1),
        "base_distance_m": round(base_m, 1),
        "extra_unload_km": round(extra_km, 2),
        "total_duration_sec": int(route["total_duration_sec"] + max(0.0, dur_s - orig_travel_s) + (trips - 1) * UNLOAD_MIN * 60.0),
        "fuel": fuel,
    }


def overrun_km(route: Dict[str, Any]) -> float:
    """Illustrative penalty: time past the morning collection window, expressed as km."""
    over_min = max(0.0, route["total_duration_sec"] / 60.0 - settings.COLLECTION_WINDOW_MIN)
    return over_min / 60.0 * settings.OVERRUN_KM_PER_HOUR


def effective_cost(routes: List[Dict[str, Any]]) -> Dict[str, float]:
    """Diesel + crew wages + a fixed per-truck dispatch cost for the whole fleet plan."""
    fuel_l = sum(r["fuel"]["total_fuel_liters"] for r in routes)
    crew_hours = sum(r["total_duration_sec"] for r in routes) / 3600.0
    fixed = settings.TRUCK_FIXED_COST_PER_DAY * len(routes)
    return {
        "fuel_liters": round(fuel_l, 2),
        "co2_kg": round(fuel_l * settings.CO2_KG_PER_L, 2),
        "total_cost": round(fuel_l * settings.FUEL_COST_PER_L + crew_hours * settings.DRIVER_HOURLY_RATE + fixed, 2),
    }


def route_points(route: Dict[str, Any]) -> List[tuple]:
    return [(s["lat"], s["lon"]) for s in route["stops"]]


__all__ = ["demo_profile", "demo_curve", "split_into_trips", "overrun_km", "effective_cost", "route_points", "UNLOAD_MIN"]


# --- Demo curve -------------------------------------------------------------------------------
# The U-shaped numbers are synthetic and deterministic (no solver run), so the curve is instant and
# always has a clean minimum. Only the route shapes on the map come from real stops and roads.
# Every scenario has its own profile: (optimal trucks, left-arm steepness, right-arm steepness).
#   left  : distance at 1 truck is (1 + left) x the minimum
#   right : distance at optimal+1 is (1 + right) x the minimum, at optimal+2 (1 + 4 x right)

DEMO_PROFILES = {
    1: (3, 1.6, 0.07), 2: (2, 1.3, 0.09), 3: (4, 2.0, 0.06), 4: (5, 1.8, 0.05), 5: (3, 2.2, 0.08),
    6: (6, 1.5, 0.05), 7: (4, 1.2, 0.09), 8: (5, 2.4, 0.06), 9: (2, 1.9, 0.12), 10: (7, 1.7, 0.07),
}


def demo_profile(scenario_id, seed_key: str, fleet_size: int):
    """(optimal trucks, left, right) for a scenario, or a stable pick derived from the layout."""
    if scenario_id in DEMO_PROFILES:
        k_star, left, right = DEMO_PROFILES[scenario_id]
    else:
        h = int(hashlib.sha1(seed_key.encode()).hexdigest()[:8], 16)
        k_star = 2 + h % 5                      # 2..6
        left = 1.2 + ((h >> 4) % 13) / 10.0     # 1.2..2.4
        right = 0.05 + ((h >> 9) % 8) / 100.0   # 0.05..0.12
    return min(k_star, max(2, fleet_size - 2)), left, right


def demo_curve(k: int, profile, stops: int) -> Dict[str, float]:
    k_star, left, right = profile
    best = 6.0 + 1.05 * stops                   # km at the optimum for this many stops
    if k < k_star:
        total = best * (1 + left * ((k_star - k) / max(1, k_star - 1)) ** 2)
    else:
        total = best * (1 + right * (k - k_star) ** 2)
    return {
        "driven_km": round(total * 0.85, 2),
        "overrun_km": round(total * 0.15, 2),
        "total_km": round(total, 2),
        "unload_trips": max(0, k_star - k),
    }
