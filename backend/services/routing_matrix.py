import os
import json
import time
import math
import hashlib
import logging
import asyncio
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
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c

class DiskCache:
    """
    Persistent file-backed cache for routing matrices and route geometries.
    Prevents redundant calls to OSRM and provides instant offline routing.
    """
    # Read-only cache shipped with the repo (road shapes and matrices for the ten ready-made
    # scenarios), so a fresh clone is instant and works offline. New results go to cache_dir.
    SEED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "precomputed_cache")

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create cache directory {cache_dir}: {e}")

    def _get_path(self, key: str, prefix: str) -> str:
        hashed = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, f"{prefix}_{hashed}.json")

    def get(self, key: str, prefix: str = "matrix") -> Optional[Any]:
        filepath = self._get_path(key, prefix)
        for path in (filepath, os.path.join(self.SEED_DIR, os.path.basename(filepath))):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    logger.debug(f"Cache read error for {path}: {e}")
        return None

    def set(self, key: str, value: Any, prefix: str = "matrix"):
        filepath = self._get_path(key, prefix)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(value, f)
        except Exception as e:
            logger.debug(f"Cache write error for {filepath}: {e}")

class MatrixCache:
    _mem_cache: Dict[str, Tuple[List[List[float]], List[List[float]]]] = {}
    _disk_cache: DiskCache = DiskCache(settings.CACHE_DIR)

    @classmethod
    def get_key(cls, coords: List[Tuple[float, float]]) -> str:
        return ";".join([f"{lat:.5f},{lon:.5f}" for lat, lon in coords])

    @classmethod
    def get(cls, key: str) -> Optional[Tuple[List[List[float]], List[List[float]]]]:
        if key in cls._mem_cache:
            return cls._mem_cache[key]
        disk_val = cls._disk_cache.get(key, prefix="matrix")
        if disk_val and "distances" in disk_val and "durations" in disk_val:
            val = (disk_val["distances"], disk_val["durations"])
            cls._mem_cache[key] = val
            return val
        return None

    @classmethod
    def set(cls, key: str, value: Tuple[List[List[float]], List[List[float]]]):
        cls._mem_cache[key] = value
        cls._disk_cache.set(key, {"distances": value[0], "durations": value[1]}, prefix="matrix")

