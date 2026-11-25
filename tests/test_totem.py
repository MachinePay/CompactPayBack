"""Totem vending (tablet + agente local): rotas /totem.

O que importa aqui:
- escopo: cliente so mexe no totem das proprias maquinas;
- codigo de vinculo e' de uso unico; o agente so enxerga a propria maquina;
- eventos do agente viram Transacao/VendaPagamento sem duplicar no reenvio e
  sem gerar alerta de "pagamento sem credito";
- cobrancas do totem (referencia "kiosk-" ou Point/Solo do totem) sao
  ignoradas pelo webhook do Mercado Pago e pelo poller da SumUp.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db")
os.environ.setdefault("START_MQTT_WORKER", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.models import Cliente, EventoTipo, Maquina, MetodoPagamento, Transacao, UserRole, Usuario, VendaPagamento
from app.models.totem import TotemVending
from app.services import mercado_pago_webhook as mpw
from app.services.maquinas_relatorio import pagamentos_sem_credito_by_machine

Base.metadata.create_all(bind=engine)

WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16


@pytest.fixture(scope="module", autouse=True)
def dados():
    db = SessionLocal()
    try:
        a = Cliente(nome_empresa="Totem A", email_contato="a@totem.test", api_key="k-totem-a", mp_access_token="APP_USR-totem-a",
                    sumup_api_key="sup_sk_a", sumup_merchant_code="MCA")
        b = Cliente(nome_empresa="Totem B", email_contato="b@totem.test", api_key="k-totem-b")
        db.add_all([a, b])
        db.commit()
        db.add_all([
            Maquina(id_hardware="TOT-1", nome_local="Shopping Centro", cliente_id=a.id),
            Maquina(id_hardware="TOT-B", nome_local="Outro cliente", cliente_id=b.id),
            Maquina(id_hardware="TOT-SEM", nome_local="Sem dono"),
        ])
        db.add(Usuario(email="admin@totem.test", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.add(Usuario(email="dono-a@totem.test", hashed_password=get_password_hash("123456"), role=UserRole.cliente, cliente_id=a.id))
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _login(client, email):
    token = client.post("/api/v1/login", data={"username": email, "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def vinculado(client):
    """Dono ativa o totem na TOT-1 e o agente vincula com o codigo."""
    dono = _login(client, "dono-a@totem.test")
    r = client.post("/api/v1/totem/maquinas/TOT-1/ativar", headers=dono)
    assert r.status_code == 200, r.text
    codigo = r.json()["codigo"]
    v = client.post("/api/v1/totem/vincular", json={"code": codigo.lower(), "agentVersion": "1.1.0", "boardVersion": "Rev0"})
    assert v.status_code == 200, v.text
    body = v.json()
    assert body["machineId"] == "TOT-1" and body["name"] == "Shopping Centro"
    return {"dono": dono, "agente": {"Authorization": f"Bearer {body['token']}"}, "codigo": codigo}


def test_escopo_do_painel(client, vinculado):
    dono = vinculado["dono"]
    assert client.post("/api/v1/totem/maquinas/TOT-B/ativar", headers=dono).status_code == 404
    admin = _login(client, "admin@totem.test")
    assert client.post("/api/v1/totem/maquinas/TOT-SEM/ativar", headers=admin).status_code == 422
    resumo = client.get("/api/v1/totem/maquinas/TOT-1", headers=dono).json()
    assert resumo["ativo"] and resumo["vinculado"] and resumo["versao_placa"] == "Rev0"
    assert resumo["credenciais_cliente"] == {"mercadopago": True, "sumup": True}


def test_codigo_e_uso_unico_e_token_so_ve_a_propria_maquina(client, vinculado):
    again = client.post("/api/v1/totem/vincular", json={"code": vinculado["codigo"]})
    assert again.status_code == 404
    assert client.get("/api/v1/totem/TOT-B/config", headers=vinculado["agente"]).status_code == 401
    assert client.get("/api/v1/totem/TOT-1/config", headers={"Authorization": "Bearer errado"}).status_code == 401


def test_config_produtos_versao_e_maquininha(client, vinculado):
    dono, agente = vinculado["dono"], vinculado["agente"]
    cfg = client.get("/api/v1/totem/TOT-1/config", headers=agente).json()
    assert len(cfg["slots"]) == 9 and cfg["payment"] == {"mode": "off"}
    assert client.get(f"/api/v1/totem/TOT-1/config?version={cfg['version']}", headers=agente).status_code == 204

    up = client.post("/api/v1/totem/midias", headers=dono, files={"arquivo": ("urso.webp", WEBP, "image/webp")})
    assert up.status_code == 200, up.text
    h = up.json()["hash"]
    assert client.get(f"/api/v1/totem/midias/{h}.webp").content == WEBP
    assert client.post("/api/v1/totem/midias", headers=dono, files={"arquivo": ("x.png", b"\x89PNG....", "image/png")}).status_code == 422

    p = client.put("/api/v1/totem/maquinas/TOT-1/produtos/2", headers=dono,
                   json={"nome": "Urso de Pelúcia", "preco_centavos": 1500, "imagem_hash": h, "estoque": 5})
    assert p.status_code == 200, p.text
    pg = client.put("/api/v1/totem/maquinas/TOT-1/pagamento", headers=dono, json={"modo_cartao": "mercadopago", "mp_terminal_id": "PAX__TOTEM1"})
    assert pg.status_code == 200

    novo = client.get(f"/api/v1/totem/TOT-1/config?version={cfg['version']}", headers=agente).json()
    assert novo["version"] != cfg["version"]
    slot2 = next(s for s in novo["slots"] if s["slot"] == 2)
    assert slot2 == {"slot": 2, "name": "Urso de Pelúcia", "priceCents": 1500, "imageHash": h, "enabled": True,
                     "stock": 5, "lowStock": None, "sortOrder": 2}
    assert novo["payment"] == {"mode": "mercadopago", "mercadopago": {"accessToken": "APP_USR-totem-a", "terminalId": "PAX__TOTEM1"}}


def test_eventos_viram_financeiro_sem_duplicar_nem_alertar(client, vinculado):
    agente = vinculado["agente"]
    agora = datetime.utcnow().isoformat() + "Z"
    eventos = [
        {"id": "ev-credit-1", "type": "credit", "payload": {"source": "note", "amount": 2000, "pulses": 20}, "createdAt": agora},
        {"id": "ev-order-1", "type": "order", "payload": {"id": "ord-1", "method": "card", "status": "done", "priceCents": 1500}, "createdAt": agora},
        {"id": "ev-order-2", "type": "order", "payload": {"id": "ord-2", "method": "card", "status": "failed", "priceCents": 1500, "refundStatus": "refunded"}, "createdAt": agora},
        {"id": "ev-disp-1", "type": "dispense", "payload": {"kind": "sale", "delivered": 1, "slot": 2}, "createdAt": agora},
        {"id": "ev-disp-test", "type": "dispense", "payload": {"kind": "test", "delivered": 1, "slot": 0}, "createdAt": agora},
        {"id": "ev-outro", "type": "settings", "payload": {"x": 1}, "createdAt": agora},
    ]
    for _ in range(2):  # reenvio (internet caiu antes da resposta) nao duplica
        r = client.post("/api/v1/totem/TOT-1/eventos", headers=agente, json={"events": eventos})
        assert r.status_code == 200, r.text
        assert set(r.json()["accepted"]) == {e["id"] for e in eventos}

    db = SessionLocal()
    try:
        vendas = db.query(VendaPagamento).filter(VendaPagamento.maquina_id == "TOT-1").all()
        assert sorted((v.origem, v.provider, v.valor_bruto) for v in vendas) == [("fisico", "fisico", 20.0), ("totem", "mercado_pago", 15.0)]
        cartao = next(v for v in vendas if v.origem == "totem")
        assert cartao.provider_payment_id == "kiosk-ord-1" and cartao.status_pulso == "totem_entregue"
        entradas = db.query(Transacao).filter(Transacao.maquina_id == "TOT-1", Transacao.tipo == EventoTipo.in_flux).all()
        assert sorted((t.metodo, t.valor) for t in entradas) == [(MetodoPagamento.digital, 15.0), (MetodoPagamento.fisico, 20.0)]
        saidas = db.query(Transacao).filter(Transacao.maquina_id == "TOT-1", Transacao.tipo == EventoTipo.out_flux).count()
        assert saidas == 1, "teste de motor nao conta premio"
        from app.models.totem import TotemProduto
        estoque = db.query(TotemProduto).filter_by(maquina_id="TOT-1", slot=2).one().estoque
        assert estoque == 4, "painel acompanha o estoque baixado no totem (5 - 1), mesmo com reenvio"
        assert pagamentos_sem_credito_by_machine(db, ["TOT-1"], datetime.utcnow() + timedelta(minutes=10)) == {}
        assert db.query(Maquina).filter(Maquina.id_hardware == "TOT-1").first().ultimo_sinal is not None
    finally:
        db.close()


def test_webhook_mp_ignora_cobranca_do_totem(monkeypatch, vinculado):
    chamadas = []

    def fake_mp(db, method, url, preferred_token=None):
        chamadas.append(url)
        if "/v1/orders/" in url:
            return {"id": "ORD1", "status": "processed", "external_reference": "kiosk-ord-9",
                    "transactions": {"payments": [{"amount": "15.00"}]}}, "tok"
        return {"id": 77, "status": "approved", "transaction_amount": 15.0, "external_reference": None,
                "point_of_interaction": {"device": {"serial_number": "x"}}}, "tok"

    monkeypatch.setattr(mpw, "mp_request_with_known_tokens", fake_mp)
    monkeypatch.setattr(mpw, "resolve_mp_token_by_user_id", lambda db, uid: None)
    r = mpw.processar_callback_mercado_pago({"type": "order", "action": "order.processed", "data": {"id": "ORD1"}})
    assert r["status"] == "ignorado" and "totem" in r["detalhe"].lower()

    monkeypatch.setattr(mpw, "extract_terminal_id", lambda payment: "PAX__TOTEM1")
    r = mpw.processar_callback_mercado_pago({"type": "payment", "action": "payment.created", "data": {"id": "77"}})
    assert r["status"] == "ignorado" and "totem" in r["detalhe"].lower()
    db = SessionLocal()
    try:
        assert db.query(VendaPagamento).filter(VendaPagamento.provider_payment_id == "77").count() == 0
    finally:
        db.close()


def test_poller_sumup_ignora_venda_do_totem(monkeypatch, vinculado):
    from app.services import sumup_poller

    db = SessionLocal()
    try:
        # Cliente A tambem tem uma maquina de pulso com SumUp: o poller roda para ele.
        db.add(Maquina(id_hardware="PULSO-SU", nome_local="Grua", cliente_id=db.query(Cliente).filter_by(api_key="k-totem-a").one().id,
                       banco_pagamento="sumup", sumup_reader_id="rdr_pulso"))
        totem = db.query(TotemVending).filter_by(maquina_id="TOT-1").one()
        totem.sumup_reader_id = "rdr_totem"
        db.commit()
        cliente = db.query(Cliente).filter_by(api_key="k-totem-a").one()
        transacoes = [
            {"id": "tx-ref", "status": "SUCCESSFUL", "amount": 10.0, "client_transaction_id": "kiosk-ord-5"},
            {"id": "tx-dev", "status": "SUCCESSFUL", "amount": 10.0},
        ]
        monkeypatch.setattr(sumup_poller, "list_recent_transactions", lambda *a, **k: transacoes)
        monkeypatch.setattr(sumup_poller, "build_reader_device_map", lambda *a: {"SERIAL-TOTEM": "rdr_totem", "SERIAL-PULSO": "rdr_pulso"})
        monkeypatch.setattr(sumup_poller, "get_receipt", lambda *a: {"transaction_data": {"card_reader": {"code": "SERIAL-TOTEM"}}})
        monkeypatch.setattr(sumup_poller, "extract_card_reader_code", lambda receipt: "SERIAL-TOTEM")
        liberou = []
        monkeypatch.setattr(sumup_poller, "liberar_pulso_sumup", lambda *a, **k: liberou.append(a))
        sumup_poller.process_cliente(db, cliente)
        assert liberou == []
        from app.models.models import SumupTransacaoPendente
        assert db.query(SumupTransacaoPendente).filter(SumupTransacaoPendente.transaction_id.in_(["tx-ref", "tx-dev"])).count() == 0
    finally:
        db.close()


def test_desativar_revoga_o_token(client, vinculado):
    dono, agente = vinculado["dono"], vinculado["agente"]
    assert client.delete("/api/v1/totem/maquinas/TOT-1", headers=dono).status_code == 200
    assert client.get("/api/v1/totem/TOT-1/config", headers=agente).status_code == 401
