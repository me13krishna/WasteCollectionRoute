import uvicorn
import sys
import os

# Add root directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.main import app

if __name__ == "__main__":
    print("\n" + "="*70)
    print(" EcoRoute AI - Municipal Waste Collection Route Optimization Platform")
    print(" Software-Only | Google OR-Tools CVRP | OSRM Road Networks | Leaflet")
    print("="*70)
    print(" Starting server at http://127.0.0.1:8000 ...")
    print(" Open http://127.0.0.1:8000 in your browser to view the interactive demo.")
    print("="*70 + "\n")
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