class RoutingMatrixService:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.OSRM_BASE_URL).rstrip("/")
        self._service_reachable: bool = True
        self._last_checked: float = 0.0
        self._fallback_mode: bool = False
        self._disk_cache = DiskCache(settings.CACHE_DIR)

    @property
    def fallback_mode(self) -> bool:
        return self._fallback_mode

    @property
    def status_summary(self) -> Dict[str, Any]:
        return {
            "osrm_base_url": self.base_url,
            "fallback_mode": self._fallback_mode,
            "provider": "Haversine Urban Road Network (1.32x circuity fallback)" if self._fallback_mode else "Live OSRM Engine",
            "circuity_factor": settings.CIRCUITY_FACTOR,
            "truck_speed_kmh": settings.TRUCK_AVG_SPEED_KMH,
        }

    async def _fetch_osrm_table(
        self, coordinates: List[Tuple[float, float]]
    ) -> Optional[Tuple[List[List[float]], List[List[float]]]]:
        """
        Attempts to fetch distance and duration matrices from OSRM table service with retry and timeout.
        """
        coords_str = ";".join([f"{lon:.6f},{lat:.6f}" for lat, lon in coordinates])
        url = f"{self.base_url}/table/v1/driving/{coords_str}?annotations=distance,duration"
        
        timeout = settings.OSRM_TIMEOUT_SEC
        retries = settings.OSRM_MAX_RETRIES

        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("code") == "Ok":
                            distances = data.get("distances", [])
                            durations = data.get("durations", [])
                            n = len(coordinates)
                            if distances and durations and len(distances) == n:
                                clean_dist = [[float(d) if d is not None else 0.0 for d in row] for row in distances]
                                clean_dur = [[float(d) if d is not None else 0.0 for d in row] for row in durations]
                                return clean_dist, clean_dur
                    elif resp.status_code == 429:
                        logger.warning(f"OSRM rate limit (429) hit. Backing off...")
                        await asyncio.sleep(0.5 * (attempt + 1))
            except Exception as e:
                logger.debug(f"OSRM table request failed (attempt {attempt + 1}/{retries + 1}): {e}")
                if attempt < retries:
                    await asyncio.sleep(0.3 * (attempt + 1))

        return None

    def _compute_haversine_matrix(
        self, coordinates: List[Tuple[float, float]]
    ) -> Tuple[List[List[float]], List[List[float]]]:
        """
        Fallback road-network approximation using Haversine distance with calibrated
        urban circuity factor (1.32) and municipal truck speed (7.5 m/s = 27 km/h).
        """
        n = len(coordinates)
        circuity = settings.CIRCUITY_FACTOR
        avg_speed_mps = (settings.TRUCK_AVG_SPEED_KMH * 1000.0) / 3600.0  # 7.5 m/s

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
                    road_m = direct_m * circuity
                    dur_s = road_m / avg_speed_mps
                    dist_row.append(round(road_m, 1))
                    dur_row.append(round(dur_s, 1))
            distances.append(dist_row)
            durations.append(dur_row)

        return distances, durations

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
        if n == 1:
            return [[0.0]], [[0.0]]

        cache_key = MatrixCache.get_key(coordinates)
        cached = MatrixCache.get(cache_key)
        if cached:
            return cached

        # Check OSRM if batch size allows (OSRM public server rejects > 100 coordinates in URL)
        now = time.time()
        should_try_osrm = (
            n <= settings.OSRM_BATCH_SIZE
            and (self._service_reachable or (now - self._last_checked > 60.0))
        )

        if should_try_osrm:
            osrm_res = await self._fetch_osrm_table(coordinates)
            if osrm_res:
                self._service_reachable = True
                self._fallback_mode = False
                MatrixCache.set(cache_key, osrm_res)
                return osrm_res
            else:
                self._service_reachable = False
                self._last_checked = now
                logger.info("OSRM table unreachable or batch too large; engaging high-precision circuity fallback.")

        # Fallback matrix
        self._fallback_mode = True
        fallback_res = self._compute_haversine_matrix(coordinates)
        MatrixCache.set(cache_key, fallback_res)
        return fallback_res

    async def _osrm_route_chunk(self, coordinates: List[Tuple[float, float]]) -> Optional[List[List[float]]]:
        coords_str = ";".join([f"{lon:.6f},{lat:.6f}" for lat, lon in coordinates])
        url = f"{self.base_url}/route/v1/driving/{coords_str}?overview=full&geometries=geojson"
        try:
            async with httpx.AsyncClient(timeout=max(settings.OSRM_TIMEOUT_SEC, 6.0)) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == "Ok" and data.get("routes"):
                        return data["routes"][0]["geometry"]["coordinates"]
        except Exception as e:
            logger.debug(f"OSRM route geometry service unavailable: {e}")
        return None

    async def get_route_geometry(
        self, coordinates: List[Tuple[float, float]]
    ) -> List[List[float]]:
        """
        Given ordered list of (lat, lon), fetch full route geometry (list of [lon, lat])
        from OSRM /route service (long routes are fetched in overlapping chunks),
        or generate a street-grid polyline when OSRM is unavailable.
        """
        if len(coordinates) < 2:
            return [[lon, lat] for lat, lon in coordinates]

        # Key on every coordinate: routes that share endpoints and length must not collide.
        geom_cache_key = "geom_v2_" + ";".join(f"{lat:.5f},{lon:.5f}" for lat, lon in coordinates)
        cached_geom = self._disk_cache.get(geom_cache_key, prefix="geom")
        if cached_geom:
            return cached_geom

        if self._service_reachable:
            max_chunk = 40
            geometry: List[List[float]] = []
            ok = True
            start = 0
            while start < len(coordinates) - 1:
                end = min(start + max_chunk, len(coordinates))
                part = await self._osrm_route_chunk(coordinates[start:end])
                if part is None:
                    ok = False
                    break
                geometry.extend(part if not geometry else part[1:])
                start = end - 1
            if ok and geometry:
                self._disk_cache.set(geom_cache_key, geometry, prefix="geom")
                return geometry

        # Street-grid fallback: intermediate elbows for urban visual realism
        result: List[List[float]] = []
        for i in range(len(coordinates) - 1):
            lat1, lon1 = coordinates[i]
            lat2, lon2 = coordinates[i+1]
            result.append([lon1, lat1])
            result.append([lon2, lat1])
        last_lat, last_lon = coordinates[-1]
        result.append([last_lon, last_lat])
        return result

routing_service = RoutingMatrixService()
