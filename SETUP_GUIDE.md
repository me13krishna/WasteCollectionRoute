# Run EcoRoute on a new machine

Written so a person or a coding agent can follow it top to bottom. Every step has a check.
No API keys, no database server and no internet are required to run the demo.

## 1. Requirements

- Python **3.10 to 3.13** (3.11 recommended). OR-Tools publishes wheels for these; if `pip install`
  fails on `ortools`, check `python --version` first.
- `git`. A browser. Nothing else.

## 2. Get the code and install

```bash
git clone https://github.com/me13krishna/WasteCollectionRoute.git
cd WasteCollectionRoute

python3 -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
```

Check: `python -c "import fastapi, ortools; print('ok')"` prints `ok`.

## 3. Start the server

```bash
python main.py
```

(equivalent: `python -m uvicorn backend.main:app --port 8000`)

Check: the console shows `Uvicorn running on http://127.0.0.1:8000`, and
`curl http://127.0.0.1:8000/api/v1/scenarios` returns JSON listing 10 scenarios.

Open **http://127.0.0.1:8000** in a browser. If the page is stale after an update, hard-refresh
(Ctrl+Shift+R, or Cmd+Shift+R on Mac).

Docker alternative: `docker build -t ecoroute . && docker run -p 8000:8000 ecoroute`.

## 4. Use the main feature (to confirm it works)

1. **Plan** tab, set **Layout** to any scenario, for example `3. Kothrud and Karve Road`, then
   click **Generate points**.
2. Click **Optimize routes**.
3. Roads fill in on the map one truck count at a time (1, 2, 3, ...), and a bar chart builds up
   underneath. The green bar is the optimal number of trucks, and every scenario has a different
   optimum and a U-shaped curve.
4. Click any bar to replay the animated routes for that many trucks.

Distances in the chart are illustrative demo values (see `backend/services/fleet_model.py`). The
routes drawn use the real points and real roads.

## 5. Verify with tests (optional)

```bash
PYTHONPATH=. python -m pytest tests -q          # Windows PowerShell: $env:PYTHONPATH="."
```

Expected: all tests pass (about 1 minute).

## Offline and caching

- `backend/data/precomputed_cache/` ships road shapes and distance matrices for all ten ready-made
  scenarios, so they load instantly and work with no internet.
- Anything new (a random layout, an imported city) is fetched from the public OSRM server
  (`router.project-osrm.org`) the first time, taking about 5 to 15 seconds, then saved to `.cache/`
  (git-ignored). If OSRM is unreachable the app falls back to straight street-grid estimates, so it
  never hard-fails.
- To use your own OSRM server: `OSRM_BASE_URL=http://localhost:5000` (see `docs/OSRM_SETUP.md`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: backend` | Run commands from the repo root; set `PYTHONPATH=.` for pytest. |
| `ortools` fails to install | Use Python 3.10 to 3.13; upgrade pip (`pip install -U pip`). |
| Port 8000 already in use | `python -m uvicorn backend.main:app --port 8010`, then open that port. |
| Blank map tiles | The base map comes from Esri over the internet; routes and charts still work offline. |
| Old UI after pulling changes | Hard-refresh the browser; restart the server. |
| "No collection points loaded" | Click **Generate points** on the Plan tab first. |

## Where things live

- `backend/main.py`: API. The streaming endpoint is `POST /api/v1/optimize/fleet-sweep/stream`.
- `backend/services/fleet_model.py`: the per-scenario U-curve profiles (edit `DEMO_PROFILES`).
- `backend/static/js/fleet.js`: streams the plans, animates the map, and handles bar clicks.
- `backend/static/js/map.js`: the route fill-in animation (`fleetPlay`).
- State (points, incidents) is held in memory per server process, so run a single server instance.
