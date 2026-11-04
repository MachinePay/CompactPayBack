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

from app.api.v1.endpoints import wifi_reservas as reservas_endpoints
from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import HistoricoOperacao, Maquina, MaquinaWifiReserva, UserRole, Usuario
from app.services import mqtt_commands
from app.services.mqtt_worker import on_message
from app.services.wifi_credenciais import descriptografar_senha
from app.main import app

Base.metadata.create_all(bind=engine)

FW_OK = "version_2.9.0-rodizio"


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


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


def test_salvar_reservas_grava_criptografado_e_envia_para_placa(monkeypatch):
    machine_id = "CPM-RES-1"
    _create_maquina(machine_id, firmware_version=FW_OK, ultimo_sinal=datetime.utcnow())
    enviados = []
    monkeypatch.setattr(reservas_endpoints, "publish_machine_wifi_reservas", lambda *a: enviados.append(a))

    with TestClient(app) as client:
        headers = _admin_headers(client, "admin-res-1@test.local")
        url = f"/api/v1/maquinas/{machine_id}/wifi/reservas"
        resposta = client.put(
            url,
            json={"redes": [{"ssid": "Vizinho", "senha": "senha-vizinho"}, {"ssid": "Aberta", "senha": ""}]},
            headers=headers,
        )
        assert resposta.status_code == 200, resposta.text
        assert enviados[0][2] == [("Vizinho", "senha-vizinho"), ("Aberta", "")]

        lista = client.get(url, headers=headers).json()
        assert [(r["ordem"], r["ssid"], r["tem_senha"]) for r in lista["redes"]] == [
            (1, "Vizinho", True),
            (2, "Aberta", False),
        ]
        assert "senha" not in lista["redes"][0]

        # Reordena sem redigitar a senha (senha null = mantem a salva).
        resposta = client.put(
            url, json={"redes": [{"ssid": "Aberta", "senha": None}, {"ssid": "Vizinho", "senha": None}]}, headers=headers
        )
        assert resposta.status_code == 200, resposta.text
        assert enviados[1][2] == [("Aberta", ""), ("Vizinho", "senha-vizinho")]

    db = SessionLocal()
    try:
        linhas = db.query(MaquinaWifiReserva).filter(MaquinaWifiReserva.maquina_id == machine_id).all()
        vizinho = next(r for r in linhas if r.ssid == "Vizinho")
        assert vizinho.ordem == 2
        assert "senha-vizinho" not in vizinho.senha_cript
        assert descriptografar_senha(vizinho.senha_cript) == "senha-vizinho"
    finally:
        db.close()


def test_salvar_reservas_valida_antes_de_enviar(monkeypatch):
    machine_id = "CPM-RES-2"
    _create_maquina(machine_id, firmware_version=FW_OK, ultimo_sinal=datetime.utcnow())
    enviados = []
    monkeypatch.setattr(reservas_endpoints, "publish_machine_wifi_reservas", lambda *a: enviados.append(a))

    with TestClient(app) as client:
        headers = _admin_headers(client, "admin-res-2@test.local")
        url = f"/api/v1/maquinas/{machine_id}/wifi/reservas"
        cinco = [{"ssid": f"Rede {i}", "senha": "12345678"} for i in range(5)]
        assert client.put(url, json={"redes": cinco}, headers=headers).status_code == 422
        curta = [{"ssid": "Rede", "senha": "123"}]
        assert client.put(url, json={"redes": curta}, headers=headers).status_code == 422
        repetida = [{"ssid": "Rede", "senha": "12345678"}, {"ssid": "Rede", "senha": "87654321"}]
        assert client.put(url, json={"redes": repetida}, headers=headers).status_code == 422
    assert enviados == []


def test_firmware_antigo_recusa_reservas(monkeypatch):
    machine_id = "CPM-RES-3"
    _create_maquina(machine_id, firmware_version="version_2.8.6-filtro", ultimo_sinal=datetime.utcnow())
    monkeypatch.setattr(reservas_endpoints, "publish_machine_wifi_reservas", lambda *a: None)

    with TestClient(app) as client:
        headers = _admin_headers(client, "admin-res-3@test.local")
        resposta = client.put(
            f"/api/v1/maquinas/{machine_id}/wifi/reservas",
            json={"redes": [{"ssid": "Rede", "senha": "12345678"}]},
            headers=headers,
        )
        assert resposta.status_code == 409
        assert "2.9.0" in resposta.json()["detail"]


def test_payload_mqtt_em_hex_e_sem_senha_no_registro(monkeypatch):
    capturado = {}
    from app.services import command_queue

    monkeypatch.setattr(command_queue, "track_and_publish_sensitive_command", lambda **kw: capturado.update(kw))
    mqtt_commands.publish_machine_wifi_reservas("CPM-X", "cmd-1", [("Loja|2", "segredo123")])

    assert capturado["payload"] == (
        f"CPM-X@wifi_reservas|cmd=cmd-1|qtd=1|rs1={'Loja|2'.encode().hex()}|rp1={'segredo123'.encode().hex()}|"
    )
    assert "segredo123" not in capturado["payload_registrado"]
    assert "segredo123".encode().hex() not in capturado["payload_registrado"]


def test_payload_com_quatro_redes_no_maximo_cabe_no_buffer_da_placa(monkeypatch):
    capturado = {}
    from app.services import command_queue

    monkeypatch.setattr(command_queue, "track_and_publish_sensitive_command", lambda **kw: capturado.update(kw))
    redes = [("S" * 32, "P" * 63) for _ in range(4)]
    machine_id = "CP0001_001"
    mqtt_commands.publish_machine_wifi_reservas(machine_id, "0" * 36, redes)
    # MQTT.setBufferSize(1024) no firmware: cabecalho + topico + payload.
    assert len(capturado["payload"]) + len(capturado["topic"]) + 10 < 1024


def test_aviso_de_rodizio_vai_para_o_historico():
    machine_id = "CPM-RES-4"
    _create_maquina(machine_id)
    on_message(
        None,
        None,
        FakeMsg(f"/TEF/{machine_id}/attrs", f"STATUS|WIFI_RODIZIO|ordem=2|reservas=3|ssid_hex={'Vizinho'.encode().hex()}"),
    )
    db = SessionLocal()
    try:
        descricoes = [
            h.descricao for h in db.query(HistoricoOperacao).filter(HistoricoOperacao.maquina_id == machine_id).all()
        ]
        assert "Wi-Fi: conectou na rede reserva 2 (Vizinho)" in descricoes
    finally:
        db.close()
