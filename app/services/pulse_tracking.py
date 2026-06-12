import time
from datetime import datetime

from app.db.session import SessionLocal
from app.models.models import HistoricoOperacao, VendaPagamento


FINAL_PULSE_STATUSES = {
    "liberado",
    "falha",
    "falha_timeout",
    "falha_publicacao",
    "falha_cmd_ignorado",
    "falha_bloqueado",
}


def update_pulse_status(command_id: str | None, status: str) -> None:
    if not command_id:
        return
    db = SessionLocal()
    try:
        historicos = db.query(HistoricoOperacao).filter(HistoricoOperacao.command_id == command_id).all()
        vendas = db.query(VendaPagamento).filter(VendaPagamento.command_id == command_id).all()
        for item in historicos:
            item.pulse_status = status
        for item in vendas:
            item.status_pulso = status
        db.commit()
    finally:
        db.close()


def wait_for_pulse_confirmation(command_id: str, timeout_seconds: float = 8.0, poll_seconds: float = 0.25) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_status = "comando_enviado"
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            item = db.query(HistoricoOperacao).filter(HistoricoOperacao.command_id == command_id).first()
            if item and item.pulse_status:
                last_status = item.pulse_status
                if item.pulse_status in FINAL_PULSE_STATUSES:
                    return item.pulse_status
        finally:
            db.close()
        time.sleep(poll_seconds)
    update_pulse_status(command_id, "falha_timeout")
    return "falha_timeout"


def device_event_description(status: str, command_id: str | None = None, detail: str | None = None) -> str:
    parts = [f"status={status}"]
    if command_id:
        parts.append(f"cmd={command_id}")
    if detail:
        parts.append(detail)
    return "Evento ESP: " + " ".join(parts)
