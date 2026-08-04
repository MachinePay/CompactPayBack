from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=15,
    max_overflow=15,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
