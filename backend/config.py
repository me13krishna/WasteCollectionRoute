import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "EcoRoute AI - Municipal Waste Collection Route Optimization"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = os.getenv("JWT_SECRET", "super-secret-waste-route-key-2026-production")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    
    # Database: Default to asynchronous SQLite for zero-friction local run; supports PostgreSQL/PostGIS when supplied
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./waste_route.db")
    
    # OSRM Routing Endpoint: Defaults to standard OSRM engine or self-hosted container
    OSRM_BASE_URL: str = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org")
    
    # Route & Cost Estimation Factors (SI & standard logistical constants)
    FUEL_L_PER_KM: float = float(os.getenv("FUEL_L_PER_KM", "0.45"))   # 0.45 L/km for municipal collection truck
    CO2_KG_PER_L: float = float(os.getenv("CO2_KG_PER_L", "2.68"))     # 2.68 kg CO2 per L diesel
    FUEL_COST_PER_L: float = float(os.getenv("FUEL_COST_PER_L", "1.35")) # $1.35 per liter
    DRIVER_HOURLY_RATE: float = float(os.getenv("DRIVER_HOURLY_RATE", "22.50")) # $22.50/hr
    
    # Default Solver Settings
    DEFAULT_OPT_TIME_LIMIT_SEC: int = int(os.getenv("DEFAULT_OPT_TIME_LIMIT_SEC", "30"))
    
    class Config:
        case_sensitive = True
        extra = "allow"

settings = Settings()
