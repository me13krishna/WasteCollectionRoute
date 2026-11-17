import datetime
import math
import logging
from typing import List, Tuple, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Known real-world high-density traffic bottleneck corridors in Pune
PUNE_TRAFFIC_HOTSPOTS = [
    {"name": "Shivajinagar & Sancheti Circle", "lat": 18.5305, "lon": 73.8505, "base_congestion": 1.45, "radius_m": 800},
    {"name": "Swargate ST Bus Stand & Chowk", "lat": 18.4990, "lon": 73.8570, "base_congestion": 1.50, "radius_m": 900},
    {"name": "Laxmi Road & Tulshibaug Bazaar Core", "lat": 18.5165, "lon": 73.8530, "base_congestion": 1.65, "radius_m": 700},
    {"name": "Deccan Gymkhana & Goodluck Chowk", "lat": 18.5175, "lon": 73.8415, "base_congestion": 1.40, "radius_m": 600},
    {"name": "Karve Road Nal Stop Concourse", "lat": 18.5065, "lon": 73.8320, "base_congestion": 1.45, "radius_m": 750},
    {"name": "Hadapsar Gadital Bottleneck", "lat": 18.5020, "lon": 73.9280, "base_congestion": 1.55, "radius_m": 1000},
    {"name": "Pune University Circle / SB Road", "lat": 18.5330, "lon": 73.8310, "base_congestion": 1.35, "radius_m": 800},
    {"name": "Market Yard Gultekdi Wholesale Hub", "lat": 18.4870, "lon": 73.8670, "base_congestion": 1.50, "radius_m": 800},
]

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

