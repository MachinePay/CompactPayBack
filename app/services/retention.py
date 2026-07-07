import logging
import threading
import time
from datetime import datetime, timedelta

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.models import HistoricoOperacao

RETENTION_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
DEVICE_STATUS_CATEGORY = "DISPOSITIVO"


def purge_old_device_status_history() -> int:
    cutoff = datetime.utcnow() - timedelta(days=settings.DEVICE_STATUS_RETENTION_DAYS)
    db = SessionLocal()
    try:
        deleted = (
            db.query(HistoricoOperacao)
            .filter(
                HistoricoOperacao.categoria == DEVICE_STATUS_CATEGORY,
                HistoricoOperacao.created_at < cutoff,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return deleted
    finally:
        db.close()


def run_retention_worker() -> None:
    logging.info(
        "Retention worker iniciado (status de dispositivo mantido por %s dias; transacoes nunca sao apagadas)",
        settings.DEVICE_STATUS_RETENTION_DAYS,
    )
    while True:
        try:
            deleted = purge_old_device_status_history()
            if deleted:
                logging.info("Retention worker removeu %s registros antigos de status de dispositivo", deleted)
        except Exception:
            logging.exception("Erro no retention worker")
        time.sleep(RETENTION_CHECK_INTERVAL_SECONDS)


def start_retention_worker() -> threading.Thread | None:
    if not settings.START_RETENTION_WORKER:
        logging.info("Retention worker desativado por START_RETENTION_WORKER=false")
        return None
    thread = threading.Thread(target=run_retention_worker, daemon=True)
    thread.start()
    return thread
