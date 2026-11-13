"""Banco "mercado_pago_point": tela touch (ESP32) + Point em modo integrado.

O Mercado Pago, o ping da placa e o MQTT sao simulados. O que importa aqui:
- a tela so cobra o valor de um produto da propria maquina (nunca um valor livre);
- a order sai com external_reference MACHINE_ID:ts, que o webhook existente sabe ler;
- a tela so enxerga a propria maquina e so as orders dela;
- se a order foi paga e o webhook nao chegou, a consulta da tela libera o credito
  uma vez so;
- nao da para cadastrar uma Point que tambem dispararia o webhook de "payment"
  (escuta ativa ou caixa de outra maquina), o que creditaria duas vezes.
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db")
os.environ.setdefault("START_MQTT_WORKER", "false")
os.environ.setdefault("START_COMMAND_QUEUE_WORKER", "false")
os.environ.setdefault("START_RETENTION_WORKER", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.v1.endpoints.tela_point as endpoint
import app.services.mercado_pago_webhook as webhook
from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.models import Cliente, EscutaTerminal, Maquina, UserRole, Usuario, VendaPagamento
from app.models.produto import Produto
from app.models.tela_point import TelaPoint
from app.services import tela_point as svc

Base.metadata.create_all(bind=engine)

TOKEN_A = "APP_USR-token-tela-a"
POINT_A = "NEWLAND_N950__N950NCB000001"
POINT_ESCUTA = "NEWLAND_N950__N950NCB000002"
POINT_CAIXA_OCUPADO = "NEWLAND_N950__N950NCB000003"
IDS = {}


@pytest.fixture(scope="module", autouse=True)
def dados():
    agora = datetime.utcnow()
    db = SessionLocal()
    try:
        a = Cliente(nome_empresa="Tela A", email_contato="a@tela.test", api_key="k-tela-a", mp_access_token=TOKEN_A)
        b = Cliente(nome_empresa="Tela B", email_contato="b@tela.test", api_key="k-tela-b", mp_access_token="APP_USR-b")
        db.add_all([a, b])
        db.commit()
        db.add_all([
            Maquina(id_hardware="TP-1", nome_local="Grua 1", cliente_id=a.id,
                    banco_pagamento="mercado_pago_point", ultimo_sinal=agora),
            Maquina(id_hardware="TP-2", nome_local="Grua 2", cliente_id=a.id,
                    banco_pagamento="mercado_pago_point", ultimo_sinal=agora),
            Maquina(id_hardware="TP-QR", nome_local="Grua QR", cliente_id=a.id,
                    banco_pagamento="mercado_pago", mp_pos_id="POS-OCUPADO", ultimo_sinal=agora),
            Maquina(id_hardware="TP-B", nome_local="Grua B", cliente_id=b.id,
                    banco_pagamento="mercado_pago_point", ultimo_sinal=agora),
        ])
        db.commit()
        p1 = Produto(nome="1 jogada", valor=5.0, maquina_id="TP-1")
        p2 = Produto(nome="3 jogadas", valor=10.0, maquina_id="TP-1")
        p_outra = Produto(nome="Outra", valor=7.0, maquina_id="TP-2")
        db.add_all([p1, p2, p_outra])
        db.add(EscutaTerminal(terminal_id="N950NCB000002", maquina_id="TP-QR", ativo=True))
        db.add(Usuario(email="admin@tela.test", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.add(Usuario(email="dono-b@tela.test", hashed_password=get_password_hash("123456"),
                       role=UserRole.cliente, cliente_id=b.id))
        db.commit()
        IDS.update(a=a.id, b=b.id, p1=p1.id, p2=p2.id, p_outra=p_outra.id)
    finally:
        db.close()
    yield


def _login(client, email):
    token = client.post("/api/v1/login", data={"username": email, "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def mp(monkeypatch):
    """Simula a API do MP. mp.orders[id] = order; POST /v1/orders cria uma nova."""
    class Fake:
        pass

    fake = Fake()
    fake.orders, fake.criadas, fake.canceladas, fake.pulsos, fake.tokens = {}, [], [], [], []
    fake.placa_online = True
    fake.recusar_api_nova = fake.recusar_api_antiga = False
    fake.pdv = []
    fake.contas = {TOKEN_A: "conta-a"}
    fake.tokens_recusados = set()
    fake.terminais = [
        {"id": POINT_A, "operating_mode": "PDV", "pos_id": 111, "store_id": 1, "external_pos_id": "CAIXA-TELA"},
        {"id": POINT_ESCUTA, "operating_mode": "PDV", "pos_id": 222},
        {"id": POINT_CAIXA_OCUPADO, "operating_mode": "PDV", "pos_id": "POS-OCUPADO"},
    ]

    recusa = HTTPException(status_code=502, detail="Mercado Pago retornou erro 403: PA_UNAUTHORIZED_RESULT_FROM_POLICIES")

    def mp_request(method, url, token, body=None, headers=None, **kw):
        if url.endswith("/users/me"):
            return {"id": fake.contas.get(token, "conta-desconhecida")}
        fake.tokens.append(token)
        if token in fake.tokens_recusados:
            raise recusa
        if "/terminals/v1/" in url and fake.recusar_api_nova:
            raise recusa
        if "/point/integration-api/devices" in url and fake.recusar_api_antiga:
            raise recusa
        if url.endswith("/terminals/v1/list?limit=50"):
            return {"data": {"terminals": fake.terminais}}
        if url.endswith("/point/integration-api/devices?limit=50"):
            return {"devices": fake.terminais, "paging": {"total": len(fake.terminais)}}
        if method == "PATCH":
            fake.pdv.append((url, body))
            return {"ok": True}
        if method == "POST" and url.endswith("/v1/orders"):
            assert headers and headers.get("X-Idempotency-Key")
            order_id = f"ORD{len(fake.criadas) + 1:03d}"
            order = {**body, "id": order_id, "status": "created"}
            fake.orders[order_id] = order
            fake.criadas.append(order)
            return order
        if method == "POST" and url.endswith("/cancel"):
            order_id = url.rsplit("/", 2)[-2]
            fake.canceladas.append(order_id)
            fake.orders[order_id]["status"] = "canceled"
            return fake.orders[order_id]
        order_id = url.rsplit("/", 1)[-1]
        if order_id not in fake.orders:
            raise HTTPException(status_code=502, detail="order not found")
        return fake.orders[order_id]

    def known_tokens(db, method, url, preferred_token=None):
        return mp_request(method, url, preferred_token), preferred_token

    monkeypatch.setattr(svc, "mp_request", mp_request)
    monkeypatch.setattr(svc, "get_command_status", lambda command_id: "enviado")
    monkeypatch.setattr(endpoint, "verificar_placa_online", lambda db, maquina: fake.placa_online)
    # Webhook existente (sem alteracao), chamado pela consulta da tela.
    monkeypatch.setattr(webhook, "mp_request_with_known_tokens", known_tokens)
    monkeypatch.setattr(webhook, "resolve_mp_token_by_user_id", lambda db, user_id: TOKEN_A)
    monkeypatch.setattr(webhook, "_acquire_payment_lock", lambda db, key: None)  # sem pg_advisory no SQLite
    monkeypatch.setattr(webhook, "publish_machine_credit_pulses", lambda machine_id, **kw: fake.pulsos.append((machine_id, kw)))
    monkeypatch.setattr(webhook, "get_command_status", lambda command_id: "enviado")
    return fake


def _tela(client, maquina="TP-1", point=POINT_A, nome="Tela grua", headers=None):
    headers = headers or _login(client, "admin@tela.test")
    return client.post(
        "/api/v1/tela/telas",
        json={"maquina_id": maquina, "nome": nome, "point_terminal_id": point},
        headers=headers,
    )


def _chave(client, **kw):
    resp = _tela(client, **kw)
    assert resp.status_code == 200, resp.text
    return {"X-Tela-Key": resp.json()["chave"]}


# ---- Cadastro da tela -------------------------------------------------------------

def test_chave_so_aparece_na_criacao_e_nova_tela_revoga_a_anterior(mp):
    with TestClient(app) as client:
        antiga = _chave(client, nome="Tela antiga")
        assert client.get("/api/v1/tela/config", headers=antiga).status_code == 200
        nova = _chave(client, nome="Tela nova")

        assert client.get("/api/v1/tela/config", headers=antiga).status_code == 401
        assert client.get("/api/v1/tela/config", headers=nova).status_code == 200
        lista = client.get("/api/v1/tela/telas", headers=_login(client, "admin@tela.test")).json()
        assert all("chave" not in t for t in lista)
        db = SessionLocal()
        try:
            ativas = db.query(TelaPoint).filter(TelaPoint.maquina_id == "TP-1", TelaPoint.ativo.is_(True)).all()
            assert [t.nome for t in ativas] == ["Tela nova"]
            assert ativas[0].chave_hash == svc.hash_chave(nova["X-Tela-Key"])
        finally:
            db.close()


def test_tela_so_em_maquina_do_banco_novo(mp):
    with TestClient(app) as client:
        resp = _tela(client, maquina="TP-QR")
        assert resp.status_code == 422


def test_cliente_nao_cadastra_tela_em_maquina_de_outro_cliente(mp):
    with TestClient(app) as client:
        resp = _tela(client, maquina="TP-1", headers=_login(client, "dono-b@tela.test"))
        assert resp.status_code == 404


def test_point_com_escuta_ativa_ou_no_caixa_de_outra_maquina_e_recusada(mp):
    with TestClient(app) as client:
        escuta = _tela(client, maquina="TP-2", point=POINT_ESCUTA)
        assert escuta.status_code == 409 and "escuta" in escuta.json()["detail"]
        caixa = _tela(client, maquina="TP-2", point=POINT_CAIXA_OCUPADO)
        assert caixa.status_code == 409 and "TP-QR" in caixa.json()["detail"]
        inexistente = _tela(client, maquina="TP-2", point="NEWLAND_N950__NAO_EXISTE")
        assert inexistente.status_code == 404


def test_revogar_tela_corta_o_acesso(mp):
    with TestClient(app) as client:
        headers = _chave(client, maquina="TP-2", nome="Tela revogar")
        tela_id = next(t["id"] for t in client.get("/api/v1/tela/telas", headers=_login(client, "admin@tela.test")).json()
                       if t["nome"] == "Tela revogar" and t["ativo"])
        client.delete(f"/api/v1/tela/telas/{tela_id}", headers=_login(client, "admin@tela.test"))
        assert client.get("/api/v1/tela/config", headers=headers).status_code == 401


# ---- A tela -------------------------------------------------------------------

def test_config_mostra_so_os_valores_da_propria_maquina(mp):
    with TestClient(app) as client:
        headers = _chave(client)
        assert client.get("/api/v1/tela/config").status_code == 401
        cfg = client.get("/api/v1/tela/config", headers=headers).json()
        assert cfg["maquina_id"] == "TP-1"
        assert cfg["online"] is True
        assert [p["valor"] for p in cfg["produtos"]] == [5.0, 10.0]


def test_cobranca_usa_valor_do_produto_e_referencia_que_o_webhook_le(mp):
    with TestClient(app) as client:
        headers = _chave(client)
        resp = client.post("/api/v1/tela/cobrancas", json={"produto_id": IDS["p2"]}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["valor"] == 10.0
        order = mp.criadas[-1]
        assert order["type"] == "point"
        assert order["transactions"]["payments"][0]["amount"] == "10.00"
        assert order["config"]["point"]["terminal_id"] == POINT_A
        assert webhook.parse_machine_id_from_external_reference(order["external_reference"]) == "TP-1"
        assert set(mp.tokens) == {TOKEN_A}


def test_cobranca_recusa_produto_de_outra_maquina_e_placa_offline(mp):
    with TestClient(app) as client:
        headers = _chave(client)
        outra = client.post("/api/v1/tela/cobrancas", json={"produto_id": IDS["p_outra"]}, headers=headers)
        assert outra.status_code == 404
        mp.placa_online = False
        offline = client.post("/api/v1/tela/cobrancas", json={"produto_id": IDS["p1"]}, headers=headers)
        assert offline.status_code == 409
        assert mp.criadas == []


def test_order_paga_libera_credito_uma_vez_mesmo_sem_webhook(mp):
    with TestClient(app) as client:
        headers = _chave(client)
        order_id = client.post("/api/v1/tela/cobrancas", json={"produto_id": IDS["p1"]}, headers=headers).json()["order_id"]

        aguardando = client.get(f"/api/v1/tela/cobrancas/{order_id}", headers=headers).json()
        assert aguardando["situacao"] == "aguardando"
        assert mp.pulsos == []

        mp.orders[order_id]["status"] = "processed"
        pago = client.get(f"/api/v1/tela/cobrancas/{order_id}", headers=headers).json()
        assert pago["situacao"] == "pago"
        assert pago["command_id"]
        # A tela consulta de novo e o webhook chega atrasado: nada sai em dobro.
        client.get(f"/api/v1/tela/cobrancas/{order_id}", headers=headers)
        webhook.processar_callback_mercado_pago({"type": "order", "action": "order.processed", "data": {"id": order_id}})

        assert [m for m, _ in mp.pulsos] == ["TP-1"]
        assert mp.pulsos[0][1]["amount"] == 5.0
        db = SessionLocal()
        try:
            vendas = db.query(VendaPagamento).filter(VendaPagamento.provider_payment_id == order_id).all()
            assert len(vendas) == 1 and vendas[0].maquina_id == "TP-1"
        finally:
            db.close()


def test_tela_nao_ve_nem_cancela_order_de_outra_maquina(mp):
    with TestClient(app) as client:
        headers_1 = _chave(client)
        headers_2 = _chave(client, maquina="TP-2", nome="Tela grua 2")
        order_id = client.post("/api/v1/tela/cobrancas", json={"produto_id": IDS["p1"]}, headers=headers_1).json()["order_id"]

        assert client.get(f"/api/v1/tela/cobrancas/{order_id}", headers=headers_2).status_code == 404
        assert client.post(f"/api/v1/tela/cobrancas/{order_id}/cancelar", headers=headers_2).status_code == 404
        assert mp.canceladas == []

        cancelada = client.post(f"/api/v1/tela/cobrancas/{order_id}/cancelar", headers=headers_1).json()
        assert cancelada["status_mp"] == "canceled"
        assert client.get(f"/api/v1/tela/cobrancas/{order_id}", headers=headers_1).json()["situacao"] == "cancelado"


def test_tela_para_de_cobrar_se_a_maquina_mudar_de_banco(mp):
    with TestClient(app) as client:
        headers = _chave(client, maquina="TP-2", nome="Tela troca banco")
        db = SessionLocal()
        try:
            db.query(Maquina).filter(Maquina.id_hardware == "TP-2").update({Maquina.banco_pagamento: "mercado_pago"})
            db.commit()
            assert client.get("/api/v1/tela/config", headers=headers).status_code == 404
        finally:
            db.query(Maquina).filter(Maquina.id_hardware == "TP-2").update({Maquina.banco_pagamento: "mercado_pago_point"})
            db.commit()
            db.close()


# ---- Permissao do Mercado Pago para as Points ----------------------------------------

def test_api_nova_de_terminais_recusada_usa_a_api_antiga(mp):
    mp.recusar_api_nova = True
    with TestClient(app) as client:
        admin = _login(client, "admin@tela.test")
        resp = client.get("/api/v1/tela/point/terminais", params={"cliente_id": IDS["a"]}, headers=admin)
        assert resp.status_code == 200, resp.text
        assert POINT_A in [t["id"] for t in resp.json()]
        # Cadastro da tela (validacao da Point) e modo integrado tambem caem para a API antiga.
        assert _tela(client).status_code == 200
        ativar = client.post(f"/api/v1/tela/point/terminais/{POINT_A}/modo-integrado",
                             params={"cliente_id": IDS["a"]}, headers=admin)
        assert ativar.status_code == 200, ativar.text
        assert mp.pdv == [(f"https://api.mercadopago.com/point/integration-api/devices/{POINT_A}", {"operating_mode": "PDV"})]


def test_sem_permissao_em_nenhuma_api_explica_o_que_fazer(mp):
    mp.recusar_api_nova = mp.recusar_api_antiga = True
    with TestClient(app) as client:
        admin = _login(client, "admin@tela.test")
        resp = client.get("/api/v1/tela/point/terminais", params={"cliente_id": IDS["a"]}, headers=admin)
        assert resp.status_code == 403
        assert "Point" in resp.json()["detail"] and "aplicacao do Mercado Pago" in resp.json()["detail"]


# ---- Caixa Mercado Pago (a Point fica vinculada ao caixa da maquina) --------------------

def _pos_fake(chamadas):
    def create_pos_for_machine(cliente, maquina):
        chamadas.append(maquina.id_hardware)
        return {
            "mp_store_id": "STORE-1",
            "mp_store_external_id": "LOJA1",
            "mp_pos_id": f"POS-{maquina.id_hardware}",
            "mp_pos_external_id": maquina.id_hardware,
            "mp_qr_image": "https://qr",
        }
    return create_pos_for_machine


def test_criar_maquina_com_banco_de_tela_cria_o_caixa_igual_ao_mercado_pago(mp, monkeypatch):
    import app.api.v1.endpoints.maquinas as maquinas_endpoint

    chamadas = []
    monkeypatch.setattr(maquinas_endpoint, "create_pos_for_machine", _pos_fake(chamadas))
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/maquinas",
            json={"id_hardware": "TP-NOVA", "nome": "Grua nova", "cliente_id": IDS["a"],
                  "banco_pagamento": "mercado_pago_point"},
            headers=_login(client, "admin@tela.test"),
        )
        assert resp.status_code == 200, resp.text
    assert chamadas == ["TP-NOVA"]
    db = SessionLocal()
    try:
        assert db.query(Maquina).filter(Maquina.id_hardware == "TP-NOVA").one().mp_pos_id == "POS-TP-NOVA"
    finally:
        db.close()


def test_maquina_existente_sem_caixa_cria_o_caixa_uma_vez(mp, monkeypatch):
    chamadas = []
    monkeypatch.setattr(svc, "create_pos_for_machine", _pos_fake(chamadas))
    with TestClient(app) as client:
        admin = _login(client, "admin@tela.test")
        assert client.get("/api/v1/tela/maquinas/TP-2/caixa", headers=admin).json()["caixa"] is None
        primeiro = client.post("/api/v1/tela/maquinas/TP-2/caixa", headers=admin)
        segundo = client.post("/api/v1/tela/maquinas/TP-2/caixa", headers=admin)
        assert primeiro.status_code == segundo.status_code == 200
        assert segundo.json()["caixa"]["pos_id"] == "POS-TP-2"
        assert client.post("/api/v1/tela/maquinas/TP-QR/caixa", headers=admin).status_code == 422
        assert client.post("/api/v1/tela/maquinas/TP-2/caixa", headers=_login(client, "dono-b@tela.test")).status_code == 404
    assert chamadas == ["TP-2"]


def test_lista_marca_a_point_vinculada_ao_caixa_da_maquina(mp):
    db = SessionLocal()
    try:
        db.query(Maquina).filter(Maquina.id_hardware == "TP-1").update({Maquina.mp_pos_id: "111"})
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        points = client.get("/api/v1/tela/point/terminais", params={"maquina_id": "TP-1"},
                            headers=_login(client, "admin@tela.test")).json()
    assert {p["id"]: p["neste_caixa"] for p in points} == {POINT_A: True, POINT_ESCUTA: False, POINT_CAIXA_OCUPADO: False}


# ---- So o token cadastrado no cliente ---------------------------------------------------

def test_token_do_cliente_recusado_nao_tenta_nenhum_outro_token(mp, monkeypatch):
    """Mesmo com outro token da mesma conta salvo no sistema, so o do cliente e' usado."""
    import app.core.config as config

    monkeypatch.setattr(config.settings, "MP_ACCESS_TOKEN", "APP_USR-principal-mesma-conta")
    mp.contas["APP_USR-principal-mesma-conta"] = "conta-a"
    mp.tokens_recusados = {TOKEN_A}
    with TestClient(app) as client:
        ativar = client.post(f"/api/v1/tela/point/terminais/{POINT_A}/modo-integrado",
                             params={"cliente_id": IDS["a"]}, headers=_login(client, "admin@tela.test"))
        assert ativar.status_code == 403
    assert set(mp.tokens) == {TOKEN_A}
    assert mp.pdv == []
