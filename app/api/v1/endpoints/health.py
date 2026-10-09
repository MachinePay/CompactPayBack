from datetime import datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
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
                "version": settings.APP_VERSION,
                "revision": settings.APP_REVISION,
                "database": "unavailable",
                "timestamp": datetime.utcnow().isoformat(),
            },
        ) from exc
    finally:
        db.close()

    return {
        "status": "ok",
        "service": "compactpay-backend",
        "version": settings.APP_VERSION,
        "revision": settings.APP_REVISION,
        "database": "ok",
        "mqtt": _mqtt_worker_status(),
        "timestamp": datetime.utcnow().isoformat(),
    }


def _mqtt_worker_status() -> dict:
    """Se o worker MQTT esta ouvindo as placas. Responde sempre 200 (para nao
    fazer o health check do Render reiniciar o servico por causa da AWS), mas
    com "status": "connected"/"disconnected" - da pra monitorar com um
    servico externo que procure a palavra "connected"."""
    if not settings.START_MQTT_WORKER:
        return {"status": "disabled"}
    from app.services.mqtt_worker import MQTT_WORKER_STATE

    def _iso(value):
        return value.isoformat() if value else None

    return {
        "status": "connected" if MQTT_WORKER_STATE["connected"] else "disconnected",
        "connected_since": _iso(MQTT_WORKER_STATE["connected_since"]),
        "last_message_at": _iso(MQTT_WORKER_STATE["last_message_at"]),
        "last_error": MQTT_WORKER_STATE["last_error"],
        "connect_attempts": MQTT_WORKER_STATE["connect_attempts"],
    }


@router.get("/version")
def version():
    return {
        "service": "compactpay-backend",
        "version": settings.APP_VERSION,
        "revision": settings.APP_REVISION,
        "timestamp": datetime.utcnow().isoformat(),
    }
