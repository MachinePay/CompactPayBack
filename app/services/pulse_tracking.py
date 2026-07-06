import time
from datetime import datetime

from app.db.session import SessionLocal
from app.models.models import HistoricoOperacao, Maquina, VendaPagamento
from app.services.command_queue import update_command_from_pulse_status
from app.services.pagamentos_helpers import auto_refund_failed_pulse


FINAL_PULSE_STATUSES = {
    "pulso_confirmado",
    "falha",
    "falha_timeout",
    "falha_publicacao",
    "falha_cmd_ignorado",
    "falha_bloqueado",
    "falha_sem_confirmacao",
    "saldo_pendente",
}


def update_pulse_status(command_id: str | None, status: str) -> None:
    if not command_id:
        return
    update_command_from_pulse_status(command_id, status)
    db = SessionLocal()
    try:
        historicos = (
            db.query(HistoricoOperacao)
            .filter(
                HistoricoOperacao.command_id == command_id,
                HistoricoOperacao.categoria != "DISPOSITIVO",
            )
            .all()
        )
        vendas = db.query(VendaPagamento).filter(VendaPagamento.command_id == command_id).all()
        for item in historicos:
            item.pulse_status = status
            if status in {"falha", "falha_timeout", "falha_publicacao", "falha_cmd_ignorado", "falha_bloqueado", "falha_sem_confirmacao", "saldo_pendente", "pulso_sem_retorno"}:
                maquina = db.query(Maquina).filter(Maquina.id_hardware == item.maquina_id).first()
                auto_refund_failed_pulse(db, item, maquina=maquina)
        for item in vendas:
            item.status_pulso = status
        db.commit()
    finally:
        db.close()


def wait_for_pulse_confirmation(
    command_id: str,
    timeout_seconds: float = 8.0,
    poll_seconds: float = 0.25,
    expected_confirmations: int = 1,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_status = "comando_enviado"
    expected_confirmations = max(1, int(expected_confirmations or 1))
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            item = (
                db.query(HistoricoOperacao)
                .filter(
                    HistoricoOperacao.command_id == command_id,
                    HistoricoOperacao.categoria != "DISPOSITIVO",
                )
                .first()
            )
            if item and item.pulse_status:
                last_status = item.pulse_status
                if item.pulse_status == "pulso_confirmado":
                    confirmations = (
                        db.query(HistoricoOperacao)
                        .filter(
                            HistoricoOperacao.command_id == command_id,
                            HistoricoOperacao.categoria == "DISPOSITIVO",
                            HistoricoOperacao.pulse_status == "pulso_confirmado",
                        )
                        .count()
                    )
                    if confirmations >= expected_confirmations:
                        return item.pulse_status
                elif item.pulse_status in FINAL_PULSE_STATUSES:
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
