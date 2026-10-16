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

from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import Maquina, UserRole, Usuario, VendaPagamento
from app.services.mqtt_worker import on_message
from app.main import app

Base.metadata.create_all(bind=engine)


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


def _admin(client):
    db = SessionLocal()
    try:
        db.add(Usuario(email="admin-ficha@test.local", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.commit()
    finally:
        db.close()
    token = client.post("/api/v1/login", data={"username": "admin-ficha@test.local", "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_maquina_de_ficha_grava_valor_da_ficha_e_conta_fichas():
    db = SessionLocal()
    try:
        db.add(Maquina(id_hardware="FICHA-1", nome_local="Ficha", ultimo_sinal=datetime.utcnow()))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        admin = _admin(client)
        sem_valor = client.put("/api/v1/maquinas/FICHA-1", json={"nome": "Ficha", "maquina_ficha": True}, headers=admin)
        assert sem_valor.status_code == 422
        ok = client.put(
            "/api/v1/maquinas/FICHA-1", json={"nome": "Ficha", "maquina_ficha": True, "valor_ficha": 2.5}, headers=admin
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["maquina_ficha"] is True and ok.json()["valor_ficha"] == 2.5

        for _ in range(3):
            on_message(None, None, FakeMsg("/TEF/FICHA-1/attrs", "MOEDA DETECTADA (IN)"))

        item = next(m for m in client.get("/api/v1/maquinas?periodo=dia", headers=admin).json() if m["id_hardware"] == "FICHA-1")

    assert item["fisico_quantidade"] == 3  # 3 fichas
    assert item["faturamento_fisico"] == 7.5  # valor real: 3 x R$ 2,50
    db = SessionLocal()
    try:
        vendas = db.query(VendaPagamento).filter(VendaPagamento.maquina_id == "FICHA-1").all()
        assert {v.tipo_pagamento for v in vendas} == {"ficha"}
    finally:
        db.close()


def test_maquina_comum_continua_um_real_por_pulso():
    db = SessionLocal()
    try:
        db.add(Maquina(id_hardware="MOEDA-1", nome_local="Moeda"))
        db.commit()
    finally:
        db.close()
    on_message(None, None, FakeMsg("/TEF/MOEDA-1/attrs", "MOEDA DETECTADA (IN)"))
    db = SessionLocal()
    try:
        venda = db.query(VendaPagamento).filter(VendaPagamento.maquina_id == "MOEDA-1").one()
        assert venda.valor_bruto == 1.0 and venda.tipo_pagamento == "moeda_nota"
    finally:
        db.close()
