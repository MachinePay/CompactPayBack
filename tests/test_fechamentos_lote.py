"""Resumo do fechamento em lote (GET /fechamentos-lote/maquinas): valor em
aberto desde o ultimo fechamento + ultimo fechamento de cada maquina, sempre
respeitando a regra de acesso (cliente so as proprias)."""
import os
import sys
import tempfile
from datetime import datetime, timedelta
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
from app.models.models import Cliente, Maquina, UserRole, Usuario, VendaPagamento
from app.main import app

Base.metadata.create_all(bind=engine)


def _setup():
    agora = datetime.utcnow()
    db = SessionLocal()
    try:
        a = Cliente(nome_empresa="Lote A", email_contato="a@lote.test", api_key="k-lote-a")
        b = Cliente(nome_empresa="Lote B", email_contato="b@lote.test", api_key="k-lote-b")
        db.add_all([a, b])
        db.commit()
        db.add(Maquina(id_hardware="LOTE-A", nome_local="Maq A", cliente_id=a.id))
        db.add(Maquina(id_hardware="LOTE-B1", nome_local="Maq B1", cliente_id=b.id))
        db.add(Maquina(id_hardware="LOTE-B2", nome_local="Maq B2", cliente_id=b.id))
        db.commit()
        db.add(VendaPagamento(maquina_id="LOTE-B1", origem="digital", valor_bruto=50.0, valor_liquido=50.0,
                              conta_faturamento=True, created_at=agora - timedelta(minutes=5)))
        db.add(Usuario(email="admin@lote.test", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.add(Usuario(email="dono-a@lote.test", hashed_password=get_password_hash("123456"),
                       role=UserRole.cliente, cliente_id=a.id))
        db.commit()
        return a.id, b.id
    finally:
        db.close()


def _headers(client, email):
    token = client.post("/api/v1/login", data={"username": email, "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_resumo_lote_mostra_aberto_e_ultimo_fechamento():
    _, cliente_b = _setup()
    with TestClient(app) as client:
        admin = _headers(client, "admin@lote.test")
        url = f"/api/v1/fechamentos-lote/maquinas?cliente_id={cliente_b}"

        antes = {item["id_hardware"]: item for item in client.get(url, headers=admin).json()}
        assert set(antes) == {"LOTE-B1", "LOTE-B2"}
        assert antes["LOTE-B1"]["aberto_total"] == 50.0
        assert antes["LOTE-B1"]["ultimo_fechamento"] is None

        criado = client.post("/api/v1/maquinas/LOTE-B1/fechamentos?periodo=fechamento", headers=admin)
        assert criado.status_code == 200, criado.text

        depois = {item["id_hardware"]: item for item in client.get(url, headers=admin).json()}
        assert depois["LOTE-B1"]["aberto_total"] == 0.0
        assert depois["LOTE-B1"]["ultimo_fechamento"]["id"] == criado.json()["id"]
        assert depois["LOTE-B1"]["ultimo_fechamento"]["total_pagamentos"] == 50.0
        assert depois["LOTE-B2"]["ultimo_fechamento"] is None


def test_resumo_lote_respeita_escopo_do_cliente():
    with TestClient(app) as client:
        a = _headers(client, "dono-a@lote.test")
        b_id = SessionLocal().query(Cliente.id).filter(Cliente.api_key == "k-lote-b").scalar()
        resposta = client.get(f"/api/v1/fechamentos-lote/maquinas?cliente_id={b_id}", headers=a)
        assert {item["id_hardware"] for item in resposta.json()} == {"LOTE-A"}
        assert client.get("/api/v1/fechamentos-lote/maquinas").status_code == 401