class RealTimeTrafficEngine:
    """
    Real-Time Traffic & Dynamic Congestion Engine for Municipal Waste Route Optimization.
    Calculates time-of-day traffic flow, road bottlenecks, and live incident delays.
    Generates speed-adjusted duration matrices and realistic load-aware fuel cost matrices.
    """

    def get_time_of_day_congestion_factor(self, now: Optional[datetime.datetime] = None) -> Tuple[float, str, str]:
        """
        Determines current urban traffic congestion factor based on time of day.
        Returns: (multiplier, period_name, description)
        """
        if now is None:
            now = datetime.datetime.now()

        hour = now.hour + (now.minute / 60.0)

        # 08:30 - 10:30: Morning Rush Hour (commuter peak + morning street markets)
        if 8.5 <= hour <= 10.5:
            return 1.75, "Morning Peak", "Heavy morning commuter rush and street wholesale activities. Speeds reduced by ~43%."

        # 10:50 - 16:30: Mid-Day Commercial Operations
        elif 10.5 < hour < 16.5:
            return 1.25, "Mid-Day Flow", "Moderate commercial vehicle & shopper traffic. Speeds reduced by ~20%."

        # 16:30 - 20:30: Evening Peak Congestion (highest urban traffic density)
        elif 16.5 <= hour <= 20.5:
            return 1.95, "Evening Peak", "Severe evening rush hour across commercial bazaars and arterial bridges. Speeds reduced by ~49%."

        # 20:30 - 22:30: Post-Dinner / Night Cool-down
        elif 20.5 < hour < 22.5:
            return 1.15, "Late Evening", "Decreasing urban traffic with minor commercial slowdowns."

        # 22:30 - 08:30: Night & Early Morning Free Flow
        else:
            return 1.00, "Night / Early Morning", "Free-flow road traffic conditions. Maximum vehicle operating speed."

    def apply_traffic_to_matrices(
        self,
        coordinates: List[Tuple[float, float]],
        distance_matrix: List[List[float]],
        duration_matrix: List[List[float]],
        incidents: Optional[List[Dict[str, Any]]] = None,
        traffic_enabled: bool = True
    ) -> Dict[str, Any]:
        """
        Enriches road matrices with real-time dynamic traffic congestion and incidents.
        Computes:
        - traffic_duration_matrix (seconds)
        - traffic_fuel_matrix (fuel score combining distance, road speed, and traffic idling)
        - traffic_overlay_features (GeoJSON for map rendering)
        - traffic_summary
        """
        n = len(coordinates)
        base_factor, period_name, period_desc = self.get_time_of_day_congestion_factor()
        
        if not traffic_enabled:
            base_factor = 1.0
            period_name = "Traffic Simulation Disabled"
            period_desc = "Using standard free-flow road conditions."

        traffic_durations: List[List[float]] = []
        traffic_fuel_matrix: List[List[float]] = []
        overlay_segments: List[Dict[str, Any]] = []

        active_incidents = incidents or []
        congestion_sum = 0.0
        segment_count = 0

        for i in range(n):
            dur_row: List[float] = []
            fuel_row: List[float] = []
            lat1, lon1 = coordinates[i]

            for j in range(n):
                if i == j:
                    dur_row.append(0.0)
                    fuel_row.append(0.0)
                    continue

                lat2, lon2 = coordinates[j]
                base_dist_m = distance_matrix[i][j]
                base_dur_s = duration_matrix[i][j]

                # Calculate mid-point of road connection
                mid_lat = (lat1 + lat2) / 2.0
                mid_lon = (lon1 + lon2) / 2.0

                # 1. Base time-of-day factor
                seg_factor = base_factor

                # 2. Hotspot Proximity Impact
                for spot in PUNE_TRAFFIC_HOTSPOTS:
                    d = haversine_m(mid_lat, mid_lon, spot["lat"], spot["lon"])
                    if d < spot["radius_m"]:
                        proximity_ratio = 1.0 - (d / spot["radius_m"])
                        seg_factor += (spot["base_congestion"] - 1.0) * proximity_ratio

                # 3. Dynamic Reported Incidents (Road closure, waterlogging, severe jam)
                for inc in active_incidents:
                    if inc.get("active") and inc.get("lat") and inc.get("lon"):
                        d_inc = haversine_m(mid_lat, mid_lon, inc["lat"], inc["lon"])
                        if inc.get("incident_type") == "road_closure":
                            if d_inc < 600:
                                seg_factor *= 10.0  # severely penalize / virtually block
                        elif inc.get("incident_type") == "traffic_jam":
                            if d_inc < 800:
                                seg_factor *= 2.8

                # Cap max factor
                seg_factor = max(1.0, min(seg_factor, 12.0))
                adjusted_dur_s = base_dur_s * seg_factor
                dur_row.append(adjusted_dur_s)

                # Fuel Consumption Model for this arc:
                # Fuel (liters) = Base Cruise Fuel + Traffic Delay Idle Fuel
                # Cruise: ~0.40 L/km for urban refuse truck
                # Idling / Stop-and-go delay: Refuse truck engine burns ~2.4 Liters/hour idling in traffic
                delay_hours = max(0.0, (adjusted_dur_s - base_dur_s) / 3600.0)
                cruise_fuel = (base_dist_m / 1000.0) * 0.40
                traffic_idle_fuel = delay_hours * 2.40
                total_arc_fuel = cruise_fuel + traffic_idle_fuel
                
                # Scaled for solver cost callback
                fuel_row.append(total_arc_fuel)

                congestion_sum += seg_factor
                segment_count += 1

                # Add sample overlay segments for visual map layer (limit count to avoid overwhelming map)
                if i < 15 and j < 15 and i < j and base_dist_m < 8000:
                    level = "smooth"
                    color = "#10b981"
                    if seg_factor >= 1.6:
                        level = "heavy"
                        color = "#f43f5e"
                    elif seg_factor >= 1.25:
                        level = "moderate"
                        color = "#f59e0b"

                    overlay_segments.append({
                        "from_coord": [lat1, lon1],
                        "to_coord": [lat2, lon2],
                        "congestion_factor": round(seg_factor, 2),
                        "level": level,
                        "color": color
                    })

            traffic_durations.append(dur_row)
            traffic_fuel_matrix.append(fuel_row)

        avg_congestion = round(congestion_sum / segment_count, 2) if segment_count > 0 else 1.0

        return {
            "traffic_duration_matrix": traffic_durations,
            "traffic_fuel_matrix": traffic_fuel_matrix,
            "overlay_segments": overlay_segments,
            "summary": {
                "period_name": period_name,
                "period_desc": period_desc,
                "current_time": datetime.datetime.now().strftime("%H:%M:%S"),
                "avg_congestion_index": avg_congestion,
                "active_incidents_count": len([i for i in active_incidents if i.get("active")]),
                "traffic_enabled": traffic_enabled
            }
        }

traffic_engine = RealTimeTrafficEngine()
