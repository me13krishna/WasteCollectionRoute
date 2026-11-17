import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Universal SQLite & PostgreSQL database setup (100% compatible with Python 3.11 - 3.14)
SYNC_DB_URL = os.getenv("DATABASE_URL", "sqlite:///./waste_route.db").replace("+aiosqlite", "").replace("+asyncpg", "")

engine = create_engine(
    SYNC_DB_URL,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in SYNC_DB_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    from backend.models import db_models  # ensure models are registered
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
