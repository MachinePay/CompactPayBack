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

from fastapi.testclient import TestClient

from app.api.v1.endpoints import sumup_usuarios
from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import Cliente, Maquina, UserRole, Usuario
from app.main import app

Base.metadata.create_all(bind=engine)

ADMIN = "admin-escolhido@test.local"


def _setup():
    db = SessionLocal()
    try:
        if db.query(Usuario).filter(Usuario.email == ADMIN).first():
            return
        cliente = Cliente(
            nome_empresa="Cli Escolhido", email_contato="cli@escolhido.test", api_key="k-escolhido",
            cliente_sumup=True, sumup_api_key="sup_sk_esc", sumup_merchant_code="MKESC1",
        )
        db.add(cliente)
        db.commit()
        for id_hw in ("ES-1", "ES-2", "ES-3"):
            db.add(Maquina(id_hardware=id_hw, nome_local="Garra", cliente_id=cliente.id, banco_pagamento="sumup"))
        db.add(Usuario(email=ADMIN, hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.commit()
    finally:
        db.close()


def _headers(client):
    token = client.post("/api/v1/login", data={"username": ADMIN, "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _falso(chamadas):
    def criar(chave, mc, email, senha, nickname):
        chamadas.append((email, senha))
        return {"id": f"mem_{len(chamadas)}", "user": {"email": email, "type": "managed_user"}}

    return criar


def test_usa_email_e_senha_escolhidos(monkeypatch):
    _setup()
    chamadas = []
    monkeypatch.setattr(sumup_usuarios, "create_managed_member", _falso(chamadas))
    with TestClient(app) as client:
        headers = _headers(client)
        resposta = client.post(
            "/api/v1/maquinas/ES-1/sumup/usuario",
            json={"email_prefixo": "Garra.Shopping1", "senha": "Minha1234"},
            headers=headers,
        )
        assert resposta.status_code == 200, resposta.text
        assert resposta.json()["usuario"] == "garra.shopping1@compactpay.com.br"
        assert resposta.json()["senha"] == "Minha1234"
        assert chamadas == [("garra.shopping1@compactpay.com.br", "Minha1234")]

        login = client.get("/api/v1/maquinas/ES-1/sumup/usuario", headers=headers).json()
        assert login == {"usuario": "garra.shopping1@compactpay.com.br", "senha": "Minha1234", "criado_pelo_sistema": True}

        # "Criar outro" com login novo escolhido.
        outro = client.post(
            "/api/v1/maquinas/ES-1/sumup/usuario",
            json={"recriar": True, "email_prefixo": "garra-shopping-2", "senha": "Outra5678"},
            headers=headers,
        )
        assert outro.status_code == 200, outro.text
        assert outro.json()["usuario"] == "garra-shopping-2@compactpay.com.br"


def test_rejeita_email_ou_senha_invalidos_sem_chamar_sumup(monkeypatch):
    _setup()
    chamadas = []
    monkeypatch.setattr(sumup_usuarios, "create_managed_member", _falso(chamadas))
    invalidos = [
        {"email_prefixo": "com espaco", "senha": "Senha1234"},
        {"email_prefixo": ".comeca", "senha": "Senha1234"},
        {"email_prefixo": "dois..pontos", "senha": "Senha1234"},
        {"email_prefixo": "x@outro.com", "senha": "Senha1234"},
        {"email_prefixo": "valido", "senha": "curta1"},
        {"email_prefixo": "valido", "senha": "semnumeros"},
        {"email_prefixo": "valido", "senha": "12345678"},
        {"email_prefixo": "valido", "senha": "tem espaco1"},
        {"email_prefixo": "", "senha": "Senha1234"},
    ]
    with TestClient(app) as client:
        headers = _headers(client)
        for corpo in invalidos:
            resposta = client.post("/api/v1/maquinas/ES-2/sumup/usuario", json=corpo, headers=headers)
            assert resposta.status_code == 422, (corpo, resposta.text)
    assert chamadas == []


def test_email_ja_usado_em_outra_maquina_do_cliente(monkeypatch):
    _setup()
    chamadas = []
    monkeypatch.setattr(sumup_usuarios, "create_managed_member", _falso(chamadas))
    with TestClient(app) as client:
        headers = _headers(client)
        assert client.post(
            "/api/v1/maquinas/ES-2/sumup/usuario",
            json={"email_prefixo": "repetido", "senha": "Senha1234"},
            headers=headers,
        ).status_code == 200
        resposta = client.post(
            "/api/v1/maquinas/ES-3/sumup/usuario",
            json={"email_prefixo": "repetido", "senha": "Senha1234"},
            headers=headers,
        )
        assert resposta.status_code == 422
        assert "ES-2" in resposta.json()["detail"]
    assert len(chamadas) == 1


def test_maquina_recem_criada_ja_recebe_login_escolhido(monkeypatch):
    """Fluxo do painel ao criar maquina nova: POST /maquinas e logo depois o
    usuario da maquininha com o login digitado."""
    _setup()
    chamadas = []
    monkeypatch.setattr(sumup_usuarios, "create_managed_member", _falso(chamadas))
    db = SessionLocal()
    try:
        cliente_id = db.query(Cliente).filter(Cliente.sumup_merchant_code == "MKESC1").first().id
    finally:
        db.close()
    with TestClient(app) as client:
        headers = _headers(client)
        nova = client.post(
            "/api/v1/maquinas",
            json={
                "id_hardware": "ES-NOVA", "nome": "Nova", "localizacao": "Loja", "cliente_id": cliente_id,
                "banco_pagamento": "sumup", "sumup_device_code": "ABC123", "sumup_usuario": "",
            },
            headers=headers,
        )
        assert nova.status_code in (200, 201), nova.text
        resposta = client.post(
            "/api/v1/maquinas/ES-NOVA/sumup/usuario",
            json={"email_prefixo": "nova", "senha": "Senha1234"},
            headers=headers,
        )
        assert resposta.status_code == 200, resposta.text
    db = SessionLocal()
    try:
        maquina = db.query(Maquina).filter(Maquina.id_hardware == "ES-NOVA").first()
        assert maquina.sumup_usuario == "nova@compactpay.com.br"
        assert maquina.sumup_usuario_senha_cript
    finally:
        db.close()


def test_cartao_de_maquina_so_com_login_credita_pelo_usuario():
    from app.services.sumup_poller import _resolve_maquina

    so_login = Maquina(id_hardware="L-1", nome_local="L1", sumup_usuario="l1@compactpay.com.br")
    com_reader = Maquina(id_hardware="L-2", nome_local="L2", sumup_usuario="l2@compactpay.com.br", sumup_reader_id="rdr_2")
    por_usuario = {"l1@compactpay.com.br": so_login, "l2@compactpay.com.br": com_reader}

    # Cartao (traz codigo do reader desconhecido) numa maquina vinculada so pelo login.
    maquina, motivo = _resolve_maquina("999", {}, {}, {}, usuario="L1@compactpay.com.br", maquina_by_usuario=por_usuario)
    assert maquina is so_login and motivo == "verificado_por_usuario"

    # Maquina que tem reader cadastrado: reader diferente continua sendo ignorado.
    maquina, motivo = _resolve_maquina("999", {}, {}, {}, usuario="l2@compactpay.com.br", maquina_by_usuario=por_usuario)
    assert maquina is None and motivo == "device_sem_reader_correspondente"

    # Usuario que nao e' de nenhuma maquina: ignorado como antes.
    maquina, motivo = _resolve_maquina("999", {}, {}, {}, usuario="dono@loja.com", maquina_by_usuario=por_usuario)
    assert maquina is None and motivo == "device_sem_reader_correspondente"


def test_cria_maquina_sumup_sem_reader(monkeypatch):
    _setup()
    monkeypatch.setattr(sumup_usuarios, "create_managed_member", _falso([]))
    db = SessionLocal()
    try:
        cliente_id = db.query(Cliente).filter(Cliente.sumup_merchant_code == "MKESC1").first().id
    finally:
        db.close()
    with TestClient(app) as client:
        headers = _headers(client)
        nova = client.post(
            "/api/v1/maquinas",
            json={
                "id_hardware": "ES-SEMREADER", "nome": "Sem reader", "localizacao": "Loja", "cliente_id": cliente_id,
                "banco_pagamento": "sumup", "sumup_usuario": "",
            },
            headers=headers,
        )
        assert nova.status_code in (200, 201), nova.text
        resposta = client.post(
            "/api/v1/maquinas/ES-SEMREADER/sumup/usuario",
            json={"email_prefixo": "semreader", "senha": "Senha1234"},
            headers=headers,
        )
        assert resposta.status_code == 200, resposta.text
