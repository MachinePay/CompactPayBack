import os
import sys
import tempfile
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["START_SUMUP_POLLER_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.v1.endpoints import sumup_usuarios
from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import Cliente, Maquina, UserRole, Usuario
from app.main import app

Base.metadata.create_all(bind=engine)


def _setup():
    db = SessionLocal()
    try:
        cliente = Cliente(
            nome_empresa="Cli Membros", email_contato="cli@membros.test", api_key="k-membros",
            cliente_sumup=True, sumup_api_key="sup_sk_teste", sumup_merchant_code="MKTESTE1",
        )
        db.add(cliente)
        db.commit()
        db.add(Maquina(id_hardware="MB-1", nome_local="Garra", cliente_id=cliente.id, banco_pagamento="sumup"))
        db.add(Usuario(email="admin-membros@test.local", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.commit()
    finally:
        db.close()


def _headers(client):
    token = client.post("/api/v1/login", data={"username": "admin-membros@test.local", "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_cria_usuario_da_maquininha_e_guarda_senha_criptografada(monkeypatch):
    _setup()
    chamadas = []

    def falso_criar(chave, mc, email, senha, nickname):
        chamadas.append((mc, email, senha, nickname))
        return {"id": "mem_123", "user": {"email": email, "type": "managed_user"}}

    monkeypatch.setattr(sumup_usuarios, "create_managed_member", falso_criar)
    with TestClient(app) as client:
        headers = _headers(client)
        resposta = client.post("/api/v1/maquinas/MB-1/sumup/usuario", headers=headers)
        assert resposta.status_code == 200, resposta.text
        dados = resposta.json()
        assert dados["usuario"].startswith("maquinamb-1.") and dados["usuario"].endswith("@compactpay.com.br")
        assert len(dados["senha"]) >= 12
        assert chamadas[0][0] == "MKTESTE1"

        # Segunda vez sem "recriar" nao cria outro.
        assert client.post("/api/v1/maquinas/MB-1/sumup/usuario", headers=headers).status_code == 409
        # Ver login devolve a mesma senha.
        login = client.get("/api/v1/maquinas/MB-1/sumup/usuario", headers=headers).json()
        assert login["senha"] == dados["senha"]

    db = SessionLocal()
    try:
        maquina = db.query(Maquina).filter(Maquina.id_hardware == "MB-1").first()
        assert maquina.sumup_usuario == dados["usuario"]
        assert dados["senha"] not in (maquina.sumup_usuario_senha_cript or "")
        assert maquina.sumup_usuario_member_id == "mem_123"
    finally:
        db.close()


def test_chave_sem_permissao_explica_o_que_fazer(monkeypatch):
    def sem_permissao(*args, **kwargs):
        raise HTTPException(status_code=502, detail="SumUp retornou erro 403: forbidden")

    monkeypatch.setattr(sumup_usuarios, "create_managed_member", sem_permissao)
    db = SessionLocal()
    try:
        cliente = db.query(Cliente).filter(Cliente.sumup_merchant_code == "MKTESTE1").first()
        db.add(Maquina(id_hardware="MB-2", nome_local="Garra 2", cliente_id=cliente.id, banco_pagamento="sumup"))
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        resposta = client.post("/api/v1/maquinas/MB-2/sumup/usuario", headers=_headers(client))
    assert resposta.status_code == 502
    assert "members.write" in resposta.json()["detail"]
