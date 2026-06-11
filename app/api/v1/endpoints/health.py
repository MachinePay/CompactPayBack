from datetime import datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SessionLocal

router = APIRouter()


@router.get("/health")
def health_check():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "degraded",
                "service": "compactpay-backend",
                "database": "unavailable",
                "timestamp": datetime.utcnow().isoformat(),
            },
        ) from exc
    finally:
        db.close()

    return {
        "status": "ok",
        "service": "compactpay-backend",
        "database": "ok",
        "timestamp": datetime.utcnow().isoformat(),
    }
