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

from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import HistoricoOperacao, Maquina, UserRole, Usuario
from app.services.mqtt_worker import on_message
from app.main import app

Base.metadata.create_all(bind=engine)


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


def _create_maquina(id_hardware, **kwargs):
    db = SessionLocal()
    try:
        maquina = db.query(Maquina).filter(Maquina.id_hardware == id_hardware).first()
        if not maquina:
            maquina = Maquina(id_hardware=id_hardware, nome_local=id_hardware)
            db.add(maquina)
        for key, value in kwargs.items():
            setattr(maquina, key, value)
        db.commit()
    finally:
        db.close()


def _get_maquina(id_hardware):
    db = SessionLocal()
    try:
        return db.query(Maquina).filter(Maquina.id_hardware == id_hardware).first()
    finally:
        db.close()


def _create_admin_token(client, email):
    db = SessionLocal()
    try:
        db.add(Usuario(email=email, hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.commit()
    finally:
        db.close()
    response = client.post("/api/v1/login", data={"username": email, "password": "123456"})
    return response.json()["access_token"]


def _request_update(client, headers, machine_id, url="https://example.com/firmware.bin", version="9.9.9"):
    with patch("app.services.mqtt_commands.publish.single"):
        return client.post(
            f"/api/v1/maquinas/{machine_id}/atualizacao",
            json={"url": url, "version": version},
            headers=headers,
        )


def test_enviar_atualizacao_rejects_when_machine_offline():
    machine_id = "CPM-OTA-OFFLINE"
    _create_maquina(machine_id, ultimo_sinal=None)

    with TestClient(app) as client:
        token = _create_admin_token(client, "admin-ota-offline@test.local")
        headers = {"Authorization": f"Bearer {token}"}
        response = _request_update(client, headers, machine_id)

    assert response.status_code == 409
    assert "offline" in response.json()["detail"].lower()


def test_enviar_atualizacao_rejects_concurrent_update_in_progress():
    machine_id = "CPM-OTA-LOCK"
    _create_maquina(
        machine_id,
        ultimo_sinal=datetime.utcnow(),
        firmware_update_status="downloading",
        firmware_update_requested_at=datetime.utcnow(),
    )

    with TestClient(app) as client:
        token = _create_admin_token(client, "admin-ota-lock@test.local")
        headers = {"Authorization": f"Bearer {token}"}
        response = _request_update(client, headers, machine_id)

    assert response.status_code == 409
    assert "andamento" in response.json()["detail"].lower()


def test_enviar_atualizacao_allows_new_attempt_after_stale_lock_expires():
    machine_id = "CPM-OTA-STALE-LOCK"
    _create_maquina(
        machine_id,
        ultimo_sinal=datetime.utcnow(),
        firmware_version="1.0.0",
        firmware_update_status="downloading",
        firmware_update_requested_at=datetime.utcnow() - timedelta(minutes=11),
    )

    with TestClient(app) as client:
        token = _create_admin_token(client, "admin-ota-stale@test.local")
        headers = {"Authorization": f"Bearer {token}"}
        response = _request_update(client, headers, machine_id)

    assert response.status_code == 200
    maquina = _get_maquina(machine_id)
    assert maquina.firmware_update_status == "sent"


def test_enviar_atualizacao_snapshots_last_good_version_before_dispatch():
    machine_id = "CPM-OTA-SNAPSHOT"
    _create_maquina(
        machine_id,
        ultimo_sinal=datetime.utcnow(),
        firmware_version="1.2.3",
        firmware_last_good_version=None,
    )

    with TestClient(app) as client:
        token = _create_admin_token(client, "admin-ota-snapshot@test.local")
        headers = {"Authorization": f"Bearer {token}"}
        response = _request_update(client, headers, machine_id, version="1.3.0")

    assert response.status_code == 200
    maquina = _get_maquina(machine_id)
    assert maquina.firmware_last_good_version == "1.2.3"
    assert maquina.firmware_target_version == "1.3.0"
    assert maquina.firmware_update_progress is None
    assert maquina.firmware_update_error is None


def test_update_iniciado_status_sets_downloading_and_resets_progress():
    machine_id = "CPM-OTA-INICIADO"
    _create_maquina(machine_id)

    on_message(
        None,
        None,
        FakeMsg(f"/TEF/{machine_id}/attrs", "STATUS|UPDATE_INICIADO|cmd=cmd-1|url=https://example.com/fw.bin"),
    )

    maquina = _get_maquina(machine_id)
    assert maquina.firmware_update_status == "downloading"
    assert maquina.firmware_update_progress == 0
    assert maquina.firmware_update_url == "https://example.com/fw.bin"


def test_update_progresso_updates_progress_without_creating_historico_row():
    machine_id = "CPM-OTA-PROGRESSO"
    _create_maquina(machine_id, firmware_update_status="downloading")

    db = SessionLocal()
    try:
        historico_antes = db.query(HistoricoOperacao).filter(HistoricoOperacao.maquina_id == machine_id).count()
    finally:
        db.close()

    on_message(None, None, FakeMsg(f"/TEF/{machine_id}/attrs", "STATUS|UPDATE_PROGRESSO|cmd=cmd-1|percent=40"))

    maquina = _get_maquina(machine_id)
    assert maquina.firmware_update_progress == 40

    db = SessionLocal()
    try:
        historico_depois = db.query(HistoricoOperacao).filter(HistoricoOperacao.maquina_id == machine_id).count()
    finally:
        db.close()
    assert historico_depois == historico_antes


def test_update_falhou_captures_error_detail():
    machine_id = "CPM-OTA-FALHOU"
    _create_maquina(machine_id, firmware_update_status="downloading")

    on_message(
        None,
        None,
        FakeMsg(f"/TEF/{machine_id}/attrs", "STATUS|UPDATE_FALHOU|cmd=cmd-1|erro=timeout na conexao"),
    )

    maquina = _get_maquina(machine_id)
    assert maquina.firmware_update_status == "failed"
    assert maquina.firmware_update_error == "timeout na conexao"


def test_confirmed_online_heartbeat_after_update_sets_last_good_version_and_clears_error():
    machine_id = "CPM-OTA-CONFIRMADO"
    _create_maquina(
        machine_id,
        firmware_update_status="failed",
        firmware_update_error="algum erro anterior",
        firmware_target_version="2.0.0",
    )

    on_message(
        None,
        None,
        FakeMsg(f"/TEF/{machine_id}/attrs", "STATUS|ONLINE|fw=2.0.0|rssi=-60|wifi=80"),
    )

    maquina = _get_maquina(machine_id)
    assert maquina.firmware_update_status == "updated"
    assert maquina.firmware_update_progress == 100
    assert maquina.firmware_update_error is None
    assert maquina.firmware_last_good_version == "2.0.0"
    assert maquina.firmware_target_version is None
