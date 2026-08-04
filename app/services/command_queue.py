import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import and_

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.models import ComandoMaquina

ACK_TIMEOUT_SECONDS = 4
RETRY_DELAY_SECONDS = 4
MAX_ATTEMPTS = 3

FINAL_COMMAND_STATUSES = {"executado", "falhou", "cancelado"}
RETRYABLE_STATUSES = {"pendente", "enviado", "aguardando_retry", "falha_publicacao"}

# Tipo de comando que libera credito fisico na maquina (ver mqtt_commands.py:
# publish_machine_credit usa tipo=action.lower(), e a acao usada em pagamentos
# e sempre "paid"). So esse tipo passa pela fila por maquina - ping/update nao
# disputam o mesmo recurso fisico (o relé de credito) e podem seguir direto.
CREDIT_COMMAND_TIPO = "paid"
QUEUED_STATUS = "na_fila"


def _now() -> datetime:
    return datetime.utcnow()


def _status_from_device_status(status: str) -> tuple[str | None, bool]:
    if status in {"CMD_RECEBIDO", "PONG"}:
        return "recebido", False
    if status in {"PULSO_INICIADO", "LIBERADO", "PULSO_CONFIRMADO", "UPDATE_INICIADO"}:
        return "executando", False
    if status in {"PULSOS_CONCLUIDOS", "PULSOS_ENVIADOS_SEM_RETORNO", "SALDO_PENDENTE", "UPDATE_OK", "UPDATE_SEM_NOVIDADE"}:
        return "executado", True
    if status in {"CMD_IGNORADO", "PULSO_BLOQUEADO_SEGURANCA", "PULSO_NAO_CONFIRMADO", "UPDATE_FALHOU"}:
        return "falhou", True
    return None, False


def _status_from_pulse_status(status: str) -> tuple[str | None, bool]:
    if status in {"comando_enviado"}:
        return "enviado", False
    if status in {"cmd_recebido", "cmd_duplicado"}:
        return "recebido", False
    if status in {"pulso_iniciado", "pulso_enviado", "pulso_unitario", "update_iniciado"}:
        return "executando", False
    if status in {"pulso_confirmado", "saldo_pendente", "update_ok", "update_sem_novidade"}:
        return "executado", True
    if status in {
        "falha",
        "falha_timeout",
        "falha_publicacao",
        "falha_cmd_ignorado",
        "falha_bloqueado",
        "falha_sem_confirmacao",
        "pulso_sem_retorno",
        "update_falhou",
    }:
        return "falhou", True
    return None, False


