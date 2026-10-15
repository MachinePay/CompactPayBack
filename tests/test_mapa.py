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

from app.api.v1.endpoints.mapa import extrair_coordenadas
from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import Cliente, Maquina, UserRole, Usuario
from app.main import app

Base.metadata.create_all(bind=engine)


def test_extrair_coordenadas_de_link_e_texto():
    assert extrair_coordenadas("-23.5505, -46.6333") == (-23.5505, -46.6333)
    assert extrair_coordenadas(
        "https://www.google.com/maps/place/Shopping/@-22.9068,-43.1729,17z/data=!3d-22.90681!4d-43.17291"
    ) == (-22.90681, -43.17291)
    assert extrair_coordenadas("https://maps.google.com/?q=-19.9167,-43.9345") == (-19.9167, -43.9345)
    assert extrair_coordenadas("Shopping Centro") is None
    assert extrair_coordenadas("200.0, 10.0") is None


def _login(client, email, role, cliente_id=None):
    db = SessionLocal()
    try:
        db.add(Usuario(email=email, hashed_password=get_password_hash("123456"), role=role, cliente_id=cliente_id))
        db.commit()
    finally:
        db.close()
    token = client.post("/api/v1/login", data={"username": email, "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_mapa_so_mostra_maquina_com_local_real_e_cliente_ve_so_as_proprias():
    db = SessionLocal()
    try:
        cliente = Cliente(nome_empresa="Cliente Mapa", email_contato="mapa@cliente.test", api_key="k-mapa")
        db.add(cliente)
        db.commit()
        cliente_id = cliente.id
        db.add(Maquina(id_hardware="MP-COM", nome_local="Com local", cliente_id=cliente_id, ultimo_sinal=datetime.utcnow()))
        db.add(Maquina(id_hardware="MP-SEM", nome_local="Sem local", cliente_id=cliente_id))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        admin = _login(client, "admin-mapa@test.local", UserRole.admin)
        assert client.put("/api/v1/mapa/maquinas/MP-COM/localizacao", json={"texto": "-23.55, -46.63"}, headers=admin).status_code == 200
        dados = client.get(f"/api/v1/mapa/maquinas?cliente_id={cliente_id}", headers=admin).json()
        assert [m["maquina_id"] for m in dados["maquinas"]] == ["MP-COM"]
        assert [m["maquina_id"] for m in dados["sem_localizacao"]] == ["MP-SEM"]

        dono = _login(client, "dono-mapa@test.local", UserRole.cliente, cliente_id)
        assert client.get("/api/v1/mapa/acesso", headers=dono).json() == {"permitido": True}
        meu = client.get("/api/v1/mapa/maquinas", headers=dono).json()
        assert [m["maquina_id"] for m in meu["maquinas"]] == ["MP-COM"]
        assert meu["sem_localizacao"] == []
        # Cliente nao muda localizacao (so admin).
        assert client.put("/api/v1/mapa/maquinas/MP-COM/localizacao", json={"texto": "-1, -1"}, headers=dono).status_code == 403


def test_editar_maquina_com_localizacao_no_mapa_opcional():
    db = SessionLocal()
    try:
        db.add(Maquina(id_hardware="MP-EDIT", nome_local="Editar local"))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        admin = _login(client, "admin-mapa-edit@test.local", UserRole.admin)
        base = {"nome": "Editar local"}
        assert client.put("/api/v1/maquinas/MP-EDIT", json=base, headers=admin).status_code == 200
        com_local = client.put("/api/v1/maquinas/MP-EDIT", json={**base, "localizacao_mapa": "-23.5, -46.6"}, headers=admin)
        assert com_local.status_code == 200, com_local.text
        assert com_local.json()["latitude"] == -23.5
        assert client.put("/api/v1/maquinas/MP-EDIT", json={**base, "localizacao_mapa": "texto qualquer"}, headers=admin).status_code == 422
        # Sem o campo, nao mexe; vazio, tira do mapa.
        assert client.put("/api/v1/maquinas/MP-EDIT", json=base, headers=admin).json()["latitude"] == -23.5
        assert client.put("/api/v1/maquinas/MP-EDIT", json={**base, "localizacao_mapa": ""}, headers=admin).json()["latitude"] is None


def test_localizacao_pelo_gps_do_celular_e_edicao_sem_mudanca_nao_apaga_origem():
    db = SessionLocal()
    try:
        db.add(Maquina(id_hardware="MP-GPS", nome_local="GPS"))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        admin = _login(client, "admin-mapa-gps@test.local", UserRole.admin)
        url = "/api/v1/mapa/maquinas/MP-GPS/localizacao"
        impreciso = client.put(url, json={"latitude": -23.5, "longitude": -46.6, "precisao_m": 900}, headers=admin)
        assert impreciso.status_code == 422
        assert client.put(url, json={"latitude": -23.51, "longitude": -46.61, "precisao_m": 12}, headers=admin).status_code == 200

        # Salvar o cadastro com o mesmo valor (formulario vem preenchido) mantem "celular".
        editado = client.put(
            "/api/v1/maquinas/MP-GPS", json={"nome": "GPS", "localizacao_mapa": "-23.51, -46.61"}, headers=admin
        ).json()
        assert editado["localizacao_origem"] == "celular"
        # Mudar na mao vira "manual".
        manual = client.put(
            "/api/v1/maquinas/MP-GPS", json={"nome": "GPS", "localizacao_mapa": "-23.6, -46.7"}, headers=admin
        ).json()
        assert manual["localizacao_origem"] == "manual"


def test_aviso_de_localizacao_depois_do_portal_dos_5_cliques():
    from datetime import timedelta
    from unittest.mock import patch
    import app.services.mqtt_worker as worker

    class Msg:
        def __init__(self, payload):
            self.topic = "/TEF/MP-PORTAL/attrs"
            self.payload = payload.encode()

    db = SessionLocal()
    try:
        db.add(Maquina(id_hardware="MP-PORTAL", nome_local="Portal"))
        db.commit()
    finally:
        db.close()

    inicio = datetime.utcnow() - timedelta(minutes=10)

    class Relogio(datetime):
        atual = inicio

        @classmethod
        def utcnow(cls):
            return cls.atual

    with patch.object(worker, "datetime", Relogio):
        worker.on_message(None, None, Msg("STATUS|RESET_WIFI_BOTAO"))
        Relogio.atual = inicio + timedelta(minutes=3)  # voltou depois do portal
        worker.on_message(None, None, Msg("STATUS|ONLINE|fw=2.8.1|rssi=-60|wifi=80"))

    with TestClient(app) as client:
        admin = _login(client, "admin-mapa-portal@test.local", UserRole.admin)
        pendentes = client.get("/api/v1/mapa/configuradas-agora", headers=admin).json()
        assert [p["maquina_id"] for p in pendentes] == ["MP-PORTAL"]
        client.put(
            "/api/v1/mapa/maquinas/MP-PORTAL/localizacao",
            json={"latitude": -23.5, "longitude": -46.6, "precisao_m": 10},
            headers=admin,
        )
        assert client.get("/api/v1/mapa/configuradas-agora", headers=admin).json() == []
