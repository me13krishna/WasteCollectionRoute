import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "EcoRoute AI - Municipal Waste Collection Route Optimization"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = os.getenv("JWT_SECRET", "super-secret-waste-route-key-2026-production")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    
    # Database: Default to SQLite for zero-friction local run; supports PostgreSQL/PostGIS when supplied
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./waste_route.db")
    
    # OSRM Routing Endpoint: Public demo server or local self-hosted container
    OSRM_BASE_URL: str = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org")
    OSRM_TIMEOUT_SEC: float = float(os.getenv("OSRM_TIMEOUT_SEC", "2.5"))
    OSRM_MAX_RETRIES: int = int(os.getenv("OSRM_MAX_RETRIES", "2"))
    OSRM_BATCH_SIZE: int = int(os.getenv("OSRM_BATCH_SIZE", "100"))  # public OSRM table limit is 100 coordinates
    
    # Truck-count trade-off model (editable in the Fleet size tab)
    TRUCK_FIXED_COST_PER_DAY: float = float(os.getenv("TRUCK_FIXED_COST_PER_DAY", "1000"))  # vehicle upkeep + helper, per truck dispatched
    DELAY_COST_PER_HOUR: float = float(os.getenv("DELAY_COST_PER_HOUR", "1500"))            # cost of each hour until the last truck is back (uncollected waste, complaints)

    # Fleet-size U-curve (illustrative planning constants, not measured data).
    # Door-to-door collection must be finished inside a morning window; every hour a truck runs past
    # it is charged as OVERRUN_KM_PER_HOUR km of "effective distance" (relief crew, hire, complaints).
    COLLECTION_WINDOW_MIN: float = float(os.getenv("COLLECTION_WINDOW_MIN", "120"))
    OVERRUN_KM_PER_HOUR: float = float(os.getenv("OVERRUN_KM_PER_HOUR", "60"))

    # Regional Currency & SI Units (Default: Pune, Maharashtra, India)
    CURRENCY_SYMBOL: str = os.getenv("CURRENCY_SYMBOL", "₹")
    CURRENCY_CODE: str = os.getenv("CURRENCY_CODE", "INR")
    DISTANCE_UNIT: str = "km"
    VOLUME_UNIT: str = "L"
    MASS_UNIT: str = "kg"
    
    # Logistical & Environmental Constants (EEA MEET Refuse Truck Model)
    FUEL_L_PER_KM: float = float(os.getenv("FUEL_L_PER_KM", "0.42"))
    CO2_KG_PER_L: float = float(os.getenv("CO2_KG_PER_L", "2.68"))      # 2.68 kg CO2 per L diesel
    FUEL_COST_PER_L: float = float(os.getenv("FUEL_COST_PER_L", "94.50")) # ₹94.50 per liter in Pune
    DRIVER_HOURLY_RATE: float = float(os.getenv("DRIVER_HOURLY_RATE", "250.00")) # ₹250.00/hr crew wage
    TRUCK_IDLE_FUEL_L_PER_HR: float = 2.40  # 2.4 L/hr idling in traffic
    COMPACTOR_PTO_L_PER_STOP: float = 0.035 # 0.035 L per bin lift/compaction cycle
    
    # Road network circuity fallback parameters
    CIRCUITY_FACTOR: float = 1.32           # Urban road detour factor over Haversine
    TRUCK_AVG_SPEED_KMH: float = 27.0       # Average municipal collection speed (7.5 m/s)
    
    # Default Solver Settings
    DEFAULT_OPT_TIME_LIMIT_SEC: int = int(os.getenv("DEFAULT_OPT_TIME_LIMIT_SEC", "15"))
    
    # Cache Directory
    CACHE_DIR: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")

    model_config = SettingsConfigDict(case_sensitive=True, extra="allow")

settings = Settings()
