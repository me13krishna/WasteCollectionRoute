import math
import logging
import httpx
from typing import List, Tuple, Dict, Any, Optional
from backend.config import settings

logger = logging.getLogger(__name__)

# Haversine formula for spherical distance (meters)
def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

class MatrixCache:
    _cache: Dict[str, Tuple[List[List[float]], List[List[float]]]] = {}

    @classmethod
    def get_key(cls, coords: List[Tuple[float, float]]) -> str:
        return ";".join([f"{lat:.5f},{lon:.5f}" for lat, lon in coords])

    @classmethod
    def get(cls, key: str):
        return cls._cache.get(key)

    @classmethod
    def set(cls, key: str, value: Tuple[List[List[float]], List[List[float]]]):
        cls._cache[key] = value

class RoutingMatrixService:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.OSRM_BASE_URL).rstrip("/")

    async def get_distance_and_duration_matrix(
        self, coordinates: List[Tuple[float, float]]
    ) -> Tuple[List[List[float]], List[List[float]]]:
        """
        Coordinates format: [(lat, lon), (lat, lon), ...]
        Returns:
            distances: 2D matrix in meters
            durations: 2D matrix in seconds
        """
        n = len(coordinates)
        if n == 0:
            return [], []

        cache_key = MatrixCache.get_key(coordinates)
        cached = MatrixCache.get(cache_key)
        if cached:
            return cached

        # Try fetching from OSRM table service
        try:
            # OSRM expects lon,lat;lon,lat;...
            coords_str = ";".join([f"{lon:.6f},{lat:.6f}" for lat, lon in coordinates])
            url = f"{self.base_url}/table/v1/driving/{coords_str}?annotations=distance,duration"
            
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == "Ok":
                        distances = data.get("distances", [])
                        durations = data.get("durations", [])
                        if distances and durations and len(distances) == n:
                            # Sanitize nulls if any
                            clean_dist = [[d if d is not None else 0.0 for d in row] for row in distances]
                            clean_dur = [[d if d is not None else 0.0 for d in row] for row in durations]
                            MatrixCache.set(cache_key, (clean_dist, clean_dur))
                            return clean_dist, clean_dur
        except Exception as e:
            logger.warning(f"OSRM Table service request failed: {e}. Falling back to road-network topology simulation.")

        # Road-Network Fallback Approximation:
        # Urban collection routes typically exhibit a circuity factor of 1.25 - 1.40 over Haversine
        # with an average collection truck speed of 25-30 km/h (approx 7.5 m/s) in municipal streets.
        circuity_factor = 1.32
        avg_speed_mps = 7.5  # 27 km/h

        distances: List[List[float]] = []
        durations: List[List[float]] = []

        for i in range(n):
            dist_row: List[float] = []
            dur_row: List[float] = []
            lat1, lon1 = coordinates[i]
            for j in range(n):
                if i == j:
                    dist_row.append(0.0)
                    dur_row.append(0.0)
                else:
                    lat2, lon2 = coordinates[j]
                    direct_m = haversine_distance(lat1, lon1, lat2, lon2)
                    road_m = direct_m * circuity_factor
                    dur_s = road_m / avg_speed_mps
                    dist_row.append(road_m)
                    dur_row.append(dur_s)
            distances.append(dist_row)
            durations.append(dur_row)

        MatrixCache.set(cache_key, (distances, durations))
        return distances, durations

    async def get_route_geometry(
        self, coordinates: List[Tuple[float, float]]
    ) -> List[List[float]]:
        """
        Given ordered list of (lat, lon), fetch full route geometry (list of [lon, lat])
        from OSRM /route service, or generate realistic road-following polyline.
        """
        if len(coordinates) < 2:
            return [[lon, lat] for lat, lon in coordinates]

        try:
            # Chunking if route is large
            coords_str = ";".join([f"{lon:.6f},{lat:.6f}" for lat, lon in coordinates])
            url = f"{self.base_url}/route/v1/driving/{coords_str}?overview=full&geometries=geojson"

            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == "Ok" and data.get("routes"):
                        geometry = data["routes"][0]["geometry"]["coordinates"]
                        return geometry
        except Exception as e:
            logger.warning(f"OSRM Route geometry service failed: {e}. Generating curved road path.")

        # Road curvature interpolation fallback:
        # Generates intermediate points conforming to urban street grid
        result: List[List[float]] = []
        for i in range(len(coordinates) - 1):
            lat1, lon1 = coordinates[i]
            lat2, lon2 = coordinates[i+1]
            result.append([lon1, lat1])
            # Add intermediate street grid elbow for road realism
            mid_lon = lon2
            mid_lat = lat1
            result.append([mid_lon, mid_lat])
        last_lat, last_lon = coordinates[-1]
        result.append([last_lon, last_lat])
        return result

routing_service = RoutingMatrixService()
