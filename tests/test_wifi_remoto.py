import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.api.v1.endpoints import wifi as wifi_endpoints
from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import (
    ComandoMaquina,
    HistoricoOperacao,
    Maquina,
    MaquinaWifiCredencial,
    MaquinaWifiTentativa,
    UserRole,
    Usuario,
)
from app.services.mqtt_worker import on_message
from app.services.wifi_credenciais import descriptografar_senha
from app.main import app

Base.metadata.create_all(bind=engine)

FW_OK = "version_2.8.0-wifi-remoto-2026-10-14"


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


def _hex(texto):
    return texto.encode("utf-8").hex()


def _create_maquina(id_hardware, **kwargs):
    db = SessionLocal()
    try:
        maquina = Maquina(id_hardware=id_hardware, nome_local=id_hardware)
        for key, value in kwargs.items():
            setattr(maquina, key, value)
        db.add(maquina)
        db.commit()
    finally:
        db.close()


def _admin_headers(client, email):
    db = SessionLocal()
    try:
        db.add(Usuario(email=email, hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.commit()
    finally:
        db.close()
    token = client.post("/api/v1/login", data={"username": email, "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _wifi_info(machine_id, ssid, senha, cmd=None):
    extra = f"|cmd={cmd}" if cmd else ""
    on_message(None, None, FakeMsg(f"/TEF/{machine_id}/attrs", f"STATUS|WIFI_INFO|ssid_hex={_hex(ssid)}|pass_hex={_hex(senha)}{extra}"))


def test_wifi_info_saves_encrypted_without_duplicates_and_never_logs_password():
    machine_id = "CPM-WIFI-INFO"
    _create_maquina(machine_id)

    _wifi_info(machine_id, "Loja|Centro", "senha-secreta-1")
    _wifi_info(machine_id, "Loja|Centro", "senha-secreta-1")  # repetida
    _wifi_info(machine_id, "Loja|Centro", "senha-nova-2")  # mesma rede, senha nova

    db = SessionLocal()
    try:
        redes = db.query(MaquinaWifiCredencial).filter(MaquinaWifiCredencial.maquina_id == machine_id).all()
        assert len(redes) == 2
        assert {descriptografar_senha(r.senha_cript) for r in redes} == {"senha-secreta-1", "senha-nova-2"}
        assert all("senha" not in r.senha_cript for r in redes)
        maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
        assert maquina.wifi_ssid == "Loja|Centro"
        historico = db.query(HistoricoOperacao).filter(HistoricoOperacao.maquina_id == machine_id).all()
        assert historico, "troca de rede deve aparecer no historico da maquina"
        assert all("senha" not in h.descricao and "pass_hex" not in h.descricao for h in historico)
    finally:
        db.close()


def test_trocar_wifi_records_attempt_and_failure_reason(monkeypatch):
    machine_id = "CPM-WIFI-TROCA"
    _create_maquina(machine_id, firmware_version=FW_OK, ultimo_sinal=datetime.utcnow())
    enviados = []
    monkeypatch.setattr(wifi_endpoints, "publish_machine_wifi", lambda *a: enviados.append(a))

    with TestClient(app) as client:
        headers = _admin_headers(client, "admin-wifi@test.local")
        resposta = client.post(
            f"/api/v1/maquinas/{machine_id}/wifi", json={"ssid": "Rede Nova", "senha": "errada123"}, headers=headers
        )
        assert resposta.status_code == 200, resposta.text
        command_id = resposta.json()["command_id"]
        assert enviados == [(machine_id, command_id, "Rede Nova", "errada123")]

        on_message(
            None,
            None,
            FakeMsg(
                f"/TEF/{machine_id}/attrs",
                f"STATUS|WIFI_TROCA_FALHOU|cmd={command_id}|motivo=senha_errada|voltou=rede_antiga",
            ),
        )
        status = client.get(f"/api/v1/maquinas/{machine_id}/wifi/troca/{command_id}", headers=headers).json()
        assert status["resultado"] == "falhou"
        assert status["motivo_texto"] == "Senha errada"

        historico = client.get(f"/api/v1/wifi/historico?machine_id={machine_id}", headers=headers).json()
        assert historico["tentativas"][0]["senha"] == "errada123"
        assert historico["tentativas"][0]["resultado"] == "falhou"

        # Sem cliente nem maquina nao lista nada (evita puxar tudo de uma vez).
        assert client.get("/api/v1/wifi/historico", headers=headers).status_code == 422
        assert client.get("/api/v1/wifi/historico?todas=true", headers=headers).status_code == 200
        # Periodo depois da tentativa: some da lista.
        futuro = client.get(
            f"/api/v1/wifi/historico?machine_id={machine_id}&data_inicio=2999-01-01T00:00:00", headers=headers
        ).json()
        assert futuro["tentativas"] == []


def test_trocar_wifi_validates_before_sending(monkeypatch):
    machine_id = "CPM-WIFI-VALIDA"
    _create_maquina(machine_id, firmware_version="version_2.7.9-ip-local", ultimo_sinal=datetime.utcnow())
    monkeypatch.setattr(wifi_endpoints, "publish_machine_wifi", lambda *a: None)

    with TestClient(app) as client:
        headers = _admin_headers(client, "admin-wifi-valida@test.local")
        curta = client.post(f"/api/v1/maquinas/{machine_id}/wifi", json={"ssid": "Rede", "senha": "123"}, headers=headers)
        assert curta.status_code == 422
        antiga = client.post(
            f"/api/v1/maquinas/{machine_id}/wifi", json={"ssid": "Rede", "senha": "12345678"}, headers=headers
        )
        assert antiga.status_code == 409


def test_wifi_command_never_stores_password_in_command_table(monkeypatch):
    machine_id = "CPM-WIFI-CMD"
    _create_maquina(machine_id, firmware_version=FW_OK, ultimo_sinal=datetime.utcnow())
    from app.services import mqtt_commands

    publicados = []
    monkeypatch.setattr(mqtt_commands, "publish_raw_mqtt_command", lambda topic, payload: publicados.append(payload))

    mqtt_commands.publish_machine_wifi(machine_id, "cmd-wifi-1", "Rede X", "segredo-123")

    db = SessionLocal()
    try:
        comando = db.query(ComandoMaquina).filter(ComandoMaquina.command_id == "cmd-wifi-1").first()
        assert comando.max_tentativas == 1
        assert "segredo" not in comando.payload and _hex("segredo-123") not in comando.payload
    finally:
        db.close()
    assert _hex("segredo-123") in publicados[0]


def test_wifi_scan_result_is_stored_and_listed():
    machine_id = "CPM-WIFI-SCAN"
    _create_maquina(machine_id, firmware_version=FW_OK, ultimo_sinal=datetime.utcnow())
    redes = f"{_hex('Loja 5G')},-48,3;{_hex('Visitantes')},-71,0;lixo"
    on_message(None, None, FakeMsg(f"/TEF/{machine_id}/attrs", f"STATUS|WIFI_REDES|cmd=scan-1|total=2|enviadas=2|redes={redes}"))

    with TestClient(app) as client:
        headers = _admin_headers(client, "admin-wifi-scan@test.local")
        data = client.get(f"/api/v1/maquinas/{machine_id}/wifi/redes", headers=headers).json()

    assert data["atualizado_em"] is not None
    assert data["redes"] == [
        {"ssid": "Loja 5G", "rssi": -48, "aberta": False, "wep": False},
        {"ssid": "Visitantes", "rssi": -71, "aberta": True, "wep": False},
    ]


def test_wifi_info_with_requested_network_confirms_lost_switch(monkeypatch):
    # Caso real (1006, 14/10/2026): a placa trocou de rede mas o WIFI_TROCADO
    # se perdeu; o painel ficou em "Testando". A rede nova chegando pelo
    # WIFI_INFO tem que fechar a troca como "trocado".
    machine_id = "CPM-WIFI-PERDIDO"
    _create_maquina(machine_id, firmware_version=FW_OK, ultimo_sinal=datetime.utcnow())
    monkeypatch.setattr(wifi_endpoints, "publish_machine_wifi", lambda *a: None)

    with TestClient(app) as client:
        headers = _admin_headers(client, "admin-wifi-perdido@test.local")
        command_id = client.post(
            f"/api/v1/maquinas/{machine_id}/wifi", json={"ssid": "Agarramais", "senha": "certa1234"}, headers=headers
        ).json()["command_id"]
        _wifi_info(machine_id, "Agarramais", "certa1234")
        status = client.get(f"/api/v1/maquinas/{machine_id}/wifi/troca/{command_id}", headers=headers).json()

    assert status["resultado"] == "trocado"


def test_attempt_without_result_becomes_sem_confirmacao():
    from datetime import timedelta

    machine_id = "CPM-WIFI-SEM-CONF"
    _create_maquina(machine_id, firmware_version=FW_OK, ultimo_sinal=datetime.utcnow())
    db = SessionLocal()
    try:
        db.add(
            MaquinaWifiTentativa(
                maquina_id=machine_id,
                command_id="cmd-sem-conf",
                ssid="Rede",
                senha_cript="",
                resultado="testando",
                created_at=datetime.utcnow() - timedelta(minutes=10),
            )
        )
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        headers = _admin_headers(client, "admin-wifi-sem-conf@test.local")
        status = client.get(f"/api/v1/maquinas/{machine_id}/wifi/troca/cmd-sem-conf", headers=headers).json()

    assert status["resultado"] == "sem_confirmacao"


def test_portal_attempts_are_recorded_with_result():
    machine_id = "CPM-WIFI-PORTAL"
    _create_maquina(machine_id, firmware_version=FW_OK, ultimo_sinal=datetime.utcnow())
    base = f"/TEF/{machine_id}/attrs"
    on_message(None, None, FakeMsg(base, f"STATUS|WIFI_PORTAL_TENTATIVA|ssid_hex={_hex('Loja')}|pass_hex={_hex('errada99')}|r=15|ordem=1|de=2"))
    on_message(None, None, FakeMsg(base, f"STATUS|WIFI_PORTAL_TENTATIVA|ssid_hex={_hex('Loja')}|pass_hex={_hex('certa123')}|r=1|ordem=2|de=2"))

    with TestClient(app) as client:
        headers = _admin_headers(client, "admin-wifi-portal@test.local")
        tentativas = client.get(f"/api/v1/wifi/historico?machine_id={machine_id}", headers=headers).json()["tentativas"]

    por_senha = {t["senha"]: t for t in tentativas}
    assert por_senha["errada99"]["origem"] == "portal"
    assert por_senha["errada99"]["motivo_texto"] == "Senha errada"
    assert por_senha["certa123"]["resultado"] == "trocado"

    db = SessionLocal()
    try:
        textos = [h.descricao for h in db.query(HistoricoOperacao).filter(HistoricoOperacao.maquina_id == machine_id)]
    finally:
        db.close()
    assert any("Senha errada" in t for t in textos)
    assert not any("errada99" in t or "certa123" in t for t in textos)
