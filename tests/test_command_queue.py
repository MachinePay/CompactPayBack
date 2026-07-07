import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import ComandoMaquina
import app.models.models  # noqa: F401
import app.models.produto  # noqa: F401
from app.services.command_queue import (
    MAX_ATTEMPTS,
    process_due_command_retries,
    track_and_publish_command,
    update_command_from_device_status,
    update_command_from_pulse_status,
)

Base.metadata.create_all(bind=engine)


def _get_comando(command_id):
    db = SessionLocal()
    try:
        return db.query(ComandoMaquina).filter(ComandoMaquina.command_id == command_id).first()
    finally:
        db.close()


def test_track_and_publish_command_success_marks_enviado():
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command") as publish_mock:
        track_and_publish_command(
            machine_id="CPM-QUEUE-1",
            command_id="cmd-success-1",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-1/cmd",
            payload="CPM-QUEUE-1@paid|cmd=cmd-success-1|",
        )

    publish_mock.assert_called_once_with("/TEF/CPM-QUEUE-1/cmd", "CPM-QUEUE-1@paid|cmd=cmd-success-1|")
    comando = _get_comando("cmd-success-1")
    assert comando is not None
    assert comando.status == "enviado"
    assert comando.tentativas == 1
    assert comando.sent_at is not None
    assert comando.next_retry_at is not None
    assert comando.ultimo_erro is None


def test_track_and_publish_command_without_command_id_skips_persistence():
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command") as publish_mock:
        track_and_publish_command(
            machine_id="CPM-QUEUE-2",
            command_id=None,
            tipo="ping",
            topic="/TEF/CPM-QUEUE-2/cmd",
            payload="CPM-QUEUE-2@ping|",
        )

    publish_mock.assert_called_once_with("/TEF/CPM-QUEUE-2/cmd", "CPM-QUEUE-2@ping|")
    db = SessionLocal()
    try:
        assert db.query(ComandoMaquina).filter(ComandoMaquina.maquina_id == "CPM-QUEUE-2").count() == 0
    finally:
        db.close()


def test_track_and_publish_command_failure_marks_aguardando_retry_and_raises():
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command", side_effect=RuntimeError("broker off")):
        try:
            track_and_publish_command(
                machine_id="CPM-QUEUE-3",
                command_id="cmd-fail-1",
                tipo="paid",
                topic="/TEF/CPM-QUEUE-3/cmd",
                payload="CPM-QUEUE-3@paid|cmd=cmd-fail-1|",
            )
            assert False, "esperava que a excecao de publicacao fosse propagada"
        except RuntimeError:
            pass

    comando = _get_comando("cmd-fail-1")
    assert comando is not None
    assert comando.tentativas == 1
    assert comando.status == "aguardando_retry"
    assert comando.ultimo_erro == "broker off"
    assert comando.next_retry_at is not None


def test_process_due_command_retries_resends_and_updates_status():
    db = SessionLocal()
    try:
        comando = ComandoMaquina(
            command_id="cmd-retry-ok",
            maquina_id="CPM-QUEUE-4",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-4/cmd",
            payload="CPM-QUEUE-4@paid|cmd=cmd-retry-ok|",
            status="aguardando_retry",
            tentativas=1,
            max_tentativas=MAX_ATTEMPTS,
            next_retry_at=datetime.utcnow() - timedelta(seconds=1),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(comando)
        db.commit()
    finally:
        db.close()

    with patch("app.services.mqtt_commands.publish_raw_mqtt_command") as publish_mock:
        processed = process_due_command_retries()

    publish_mock.assert_called_once()
    assert processed == 1
    comando = _get_comando("cmd-retry-ok")
    assert comando.status == "enviado"
    assert comando.tentativas == 2


def test_process_due_command_retries_marks_falhou_after_attempts_exhausted():
    db = SessionLocal()
    try:
        comando = ComandoMaquina(
            command_id="cmd-retry-exhausted",
            maquina_id="CPM-QUEUE-5",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-5/cmd",
            payload="CPM-QUEUE-5@paid|cmd=cmd-retry-exhausted|",
            status="falha_publicacao",
            tentativas=MAX_ATTEMPTS,
            max_tentativas=MAX_ATTEMPTS,
            next_retry_at=datetime.utcnow() - timedelta(seconds=1),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(comando)
        db.commit()
    finally:
        db.close()

    with patch("app.services.mqtt_commands.publish_raw_mqtt_command") as publish_mock:
        processed = process_due_command_retries()

    publish_mock.assert_not_called()
    assert processed == 0
    comando = _get_comando("cmd-retry-exhausted")
    assert comando.status == "falhou"
    assert comando.detalhe_status == "retry_esgotado"
    assert comando.next_retry_at is None
    assert comando.finished_at is not None


def test_update_command_from_device_status_tracks_ack_and_final_state():
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command"):
        track_and_publish_command(
            machine_id="CPM-QUEUE-6",
            command_id="cmd-lifecycle",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-6/cmd",
            payload="CPM-QUEUE-6@paid|cmd=cmd-lifecycle|",
        )

    update_command_from_device_status("cmd-lifecycle", "CMD_RECEBIDO")
    comando = _get_comando("cmd-lifecycle")
    assert comando.status == "recebido"
    assert comando.ack_at is not None

    update_command_from_pulse_status("cmd-lifecycle", "pulso_confirmado")
    comando = _get_comando("cmd-lifecycle")
    assert comando.status == "executado"
    assert comando.finished_at is not None
    assert comando.next_retry_at is None


def test_final_status_is_not_downgraded_by_late_events():
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command"):
        track_and_publish_command(
            machine_id="CPM-QUEUE-7",
            command_id="cmd-final-guard",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-7/cmd",
            payload="CPM-QUEUE-7@paid|cmd=cmd-final-guard|",
        )

    update_command_from_device_status("cmd-final-guard", "PULSOS_CONCLUIDOS")
    comando = _get_comando("cmd-final-guard")
    assert comando.status == "executado"

    # Um evento tardio (ex.: CMD_RECEBIDO duplicado chegando fora de ordem) nao pode
    # reabrir um comando que ja foi confirmado como executado.
    update_command_from_device_status("cmd-final-guard", "CMD_RECEBIDO")
    comando = _get_comando("cmd-final-guard")
    assert comando.status == "executado"
