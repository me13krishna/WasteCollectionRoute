import datetime
import math
import logging
from typing import List, Tuple, Dict, Any, Optional
from backend.config import settings

logger = logging.getLogger(__name__)

# Known real-world high-density traffic bottleneck corridors in Pune, Maharashtra
PUNE_TRAFFIC_HOTSPOTS = [
    {"name": "Shivajinagar & Sancheti Chowk", "lat": 18.5305, "lon": 73.8505, "base_congestion": 1.45, "radius_m": 800},
    {"name": "Swargate ST Bus Stand & Chowk", "lat": 18.4990, "lon": 73.8570, "base_congestion": 1.50, "radius_m": 900},
    {"name": "Laxmi Road & Tulshibaug Bazaar Core", "lat": 18.5165, "lon": 73.8530, "base_congestion": 1.65, "radius_m": 700},
    {"name": "Deccan Gymkhana & Goodluck Chowk", "lat": 18.5175, "lon": 73.8415, "base_congestion": 1.40, "radius_m": 600},
    {"name": "Karve Road Nal Stop Concourse", "lat": 18.5065, "lon": 73.8320, "base_congestion": 1.45, "radius_m": 750},
    {"name": "Hadapsar Gadital Junction", "lat": 18.5020, "lon": 73.9280, "base_congestion": 1.55, "radius_m": 1000},
    {"name": "Savitribai Phule Pune University Circle", "lat": 18.5330, "lon": 73.8310, "base_congestion": 1.35, "radius_m": 800},
    {"name": "Market Yard Gultekdi Hub", "lat": 18.4870, "lon": 73.8670, "base_congestion": 1.50, "radius_m": 800},
]

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

def point_to_segment_distance_m(p_lat: float, p_lon: float, a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Calculates perpendicular shortest distance from point P to line segment AB in meters."""
    dx = (b_lon - a_lon) * math.cos(math.radians((a_lat + b_lat) / 2.0))
    dy = b_lat - a_lat
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return haversine_m(p_lat, p_lon, a_lat, a_lon)
    px = (p_lon - a_lon) * math.cos(math.radians((a_lat + b_lat) / 2.0))
    py = p_lat - a_lat
    t = max(0.0, min(1.0, (px * dx + py * dy) / seg_len_sq))
    proj_lat = a_lat + t * dy
    proj_lon = a_lon + t * (b_lon - a_lon)
    return haversine_m(p_lat, p_lon, proj_lat, proj_lon)

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

        # 10:30 - 16:30: Mid-Day Commercial Operations
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
        - traffic_distance_matrix (meters with penalties for road closures)
        - traffic_duration_matrix (seconds with speed adjustments and incident delays)
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

        traffic_distances: List[List[float]] = []
        traffic_durations: List[List[float]] = []
        traffic_fuel_matrix: List[List[float]] = []
        overlay_segments: List[Dict[str, Any]] = []

        active_incidents = [inc for inc in (incidents or []) if inc.get("active", True)]
        congestion_sum = 0.0
        segment_count = 0

        for i in range(n):
            dist_row: List[float] = []
            dur_row: List[float] = []
            fuel_row: List[float] = []
            lat1, lon1 = coordinates[i]

            for j in range(n):
                if i == j:
                    dist_row.append(0.0)
                    dur_row.append(0.0)
                    fuel_row.append(0.0)
                    continue

                lat2, lon2 = coordinates[j]
                base_dist_m = distance_matrix[i][j]
                base_dur_s = duration_matrix[i][j]

                # 1. Base time-of-day factor
                seg_factor = base_factor

                # 2. Hotspot Proximity Impact
                mid_lat = (lat1 + lat2) / 2.0
                mid_lon = (lon1 + lon2) / 2.0
                for spot in PUNE_TRAFFIC_HOTSPOTS:
                    d = point_to_segment_distance_m(spot["lat"], spot["lon"], lat1, lon1, lat2, lon2)
                    if d < spot["radius_m"]:
                        proximity_ratio = 1.0 - (d / spot["radius_m"])
                        seg_factor += (spot["base_congestion"] - 1.0) * proximity_ratio

                # 3. Dynamic Reported Incidents (Road closure, traffic jam)
                road_closed = False
                for inc in active_incidents:
                    if inc.get("lat") and inc.get("lon"):
                        inc_lat, inc_lon = float(inc["lat"]), float(inc["lon"])
                        d_inc = point_to_segment_distance_m(inc_lat, inc_lon, lat1, lon1, lat2, lon2)
                        
                        if inc.get("incident_type") == "road_closure" and d_inc < 350:
                            road_closed = True
                            seg_factor *= 30.0  # Massive penalty: solver will reroute around this segment
                        elif inc.get("incident_type") == "traffic_jam" and d_inc < 500:
                            seg_factor *= 3.0

                # Cap max factor unless road is explicitly closed
                if not road_closed:
                    seg_factor = max(1.0, min(seg_factor, 15.0))

                adjusted_dur_s = base_dur_s * seg_factor
                adjusted_dist_m = base_dist_m * (5.0 if road_closed else 1.0)

                dist_row.append(adjusted_dist_m)
                dur_row.append(adjusted_dur_s)

                # Fuel Consumption Model for this arc:
                # Cruise: 0.40 L/km for urban refuse truck
                # Idling / Stop-and-go delay: Refuse truck engine burns 2.4 Liters/hour idling in traffic
                delay_hours = max(0.0, (adjusted_dur_s - base_dur_s) / 3600.0)
                cruise_fuel = (adjusted_dist_m / 1000.0) * settings.FUEL_L_PER_KM
                traffic_idle_fuel = delay_hours * settings.TRUCK_IDLE_FUEL_L_PER_HR
                total_arc_fuel = cruise_fuel + traffic_idle_fuel
                fuel_row.append(total_arc_fuel)

                congestion_sum += seg_factor
                segment_count += 1

                # Visual overlay segment for map representation
                if i < 20 and j < 20 and i < j and base_dist_m < 8000:
                    level = "smooth"
                    color = "#10b981"
                    if road_closed:
                        level = "closed"
                        color = "#dc2626"
                    elif seg_factor >= 1.6:
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

            traffic_distances.append(dist_row)
            traffic_durations.append(dur_row)
            traffic_fuel_matrix.append(fuel_row)

        avg_congestion = round(congestion_sum / segment_count, 2) if segment_count > 0 else 1.0

        return {
            "traffic_distance_matrix": traffic_distances,
            "traffic_duration_matrix": traffic_durations,
            "traffic_fuel_matrix": traffic_fuel_matrix,
            "overlay_segments": overlay_segments,
            "summary": {
                "period_name": period_name,
                "period_desc": period_desc,
                "current_time": datetime.datetime.now().strftime("%H:%M:%S"),
                "avg_congestion_index": avg_congestion,
                "active_incidents_count": len(active_incidents),
                "traffic_enabled": traffic_enabled
            }
        }

traffic_engine = RealTimeTrafficEngine()
