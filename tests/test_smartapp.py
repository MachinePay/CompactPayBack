"""Banco "mercado_pago_apk": rotas /smartapp usadas pelo app CompactPay na Point.

O Mercado Pago e o MQTT sao simulados. O que importa aqui:
- o pulso so sai depois de o pagamento ser conferido no MP (status, valor, referencia);
- o mesmo payment_id nunca libera credito duas vezes (retentativa do app ou webhook);
- a chave do terminal so enxerga maquinas mercado_pago_apk do proprio cliente.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.models import Cliente, ComandoMaquina, HistoricoOperacao, Maquina, UserRole, Usuario, VendaPagamento
from app.models.produto import Produto
from app.models.smartapp import TerminalSmartApp  # noqa: F401 - registra a tabela
from app.services import smartapp as svc

Base.metadata.create_all(bind=engine)

TOKEN_A = "APP_USR-token-cliente-a"


def _setup():
    agora = datetime.utcnow()
    db = SessionLocal()
    try:
        a = Cliente(nome_empresa="Lavanderia A", email_contato="a@smartapp.test", api_key="k-sa-a",
                    mp_access_token=TOKEN_A)
        b = Cliente(nome_empresa="Lavanderia B", email_contato="b@smartapp.test", api_key="k-sa-b",
                    mp_access_token="APP_USR-token-cliente-b")
        db.add_all([a, b])
        db.commit()
        db.add_all([
            Maquina(id_hardware="SA-APK", nome_local="Lavadora 1", cliente_id=a.id,
                    banco_pagamento="mercado_pago_apk", ultimo_sinal=agora),
            Maquina(id_hardware="SA-APK-OFF", nome_local="Lavadora 2", cliente_id=a.id,
                    banco_pagamento="mercado_pago_apk", ultimo_sinal=agora - timedelta(minutes=10)),
            Maquina(id_hardware="SA-MP", nome_local="Lavadora MP", cliente_id=a.id,
                    banco_pagamento="mercado_pago", ultimo_sinal=agora),
            Maquina(id_hardware="SA-APK-B", nome_local="Lavadora B", cliente_id=b.id,
                    banco_pagamento="mercado_pago_apk", ultimo_sinal=agora),
        ])
        db.commit()
        db.add(Produto(nome="Lavagem", valor=12.0, maquina_id="SA-APK"))
        db.add(Usuario(email="admin@smartapp.test", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.add(Usuario(email="dono-a@smartapp.test", hashed_password=get_password_hash("123456"),
                       role=UserRole.cliente, cliente_id=a.id))
        db.commit()
        return a.id, b.id
    finally:
        db.close()


CLIENTE_A = CLIENTE_B = None


@pytest.fixture(scope="module", autouse=True)
def dados_smartapp():
    """Cria os dados so quando este modulo roda (o banco SQLite e' compartilhado
    entre arquivos de teste) e, no fim, tira a maquina "offline" do caminho dos
    testes de alerta."""
    global CLIENTE_A, CLIENTE_B
    CLIENTE_A, CLIENTE_B = _setup()
    yield
    db = SessionLocal()
    try:
        db.query(Maquina).filter(Maquina.id_hardware.like("SA-%")).update(
            {Maquina.ultimo_sinal: datetime.utcnow()}, synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def _login(client, email):
    token = client.post("/api/v1/login", data={"username": email, "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _chave(client, cliente_id=None, nome="Point balcao"):
    admin = _login(client, "admin@smartapp.test")
    cliente_id = cliente_id or CLIENTE_A
    resp = client.post("/api/v1/smartapp/terminais", json={"nome": nome, "cliente_id": cliente_id}, headers=admin)
    assert resp.status_code == 200, resp.text
    return {"X-Terminal-Key": resp.json()["chave"]}


def _mp_payment(payment_id, ref, valor=12.0, status="approved", criado=None):
    criado = criado or datetime.now(timezone.utc)
    return {
        "id": payment_id,
        "status": status,
        "description": ref,
        "transaction_amount": valor,
        "date_created": criado.isoformat(),
        "payment_type_id": "credit_card",
        "payment_method_id": "visa",
        "card": {"last_four_digits": "4242"},
    }


@pytest.fixture
def mp(monkeypatch):
    """Simula o MP e o MQTT. mp.pagamentos[id] = resposta de GET /v1/payments/{id}."""
    class Fake:
        pagamentos: dict = {}
        tokens: list = []
        pulsos: list = []

    fake = Fake()
    fake.pagamentos, fake.tokens, fake.pulsos = {}, [], []

    def mp_request(method, url, token, **kwargs):
        fake.tokens.append(token)
        if "/v1/payments/search" in url:
            return {"results": list(fake.pagamentos.values())}
        payment_id = url.rsplit("/", 1)[-1]
        if payment_id not in fake.pagamentos:
            raise HTTPException(status_code=502, detail="Mercado Pago: payment not found")
        return fake.pagamentos[payment_id]

    monkeypatch.setattr(svc, "mp_request", mp_request)
    monkeypatch.setattr(svc, "_acquire_payment_lock", lambda db, key: None)  # pg_advisory_lock nao existe no SQLite
    monkeypatch.setattr(svc, "publish_machine_credit_pulses", lambda machine_id, **kw: fake.pulsos.append((machine_id, kw)))
    monkeypatch.setattr(svc, "get_command_status", lambda command_id: "enviado")
    monkeypatch.setattr(svc, "preencher_nome_banco_em_segundo_plano", lambda *a, **k: None)
    return fake


def _pagar(client, headers, maquina="SA-APK", payment_id="PAY-1", ref=None, valor=12.0):
    ref = ref or f"CP-{maquina}-1700000000000"
    return client.post(
        "/api/v1/smartapp/pagamentos",
        json={"maquina_id": maquina, "produto_id": None, "valor": valor, "payment_id": payment_id,
              "payment_reference": ref, "metodo": "credito", "sn_terminal": "PAX-123"},
        headers=headers,
    )


# ---- Chave do terminal ------------------------------------------------------

def test_chave_so_aparece_na_criacao_e_fica_so_o_hash():
    with TestClient(app) as client:
        headers = _chave(client, nome="Point chave")
        chave = headers["X-Terminal-Key"]
        lista = client.get("/api/v1/smartapp/terminais", headers=_login(client, "admin@smartapp.test")).json()
        assert all("chave" not in t for t in lista)
        db = SessionLocal()
        try:
            terminal = db.query(TerminalSmartApp).filter(TerminalSmartApp.nome == "Point chave").one()
            assert terminal.chave_hash == svc.hash_chave(chave)
            assert chave not in terminal.chave_hash
        finally:
            db.close()


def test_sem_chave_ou_chave_errada_ou_revogada_da_401():
    with TestClient(app) as client:
        assert client.get("/api/v1/smartapp/maquinas/SA-APK").status_code == 401
        assert client.get("/api/v1/smartapp/maquinas/SA-APK", headers={"X-Terminal-Key": "cpt_errada"}).status_code == 401

        headers = _chave(client, nome="Point revogar")
        assert client.get("/api/v1/smartapp/maquinas/SA-APK", headers=headers).status_code == 200
        terminal_id = next(
            t["id"] for t in client.get("/api/v1/smartapp/terminais", headers=_login(client, "admin@smartapp.test")).json()
            if t["nome"] == "Point revogar"
        )
        client.delete(f"/api/v1/smartapp/terminais/{terminal_id}", headers=_login(client, "admin@smartapp.test"))
        assert client.get("/api/v1/smartapp/maquinas/SA-APK", headers=headers).status_code == 401


def test_cliente_cria_chave_so_para_o_proprio_cliente():
    with TestClient(app) as client:
        dono_a = _login(client, "dono-a@smartapp.test")
        resp = client.post("/api/v1/smartapp/terminais", json={"nome": "Tentativa", "cliente_id": CLIENTE_B}, headers=dono_a)
        assert resp.status_code == 200
        assert resp.json()["cliente_id"] == CLIENTE_A
        ids = {t["cliente_id"] for t in client.get("/api/v1/smartapp/terminais", headers=dono_a).json()}
        assert ids == {CLIENTE_A}


# ---- Maquina ------------------------------------------------------------------

def test_maquina_mostra_online_e_produtos():
    with TestClient(app) as client:
        headers = _chave(client)
        dados = client.get("/api/v1/smartapp/maquinas/SA-APK", headers=headers).json()
        assert dados["nome"] == "Lavadora 1"
        assert dados["online"] is True
        assert dados["produtos"] == [{"id": dados["produtos"][0]["id"], "nome": "Lavagem", "valor": 12.0}]
        assert client.get("/api/v1/smartapp/maquinas/SA-APK-OFF", headers=headers).json()["online"] is False


def test_terminal_nao_ve_maquina_de_outro_banco_nem_de_outro_cliente():
    with TestClient(app) as client:
        headers = _chave(client)
        # Mesmo cliente, mas banco mercado_pago (fluxo antigo): fica de fora.
        assert client.get("/api/v1/smartapp/maquinas/SA-MP", headers=headers).status_code == 404
        assert client.get("/api/v1/smartapp/maquinas/SA-APK-B", headers=headers).status_code == 404


def test_verificar_online_usa_ping_e_pong_da_placa(monkeypatch):
    def ping_responde(machine_id, command_id):
        db = SessionLocal()
        try:
            db.add(HistoricoOperacao(maquina_id=machine_id, categoria="DISPOSITIVO", command_id=command_id,
                                     descricao="status=PONG fw=x", created_at=datetime.utcnow()))
            db.commit()
        finally:
            db.close()

    with TestClient(app) as client:
        headers = _chave(client)
        monkeypatch.setattr(svc, "publish_machine_ping", ping_responde)
        assert client.post("/api/v1/smartapp/maquinas/SA-APK-OFF/verificar-online", headers=headers).json()["online"] is True

        monkeypatch.setattr(svc, "publish_machine_ping", lambda machine_id, command_id: None)
        monkeypatch.setattr(svc, "PING_TIMEOUT_SECONDS", 0.3)
        assert client.post("/api/v1/smartapp/maquinas/SA-APK/verificar-online", headers=headers).json()["online"] is False


# ---- Pagamento ----------------------------------------------------------------

def test_pagamento_aprovado_libera_pulso_uma_vez_e_retentativa_devolve_o_mesmo_comando(mp):
    with TestClient(app) as client:
        headers = _chave(client)
        ref = "CP-SA-APK-1700000000001"
        mp.pagamentos["PAY-OK"] = _mp_payment("PAY-OK", ref, valor=12.0)

        primeira = _pagar(client, headers, payment_id="PAY-OK", ref=ref)
        assert primeira.status_code == 200, primeira.text
        assert primeira.json()["pulsos"] == 12
        assert primeira.json()["duplicado"] is False
        assert mp.tokens == [TOKEN_A]  # conferido com o token do proprio cliente
        assert len(mp.pulsos) == 1 and mp.pulsos[0][0] == "SA-APK"

        segunda = _pagar(client, headers, payment_id="PAY-OK", ref=ref)
        assert segunda.status_code == 200
        assert segunda.json()["duplicado"] is True
        assert segunda.json()["command_id"] == primeira.json()["command_id"]
        assert len(mp.pulsos) == 1  # nada de credito em dobro

        db = SessionLocal()
        try:
            vendas = db.query(VendaPagamento).filter(VendaPagamento.provider_payment_id == "PAY-OK").all()
            assert len(vendas) == 1
            assert vendas[0].provider == "mercado_pago"  # estorno existente continua funcionando
            assert vendas[0].valor_bruto == 12.0
        finally:
            db.close()


def test_status_do_comando_so_do_proprio_cliente():
    db = SessionLocal()
    try:
        for command_id, maquina in [("cmd-sa-a", "SA-APK"), ("cmd-sa-b", "SA-APK-B")]:
            db.add(ComandoMaquina(command_id=command_id, maquina_id=maquina, tipo="paid",
                                  topic=f"/TEF/{maquina}/cmd", payload="x", status="executado"))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        headers = _chave(client)
        resp = client.get("/api/v1/smartapp/comandos/cmd-sa-a", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "executado"
        assert client.get("/api/v1/smartapp/comandos/cmd-sa-b", headers=headers).status_code == 404


def test_sem_payment_id_procura_pela_referencia(mp):
    with TestClient(app) as client:
        headers = _chave(client)
        ref = "CP-SA-APK-1700000000002"
        mp.pagamentos["PAY-OUTRO"] = _mp_payment("PAY-OUTRO", "CP-SA-APK-999", valor=12.0)
        mp.pagamentos["PAY-REF"] = _mp_payment("PAY-REF", ref, valor=12.0)
        resp = _pagar(client, headers, payment_id=None, ref=ref)
        assert resp.status_code == 200, resp.text
        assert resp.json()["payment_id"] == "PAY-REF"

        nao_achou = _pagar(client, headers, payment_id=None, ref="CP-SA-APK-1700000000099")
        assert nao_achou.status_code == 404
        assert len(mp.pulsos) == 1


@pytest.mark.parametrize(
    "pagamento, enviado, esperado",
    [
        ({"status": "pending"}, {}, 409),                                  # nao aprovado
        ({"status": "rejected"}, {}, 409),
        ({"transaction_amount": 2.0}, {}, 409),                            # app mandou valor maior que o pago
        ({"description": "CP-SA-APK-outra-cobranca"}, {}, 409),            # pagamento de outra cobranca
        ({}, {"ref": "CP-SA-APK-B-1700000000003"}, 404),                   # placa de outro cliente
        ({}, {"ref": "CP-SA-APK-OFF-1700000000003"}, 422),                 # referencia de outra placa
        ({"date_created": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}, {}, 409),
    ],
)
def test_pagamento_invalido_nao_libera_pulso(mp, pagamento, enviado, esperado):
    with TestClient(app) as client:
        headers = _chave(client)
        ref = "CP-SA-APK-1700000000003"
        mp.pagamentos["PAY-RUIM"] = {**_mp_payment("PAY-RUIM", ref), **pagamento}
        maquina = "SA-APK-B" if enviado.get("ref", "").startswith("CP-SA-APK-B") else "SA-APK"
        resp = _pagar(client, headers, maquina=maquina, payment_id="PAY-RUIM", ref=enviado.get("ref", ref))
        assert resp.status_code == esperado, resp.text
        assert mp.pulsos == []


def test_pagamento_ja_usado_pelo_webhook_em_outra_maquina_e_recusado(mp):
    db = SessionLocal()
    try:
        db.add(VendaPagamento(maquina_id="SA-MP", origem="mercado_pago", provider="mercado_pago",
                              provider_payment_id="PAY-WEBHOOK", valor_bruto=12.0, valor_liquido=12.0,
                              command_id="cmd-webhook", created_at=datetime.utcnow()))
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        headers = _chave(client)
        ref = "CP-SA-APK-1700000000004"
        mp.pagamentos["PAY-WEBHOOK"] = _mp_payment("PAY-WEBHOOK", ref)
        resp = _pagar(client, headers, payment_id="PAY-WEBHOOK", ref=ref)
        assert resp.status_code == 409
        assert mp.pulsos == []


# ---- Cadastro de maquina com o banco novo ------------------------------------

def test_painel_cria_maquina_com_banco_mercado_pago_apk_sem_criar_caixa_mp(monkeypatch):
    import app.api.v1.endpoints.maquinas as maquinas_endpoint

    def nao_pode_chamar(*args, **kwargs):
        raise AssertionError("banco mercado_pago_apk nao cria loja/caixa no Mercado Pago")

    for nome in dir(maquinas_endpoint):
        if nome.startswith("create_") and "pos" in nome or nome in {"ensure_cliente_store", "create_store_for_machine"}:
            monkeypatch.setattr(maquinas_endpoint, nome, nao_pode_chamar)

    with TestClient(app) as client:
        admin = _login(client, "admin@smartapp.test")
        resp = client.post(
            "/api/v1/maquinas",
            json={"id_hardware": "SA-NOVA", "nome": "Nova APK", "cliente_id": CLIENTE_A, "banco_pagamento": "mercado_pago_apk"},
            headers=admin,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["banco_pagamento"] == "mercado_pago_apk"
