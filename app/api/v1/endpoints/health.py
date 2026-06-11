from datetime import datetime

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "compactpay-backend",
        "timestamp": datetime.utcnow().isoformat(),
    }
