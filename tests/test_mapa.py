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


def test_mapa_so_mostra_maquina_com_local_real_e_cliente_precisa_ser_liberado():
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
        assert client.get("/api/v1/mapa/acesso", headers=dono).json() == {"permitido": False}
        assert client.get("/api/v1/mapa/maquinas", headers=dono).status_code == 403

        client.put(f"/api/v1/mapa/clientes/{cliente_id}", json={"habilitado": True}, headers=admin)
        liberado = client.get("/api/v1/mapa/maquinas", headers=dono).json()
        assert [m["maquina_id"] for m in liberado["maquinas"]] == ["MP-COM"]
        assert liberado["sem_localizacao"] == []


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