def _get_or_create_command(
    db,
    *,
    machine_id: str,
    command_id: str,
    tipo: str,
    topic: str,
    payload: str,
) -> ComandoMaquina:
    comando = db.query(ComandoMaquina).filter(ComandoMaquina.command_id == command_id).first()
    if comando:
        comando.maquina_id = machine_id
        comando.tipo = tipo
        comando.topic = topic
        comando.payload = payload
        comando.updated_at = _now()
        return comando
    comando = ComandoMaquina(
        command_id=command_id,
        maquina_id=machine_id,
        tipo=tipo,
        topic=topic,
        payload=payload,
        status="pendente",
        max_tentativas=MAX_ATTEMPTS,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(comando)
    db.flush()
    return comando


def _publish_attempt(db, comando: ComandoMaquina) -> None:
    from app.services.mqtt_commands import publish_raw_mqtt_command

    comando.tentativas = int(comando.tentativas or 0) + 1
    comando.updated_at = _now()
    try:
        publish_raw_mqtt_command(comando.topic, comando.payload)
    except Exception as exc:
        comando.ultimo_erro = str(exc)[:500]
        comando.status = (
            "falha_publicacao"
            if comando.tentativas >= int(comando.max_tentativas or MAX_ATTEMPTS)
            else "aguardando_retry"
        )
        comando.next_retry_at = _now() + timedelta(seconds=RETRY_DELAY_SECONDS)
        comando.updated_at = _now()
        db.commit()
        raise

    comando.status = "enviado"
    comando.sent_at = _now()
    comando.next_retry_at = _now() + timedelta(seconds=ACK_TIMEOUT_SECONDS)
    comando.ultimo_erro = None
    comando.updated_at = _now()
    db.commit()


def _machine_has_in_flight_credit_command(
    db, machine_id: str, exclude_command_id: str | None = None
) -> bool:
    """Verifica se a maquina ja tem um comando de credito em andamento. A
    propria placa ignora um segundo credito enquanto o primeiro esta sendo
    processado (flag credito_travado no firmware) - sem essa checagem aqui,
    dois pagamentos quase simultaneos na mesma maquina fariam o segundo ser
    cobrado do cliente e descartado silenciosamente pela placa."""
    query = db.query(ComandoMaquina).filter(
        ComandoMaquina.maquina_id == machine_id,
        ComandoMaquina.tipo == CREDIT_COMMAND_TIPO,
        ComandoMaquina.status.notin_(FINAL_COMMAND_STATUSES | {QUEUED_STATUS}),
    )
    if exclude_command_id:
        query = query.filter(ComandoMaquina.command_id != exclude_command_id)
    return db.query(query.exists()).scalar()


def track_and_publish_command(
    *,
    machine_id: str,
    command_id: str | None,
    tipo: str,
    topic: str,
    payload: str,
) -> None:
    if not command_id:
        from app.services.mqtt_commands import publish_raw_mqtt_command

        publish_raw_mqtt_command(topic, payload)
        return

    db = SessionLocal()
    try:
        comando = _get_or_create_command(
            db,
            machine_id=machine_id,
            command_id=command_id,
            tipo=tipo,
            topic=topic,
            payload=payload,
        )
        if tipo == CREDIT_COMMAND_TIPO and _machine_has_in_flight_credit_command(
            db, machine_id, exclude_command_id=command_id
        ):
            comando.status = QUEUED_STATUS
            comando.detalhe_status = "aguardando_maquina_livre"
            comando.updated_at = _now()
            db.commit()
            return
        _publish_attempt(db, comando)
    finally:
        db.close()


def process_queued_commands() -> int:
    """Libera comandos de credito que ficaram na fila esperando a maquina
    terminar o pagamento anterior. Roda junto com o retry loop existente."""
    db = SessionLocal()
    processed = 0
    try:
        queued = (
            db.query(ComandoMaquina)
            .filter(ComandoMaquina.status == QUEUED_STATUS)
            .order_by(ComandoMaquina.created_at.asc())
            .limit(50)
            .all()
        )
        released_machines: set[str] = set()
        for comando in queued:
            if comando.maquina_id in released_machines:
                continue
            if _machine_has_in_flight_credit_command(
                db, comando.maquina_id, exclude_command_id=comando.command_id
            ):
                continue
            try:
                _publish_attempt(db, comando)
                processed += 1
                released_machines.add(comando.maquina_id)
            except Exception:
                logging.exception(
                    "Falha ao liberar comando da fila command_id=%s maquina_id=%s",
                    comando.command_id,
                    comando.maquina_id,
                )
    finally:
        db.close()
    return processed


def update_command_from_device_status(command_id: str | None, status: str) -> None:
    if not command_id:
        return
    command_status, finished = _status_from_device_status(status)
    if not command_status:
        return
    _update_command_status(command_id, command_status, status, finished)


def update_command_from_pulse_status(command_id: str | None, status: str) -> None:
    if not command_id:
        return
    command_status, finished = _status_from_pulse_status(status)
    if not command_status:
        return
    _update_command_status(command_id, command_status, status, finished)


def _update_command_status(command_id: str, status: str, detail: str, finished: bool) -> None:
    db = SessionLocal()
    try:
        comando = db.query(ComandoMaquina).filter(ComandoMaquina.command_id == command_id).first()
        if not comando:
            return
        if comando.status in FINAL_COMMAND_STATUSES and not finished:
            return
        comando.status = status
        comando.detalhe_status = detail
        comando.updated_at = _now()
        if status in {"recebido", "executando", "executado"} and not comando.ack_at:
            comando.ack_at = _now()
        if finished:
            comando.finished_at = _now()
            comando.next_retry_at = None
        db.commit()
    finally:
        db.close()


def get_command_status(command_id: str) -> str | None:
    db = SessionLocal()
    try:
        comando = db.query(ComandoMaquina).filter(ComandoMaquina.command_id == command_id).first()
        return comando.status if comando else None
    finally:
        db.close()


def process_due_command_retries() -> int:
    db = SessionLocal()
    processed = 0
    try:
        due = (
            db.query(ComandoMaquina)
            .filter(
                ComandoMaquina.status.in_(RETRYABLE_STATUSES),
                ComandoMaquina.next_retry_at.isnot(None),
                ComandoMaquina.next_retry_at <= _now(),
                ComandoMaquina.tentativas < ComandoMaquina.max_tentativas,
            )
            .order_by(ComandoMaquina.next_retry_at.asc())
            .limit(20)
            .all()
        )
        for comando in due:
            if comando.status == "enviado" and comando.ack_at:
                continue
            try:
                _publish_attempt(db, comando)
                processed += 1
            except Exception:
                logging.exception(
                    "Falha no retry MQTT command_id=%s maquina_id=%s tentativa=%s",
                    comando.command_id,
                    comando.maquina_id,
                    comando.tentativas,
                )

        expired = (
            db.query(ComandoMaquina)
            .filter(
                and_(
                    ComandoMaquina.status.in_(RETRYABLE_STATUSES),
                    ComandoMaquina.tentativas >= ComandoMaquina.max_tentativas,
                    ComandoMaquina.next_retry_at.isnot(None),
                    ComandoMaquina.next_retry_at <= _now(),
                )
            )
            .all()
        )
        for comando in expired:
            comando.status = "falhou"
            comando.detalhe_status = "retry_esgotado"
            comando.finished_at = _now()
            comando.updated_at = _now()
            comando.next_retry_at = None
        if expired:
            db.commit()
    finally:
        db.close()
    return processed


def run_command_queue_worker() -> None:
    logging.info("Command queue worker iniciado")
    while True:
        try:
            process_due_command_retries()
        except Exception:
            logging.exception("Erro no command queue worker")
        try:
            process_queued_commands()
        except Exception:
            logging.exception("Erro ao processar fila de comandos de credito")
        time.sleep(5)


def start_command_queue_worker() -> threading.Thread | None:
    if not getattr(settings, "START_COMMAND_QUEUE_WORKER", True):
        logging.info("Command queue worker desativado por START_COMMAND_QUEUE_WORKER=false")
        return None
    thread = threading.Thread(target=run_command_queue_worker, daemon=True)
    thread.start()
    return thread
