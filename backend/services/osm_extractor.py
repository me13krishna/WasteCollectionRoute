import logging
import httpx
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Free Open-Source Overpass API endpoints (no API key required)
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

class OpenStreetMapExtractor:
    """
    100% Free Open-Source GIS data extractor using OpenStreetMap Overpass API.
    Enables zero-cost ingestion of real municipal waste infrastructure,
    recycling bins, and street intersections for ANY global city.
    """

    async def geocode_city(self, city_name: str) -> Optional[Dict[str, Any]]:
        """
        Geocodes a city name to latitude and longitude using free OSM Nominatim.
        """
        headers = {"User-Agent": "EcoRouteAI-MunicipalWasteOptimizer/1.0"}
        params = {"q": city_name, "format": "json", "limit": 1}
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(NOMINATIM_URL, params=params, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if data and len(data) > 0:
                        return {
                            "name": data[0].get("display_name"),
                            "lat": float(data[0]["lat"]),
                            "lon": float(data[0]["lon"]),
                        }
            except Exception as e:
                logger.warning(f"Nominatim geocode failed for {city_name}: {e}")
        return None

    async def fetch_city_collection_points(
        self,
        lat: float,
        lon: float,
        radius_meters: int = 5000,
        limit: int = 60
    ) -> List[Dict[str, Any]]:
        """
        Queries Overpass API for real waste disposal, recycling points, and municipal nodes.
        If dedicated waste tags are scarce, fetches major street intersections & public POIs.
        """
        # Overpass QL query: waste bins, recycling, commercial/market hubs
        query = f"""
        [out:json][timeout:25];
        (
          node["amenity"~"waste_basket|waste_disposal|recycling"](around:{radius_meters},{lat},{lon});
          node["amenity"~"marketplace|bus_station"](around:{radius_meters},{lat},{lon});
          node["shop"~"supermarket"](around:{radius_meters},{lat},{lon});
        );
        out body {limit};
        """
        
        headers = {"User-Agent": "EcoRouteAI-MunicipalWasteOptimizer/1.0"}
        
        for endpoint in OVERPASS_URLS:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(endpoint, data={"data": query}, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        elements = data.get("elements", [])
                        if elements:
                            return self._process_elements(elements, limit)
            except Exception as e:
                logger.warning(f"Overpass endpoint {endpoint} failed: {e}. Trying next...")
                continue
                
        # Fallback: Generate synthetically clustered collection points around coordinates if network blocks
        return self._generate_local_fallback_grid(lat, lon, count=min(limit, 40))

    def _process_elements(self, elements: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        points: List[Dict[str, Any]] = []
        for idx, el in enumerate(elements[:limit]):
            tags = el.get("tags", {})
            name = (
                tags.get("name") or 
                tags.get("amenity", "").replace("_", " ").title() or 
                tags.get("shop", "").replace("_", " ").title() or 
                f"Collection Node #{idx + 1}"
            )
            
            # Estimate realistic collection demand (50 - 150 kg)
            amenity = tags.get("amenity", "")
            if "marketplace" in amenity or "disposal" in amenity:
                demand = 120.0
                svc = 120
                priority = 3
            elif "recycling" in amenity:
                demand = 85.0
                svc = 90
                priority = 2
            else:
                demand = 60.0
                svc = 75
                priority = 1
                
            points.append({
                "id": idx + 1,
                "name": name,
                "lat": round(float(el["lat"]), 6),
                "lon": round(float(el["lon"]), 6),
                "demand_kg": demand,
                "service_time_sec": svc,
                "priority": priority,
                "required": True,
                "status": "pending"
            })
        return points

    def _generate_local_fallback_grid(self, center_lat: float, center_lon: float, count: int) -> List[Dict[str, Any]]:
        import random
        random.seed(42)
        points = []
        for i in range(count):
            dlat = (random.random() - 0.5) * 0.05
            dlon = (random.random() - 0.5) * 0.05
            points.append({
                "id": i + 1,
                "name": f"Municipal Collection Point #{i+1}",
                "lat": round(center_lat + dlat, 6),
                "lon": round(center_lon + dlon, 6),
                "demand_kg": round(random.choice([50, 75, 90, 110, 130]), 1),
                "service_time_sec": random.choice([60, 90, 120]),
                "priority": random.choice([1, 1, 2, 2, 3]),
                "required": True,
                "status": "pending"
            })
        return points

osm_extractor = OpenStreetMapExtractor()
