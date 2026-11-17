# Self-Hosted OSRM Setup Guide (Pune & Maharashtra)

EcoRoute AI is designed to run **100% zero-friction out of the box** using its built-in urban road-network topology simulation (`circuity factor = 1.32x Haversine`) or the public OpenStreetMap OSRM demo server (`https://router.project-osrm.org`).

If you wish to run a dedicated, high-throughput offline OSRM routing container for Maharashtra/Pune, follow this quick guide.

---

## 1. Download OpenStreetMap PBF Extract

Download the latest Maharashtra PBF extract from Geofabrik:

```bash
mkdir -p osrm-data
cd osrm-data
curl -O https://download.geofabrik.de/asia/india/southern-zone-latest.osm.pbf
# Or download directly from Geofabrik Southern Zone / Maharashtra mirror
```

---

## 2. Extract Road Network with Car Profile

Run the official OSRM Docker container to extract and build the Multi-Level Dijkstra (MLD) routing graphs:

```bash
# 1. Extract road network using truck/car profile
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend:latest osrm-extract -p /opt/car.lua /data/southern-zone-latest.osm.pbf

# 2. Partition the graph into cells
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend:latest osrm-partition /data/southern-zone-latest.osrm

# 3. Customize cell weights and turn penalties
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend:latest osrm-customize /data/southern-zone-latest.osrm

# Rename to pune.osrm for docker-compose compatibility
mv southern-zone-latest.osrm pune.osrm
```

---

## 3. Launch with Docker Compose

Start the self-hosted OSRM container using the Docker Compose profile:

```bash
docker compose --profile self-hosted-osrm up -d
```

The OSRM service will listen on port `5000` (`http://localhost:5000`). Update your `.env` or `docker-compose.yml`:

```env
OSRM_BASE_URL="http://osrm:5000"
```
