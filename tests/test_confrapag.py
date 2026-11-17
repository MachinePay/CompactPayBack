"""Banco "confrapag_sdk": rotas /confrapag usadas pelo app CompactPay na maquininha PAX.

O MQTT e' simulado. O que importa aqui:
- a chave do terminal so enxerga maquinas confrapag_sdk do proprio cliente;
- so valores cadastrados para a maquina e referencias recentes desta placa liberam credito;
- o mesmo TID nunca libera credito duas vezes.
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.confrapag import TerminalConfrapag  # noqa: F401 - registra a tabela
from app.models.models import Cliente, Maquina, UserRole, Usuario, VendaPagamento
from app.models.produto import Produto
from app.services import confrapag as svc

Base.metadata.create_all(bind=engine)

CLIENTE_A = CLIENTE_B = None


def _setup():
    agora = datetime.utcnow()
    db = SessionLocal()
    try:
        a = Cliente(nome_empresa="Loja A", email_contato="a@confrapag.test", api_key="k-cf-a", cnpj="12.345.678/0001-90")
        b = Cliente(nome_empresa="Loja B", email_contato="b@confrapag.test", api_key="k-cf-b",
                    cliente_confrapag=True)
        db.add_all([a, b])
        db.commit()
        db.add_all([
            Maquina(id_hardware="CF-PAX", nome_local="Grua 1", cliente_id=a.id,
                    banco_pagamento="confrapag_sdk", ultimo_sinal=agora),
            Maquina(id_hardware="CF-PAX-2", nome_local="Grua 2", cliente_id=a.id,
                    banco_pagamento="confrapag_sdk", ultimo_sinal=agora),
            Maquina(id_hardware="CF-APK", nome_local="Grua APK", cliente_id=a.id,
                    banco_pagamento="mercado_pago_apk", ultimo_sinal=agora),
            Maquina(id_hardware="CF-PAX-B", nome_local="Grua B", cliente_id=b.id,
                    banco_pagamento="confrapag_sdk", ultimo_sinal=agora),
        ])
        db.commit()
        db.add(Produto(nome="Jogada", valor=7.5, maquina_id="CF-PAX"))
        db.add(Usuario(email="admin@confrapag.test", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.add(Usuario(email="dono-b@confrapag.test", hashed_password=get_password_hash("123456"),
                       role=UserRole.cliente, cliente_id=b.id))
        db.commit()
        return a.id, b.id
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def dados_confrapag():
    global CLIENTE_A, CLIENTE_B
    CLIENTE_A, CLIENTE_B = _setup()
    yield


@pytest.fixture
def mqtt(monkeypatch):
    pulsos = []
    monkeypatch.setattr(svc, "_acquire_payment_lock", lambda db, key: None)  # pg_advisory_lock nao existe no SQLite
    monkeypatch.setattr(svc, "publish_machine_credit_pulses", lambda machine_id, **kw: pulsos.append((machine_id, kw)))
    monkeypatch.setattr(svc, "get_command_status", lambda command_id: "enviado")
    return pulsos


def _login(client, email):
    token = client.post("/api/v1/login", data={"username": email, "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _chave(client, cliente_id=None, nome="PAX balcao", cnpj=None):
    admin = _login(client, "admin@confrapag.test")
    body = {"nome": nome, "cliente_id": cliente_id or CLIENTE_A}
    if cnpj is not None:
        body["cnpj"] = cnpj
    resp = client.post("/api/v1/confrapag/terminais", json=body, headers=admin)
    assert resp.status_code == 200, resp.text
    return {"X-Terminal-Key": resp.json()["chave"]}


def _ref(maquina, atraso=timedelta(0)):
    return f"CP-{maquina}-{int((time.time() - atraso.total_seconds()) * 1000)}"


def _pagar(client, headers, maquina="CF-PAX", tid="TID-1", ref=None, valor=7.5):
    return client.post(
        "/api/v1/confrapag/pagamentos",
        json={"maquina_id": maquina, "valor": valor, "tid": tid, "nsu": "123", "referencia": ref or _ref(maquina),
              "metodo": "credito", "bandeira": "VISA", "autorizacao": "A1", "sn_terminal": "A920-1"},
        headers=headers,
    )


# ---- Chave e CNPJ -----------------------------------------------------------

def test_chave_usa_cnpj_do_cliente_e_fica_so_o_hash():
    with TestClient(app) as client:
        headers = _chave(client, nome="PAX cnpj")
        info = client.get("/api/v1/confrapag/terminal", headers=headers).json()
        assert info["cnpj"] == "12345678000190"
        lista = client.get("/api/v1/confrapag/terminais", headers=_login(client, "admin@confrapag.test")).json()
        assert all("chave" not in t for t in lista)


def test_terminal_sem_cnpj_e_recusado():
    with TestClient(app) as client:
        resp = client.post("/api/v1/confrapag/terminais", json={"nome": "sem cnpj", "cliente_id": CLIENTE_B},
                           headers=_login(client, "admin@confrapag.test"))
        assert resp.status_code == 422
        # Com o CNPJ informado na hora, passa.
        _chave(client, cliente_id=CLIENTE_B, cnpj="98.765.432/0001-10")


def test_cliente_nao_cria_chave_para_outro_cliente():
    with TestClient(app) as client:
        dono_b = _login(client, "dono-b@confrapag.test")
        resp = client.post("/api/v1/confrapag/terminais",
                           json={"nome": "x", "cliente_id": CLIENTE_A, "cnpj": "11222333000181"}, headers=dono_b)
        assert resp.status_code == 200
        assert resp.json()["cliente_id"] == CLIENTE_B


def test_sem_chave_ou_revogada_da_401():
    with TestClient(app) as client:
        assert client.get("/api/v1/confrapag/maquinas/CF-PAX").status_code == 401
        headers = _chave(client, nome="PAX revogar")
        lista = client.get("/api/v1/confrapag/terminais", headers=_login(client, "admin@confrapag.test")).json()
        terminal_id = next(t["id"] for t in lista if t["nome"] == "PAX revogar")
        client.delete(f"/api/v1/confrapag/terminais/{terminal_id}", headers=_login(client, "admin@confrapag.test"))
        assert client.get("/api/v1/confrapag/maquinas/CF-PAX", headers=headers).status_code == 401


def test_so_enxerga_maquinas_confrapag_do_proprio_cliente():
    with TestClient(app) as client:
        headers = _chave(client)
        resp = client.get("/api/v1/confrapag/maquinas/CF-PAX", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["produtos"] == [{"id": resp.json()["produtos"][0]["id"], "nome": "Jogada", "valor": 7.5}]
        # Sem produto cadastrado: valores padrao.
        padrao = client.get("/api/v1/confrapag/maquinas/CF-PAX-2", headers=headers).json()
        assert [p["valor"] for p in padrao["produtos"]] == [2.0, 5.0, 10.0, 20.0]
        assert client.get("/api/v1/confrapag/maquinas/CF-APK", headers=headers).status_code == 404
        assert client.get("/api/v1/confrapag/maquinas/CF-PAX-B", headers=headers).status_code == 404


# ---- Pagamento --------------------------------------------------------------

def test_pagamento_libera_pulso_uma_vez_so(mqtt):
    with TestClient(app) as client:
        headers = _chave(client)
        primeiro = _pagar(client, headers, tid="TID-UNICO")
        assert primeiro.status_code == 200, primeiro.text
        assert primeiro.json()["duplicado"] is False
        assert primeiro.json()["pulsos"] == 7
        segundo = _pagar(client, headers, tid="TID-UNICO")
        assert segundo.status_code == 200
        assert segundo.json()["duplicado"] is True
        assert segundo.json()["command_id"] == primeiro.json()["command_id"]
        assert len(mqtt) == 1
        db = SessionLocal()
        try:
            venda = db.query(VendaPagamento).filter(VendaPagamento.provider_payment_id == "TID-UNICO").one()
            assert venda.provider == "confrapag" and venda.tipo_pagamento == "credit_card"
        finally:
            db.close()


def test_mesmo_tid_em_outra_maquina_e_recusado(mqtt):
    with TestClient(app) as client:
        headers = _chave(client)
        assert _pagar(client, headers, tid="TID-DUAS").status_code == 200
        resp = _pagar(client, headers, maquina="CF-PAX-2", tid="TID-DUAS", valor=5.0)
        assert resp.status_code == 409


def test_valor_fora_dos_produtos_e_recusado(mqtt):
    with TestClient(app) as client:
        headers = _chave(client)
        assert _pagar(client, headers, tid="TID-VALOR", valor=100.0).status_code == 422
        assert _pagar(client, headers, maquina="CF-PAX-2", tid="TID-VALOR-2", valor=5.0).status_code == 200
        assert len(mqtt) == 1


def test_referencia_de_outra_placa_ou_antiga_e_recusada(mqtt):
    with TestClient(app) as client:
        headers = _chave(client)
        assert _pagar(client, headers, tid="TID-R1", ref=_ref("CF-PAX-2")).status_code == 422
        assert _pagar(client, headers, tid="TID-R2", ref=_ref("CF-PAX", timedelta(hours=3))).status_code == 409
        assert mqtt == []


def test_painel_cria_maquina_confrapag_sem_token_e_sem_caixa_mp(monkeypatch):
    import app.api.v1.endpoints.maquinas as maquinas_endpoint

    def nao_pode_chamar(*args, **kwargs):
        raise AssertionError("banco confrapag_sdk nao cria loja/caixa no Mercado Pago")

    for nome in dir(maquinas_endpoint):
        if nome.startswith("create_") and "pos" in nome or nome in {"ensure_cliente_store", "create_store_for_machine"}:
            monkeypatch.setattr(maquinas_endpoint, nome, nao_pode_chamar)

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/maquinas",
            json={"id_hardware": "CF-NOVA", "nome": "Nova PAX", "cliente_id": CLIENTE_B, "banco_pagamento": "confrapag_sdk"},
            headers=_login(client, "admin@confrapag.test"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["banco_pagamento"] == "confrapag_sdk"


def test_banco_confrapag_so_para_cliente_marcado():
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/maquinas",
            json={"id_hardware": "CF-SEM-FLAG", "nome": "Sem flag", "cliente_id": CLIENTE_A, "banco_pagamento": "confrapag_sdk"},
            headers=_login(client, "admin@confrapag.test"),
        )
        assert resp.status_code == 422


def test_checkbox_cliente_confrapag_salva_no_cliente():
    with TestClient(app) as client:
        admin = _login(client, "admin@confrapag.test")
        resp = client.post("/api/v1/usuarios", json={
            "email": "novo-confrapag@example.com", "password": "Senha@12345", "role": "cliente",
            "nome": "Novo", "cliente_confrapag": True,
        }, headers=admin)
        assert resp.status_code == 200, resp.text
        clientes = client.get("/api/v1/clientes", headers=admin).json()
        novo = next(c for c in clientes if c.get("cliente_confrapag"))
        assert novo["cliente_confrapag"] is True


def test_valores_salvos_na_maquininha_viram_produtos_e_travam_o_pagamento(mqtt):
    with TestClient(app) as client:
        headers = _chave(client, cliente_id=CLIENTE_B, cnpj="98765432000110")
        resp = client.put("/api/v1/confrapag/maquinas/CF-PAX-B/valores", json={"valores": [3, 1.5, 3]}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert [p["valor"] for p in resp.json()["produtos"]] == [1.5, 3.0]
        maquina = client.get("/api/v1/confrapag/maquinas/CF-PAX-B", headers=headers).json()
        assert [p["valor"] for p in maquina["produtos"]] == [1.5, 3.0]
        assert _pagar(client, headers, maquina="CF-PAX-B", tid="TID-V1", valor=5.0).status_code == 422
        assert _pagar(client, headers, maquina="CF-PAX-B", tid="TID-V2", valor=3.0).status_code == 200
        # Valor invalido nao apaga o que ja estava salvo.
        assert client.put("/api/v1/confrapag/maquinas/CF-PAX-B/valores", json={"valores": [0]}, headers=headers).status_code == 422
        # Outra maquina de outro cliente: 404.
        assert client.put("/api/v1/confrapag/maquinas/CF-PAX/valores", json={"valores": [2]}, headers=headers).status_code == 404


def test_pagamento_de_teste_so_com_chave_em_modo_teste(mqtt):
    with TestClient(app) as client:
        admin = _login(client, "admin@confrapag.test")
        resp = client.post("/api/v1/confrapag/terminais", json={"nome": "PAX teste", "cliente_id": CLIENTE_A}, headers=admin)
        headers = {"X-Terminal-Key": resp.json()["chave"]}
        terminal_id = resp.json()["id"]

        def pagar_teste(tid):
            return client.post("/api/v1/confrapag/pagamentos", json={
                "maquina_id": "CF-PAX", "valor": 7.5, "tid": tid, "referencia": _ref("CF-PAX"),
                "metodo": "teste", "teste": True}, headers=headers)

        assert pagar_teste("REF-1").status_code == 403
        assert mqtt == []
        # Cliente comum nao liga o modo teste; admin liga.
        dono = _login(client, "dono-b@confrapag.test")
        assert client.patch(f"/api/v1/confrapag/terminais/{terminal_id}", json={"modo_teste": True}, headers=dono).status_code == 403
        assert client.patch(f"/api/v1/confrapag/terminais/{terminal_id}", json={"modo_teste": True}, headers=admin).json()["modo_teste"] is True

        resp = pagar_teste("REF-2")
        assert resp.status_code == 200, resp.text
        assert resp.json()["tid"] == "TESTE-REF-2" and resp.json()["pulsos"] == 7
        assert len(mqtt) == 1
        db = SessionLocal()
        try:
            venda = db.query(VendaPagamento).filter(VendaPagamento.provider_payment_id == "TESTE-REF-2").one()
            assert venda.is_teste is True and venda.conta_faturamento is False
        finally:
            db.close()
        # Mesmo em modo teste, valor fora da lista nao passa.
        assert client.post("/api/v1/confrapag/pagamentos", json={
            "maquina_id": "CF-PAX", "valor": 50, "tid": "REF-3", "referencia": _ref("CF-PAX"), "teste": True},
            headers=headers).status_code == 422


def test_produtos_com_nome_salvos_na_maquininha(mqtt):
    with TestClient(app) as client:
        headers = _chave(client, cliente_id=CLIENTE_B, cnpj="98765432000110")
        resp = client.put("/api/v1/confrapag/maquinas/CF-PAX-B/valores", json={"produtos": [
            {"nome": "3 JOGADAS", "valor": 5}, {"nome": "1 JOGADA", "valor": 2}, {"nome": "", "valor": 10}]}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert [(p["nome"], p["valor"]) for p in resp.json()["produtos"]] == [("1 JOGADA", 2.0), ("3 JOGADAS", 5.0), ("R$ 10,00", 10.0)]
        assert client.put("/api/v1/confrapag/maquinas/CF-PAX-B/valores", json={"produtos": []}, headers=headers).status_code == 422
        r = client.post("/api/v1/confrapag/pagamentos", json={
            "maquina_id": "CF-PAX-B", "valor": 2, "tid": "TID-PIX-1", "referencia": _ref("CF-PAX-B"), "metodo": "pix"}, headers=headers)
        assert r.status_code == 200, r.text


# ---- Pix (Confrapix simulada) ------------------------------------------------

_PROXIMO_ID = 500

@pytest.fixture
def confrapix(monkeypatch, mqtt):
    from app.services import confrapix as api

    class Fake:
        transacoes: dict = {}
        tokens: list = []
        canceladas: list = []
        proximo = 500

    fake = Fake()
    fake.transacoes, fake.tokens, fake.canceladas = {}, [], []
    global _PROXIMO_ID
    _PROXIMO_ID += 100
    fake.proximo = _PROXIMO_ID  # ids unicos entre testes (o banco SQLite e' compartilhado)

    def criar_cobranca(token, valor, descricao, expiracao, callback_url):
        fake.tokens.append(token)
        fake.proximo += 1
        t = {"id": fake.proximo, "status": "processing", "amount": valor,
             "pix": {"code": f"000201PIX{fake.proximo}", "txid": f"tx{fake.proximo}"}, "callback": callback_url}
        fake.transacoes[str(fake.proximo)] = t
        return t

    def consultar(token, transacao_id):
        fake.tokens.append(token)
        return fake.transacoes[str(transacao_id)]

    def cancelar(token, transacao_id):
        fake.canceladas.append(str(transacao_id))
        fake.transacoes[str(transacao_id)]["status"] = "canceled"
        return fake.transacoes[str(transacao_id)]

    monkeypatch.setattr(api, "criar_cobranca", criar_cobranca)
    monkeypatch.setattr(api, "consultar", consultar)
    monkeypatch.setattr(api, "cancelar", cancelar)
    fake.pulsos = mqtt
    return fake


def _token_confrapix(cliente_id, token):
    db = SessionLocal()
    try:
        db.query(Cliente).filter(Cliente.id == cliente_id).update({Cliente.confrapix_token: token})
        db.commit()
    finally:
        db.close()


def test_pix_sem_token_nao_aparece_nem_cria(confrapix):
    with TestClient(app) as client:
        _token_confrapix(CLIENTE_A, None)
        headers = _chave(client)
        assert client.get("/api/v1/confrapag/maquinas/CF-PAX", headers=headers).json()["pix_habilitado"] is False
        resp = client.post("/api/v1/confrapag/pix", json={"maquina_id": "CF-PAX", "valor": 7.5, "referencia": _ref("CF-PAX")}, headers=headers)
        assert resp.status_code == 422
        assert confrapix.transacoes == {}


def test_pix_pago_libera_pulso_uma_vez_com_token_do_cliente(confrapix):
    with TestClient(app) as client:
        _token_confrapix(CLIENTE_A, "TOKEN-CONFRAPIX-A")
        headers = _chave(client)
        assert client.get("/api/v1/confrapag/maquinas/CF-PAX", headers=headers).json()["pix_habilitado"] is True
        criado = client.post("/api/v1/confrapag/pix", json={"maquina_id": "CF-PAX", "valor": 7.5, "referencia": _ref("CF-PAX")}, headers=headers)
        assert criado.status_code == 200, criado.text
        corpo = criado.json()
        assert corpo["codigo"].startswith("000201") and corpo["pago"] is False
        cid = corpo["id"]
        assert client.get(f"/api/v1/confrapag/pix/{cid}", headers=headers).json()["pago"] is False
        assert confrapix.pulsos == []

        confrapix.transacoes[str(confrapix.proximo)]["status"] = "succeeded"
        pago = client.get(f"/api/v1/confrapag/pix/{cid}", headers=headers).json()
        assert pago["pago"] is True and pago["command_id"]
        # Webhook e nova consulta nao liberam de novo.
        assert client.post("/api/v1/confrapag/pix/webhook", json={"id": confrapix.proximo}).status_code == 200
        assert client.get(f"/api/v1/confrapag/pix/{cid}", headers=headers).json()["command_id"] == pago["command_id"]
        assert len(confrapix.pulsos) == 1
        assert set(confrapix.tokens) == {"TOKEN-CONFRAPIX-A"}
        db = SessionLocal()
        try:
            venda = db.query(VendaPagamento).filter(VendaPagamento.provider_payment_id == f"PIX-{confrapix.proximo}").one()
            assert venda.tipo_pagamento == "pix" and venda.valor_bruto == 7.5
        finally:
            db.close()


def test_pix_pago_pelo_webhook_sem_a_maquininha_consultar(confrapix):
    with TestClient(app) as client:
        _token_confrapix(CLIENTE_A, "TOKEN-CONFRAPIX-A")
        headers = _chave(client)
        cid = client.post("/api/v1/confrapag/pix", json={"maquina_id": "CF-PAX", "valor": 7.5, "referencia": _ref("CF-PAX")}, headers=headers).json()["id"]
        # Webhook forjado dizendo "pago" nao libera: so a reconsulta vale.
        client.post("/api/v1/confrapag/pix/webhook", json={"id": confrapix.proximo, "status": "succeeded"})
        assert confrapix.pulsos == []
        confrapix.transacoes[str(confrapix.proximo)]["status"] = "succeeded"
        client.post("/api/v1/confrapag/pix/webhook", json={"id": confrapix.proximo})
        assert len(confrapix.pulsos) == 1
        # Id desconhecido ou corpo invalido: 200 sem quebrar.
        assert client.post("/api/v1/confrapag/pix/webhook", json={"id": 999999}).status_code == 200
        assert client.post("/api/v1/confrapag/pix/webhook", content=b"x").status_code == 200
        assert client.get(f"/api/v1/confrapag/pix/{cid}", headers=headers).json()["pago"] is True


def test_pix_cancelado_e_valor_divergente_nao_liberam(confrapix):
    with TestClient(app) as client:
        _token_confrapix(CLIENTE_A, "TOKEN-CONFRAPIX-A")
        headers = _chave(client)
        cid = client.post("/api/v1/confrapag/pix", json={"maquina_id": "CF-PAX", "valor": 7.5, "referencia": _ref("CF-PAX")}, headers=headers).json()["id"]
        cancelado = client.delete(f"/api/v1/confrapag/pix/{cid}", headers=headers).json()
        assert cancelado["status"] == "canceled" and str(confrapix.proximo) in confrapix.canceladas

        cid2 = client.post("/api/v1/confrapag/pix", json={"maquina_id": "CF-PAX", "valor": 7.5, "referencia": _ref("CF-PAX")}, headers=headers).json()["id"]
        confrapix.transacoes[str(confrapix.proximo)].update(status="succeeded", amount=1.0)
        assert client.get(f"/api/v1/confrapag/pix/{cid2}", headers=headers).json()["pago"] is False
        assert confrapix.pulsos == []
        # Outra chave (outro terminal) nao enxerga a cobranca.
        outra = _chave(client, nome="PAX outra")
        assert client.get(f"/api/v1/confrapag/pix/{cid2}", headers=outra).status_code == 404


# ---- Vinculacao por codigo ---------------------------------------------------

def _gerar_codigo(client, maquina="CF-PAX", cliente_id=None, headers=None):
    headers = headers or _login(client, "admin@confrapag.test")
    return client.post("/api/v1/confrapag/vinculos",
                       json={"maquina_id": maquina, "cliente_id": cliente_id or CLIENTE_A}, headers=headers)


def test_codigo_vincula_maquininha_uma_vez_e_aponta_para_a_placa():
    with TestClient(app) as client:
        resp = _gerar_codigo(client)
        assert resp.status_code == 200, resp.text
        codigo = resp.json()["codigo"]
        assert len(codigo) == 7 and codigo[3] == "-"
        # A PAX digita com ou sem traco, minusculo.
        vinc = client.post("/api/v1/confrapag/vincular", json={"codigo": codigo.replace("-", "").lower(), "sn_terminal": "SN1"})
        assert vinc.status_code == 200, vinc.text
        dados = vinc.json()
        assert dados["placa_id"] == "CF-PAX" and dados["cnpj"] == "12345678000190"
        headers = {"X-Terminal-Key": dados["chave"]}
        assert client.get("/api/v1/confrapag/maquinas/CF-PAX", headers=headers).status_code == 200
        # Uso unico.
        assert client.post("/api/v1/confrapag/vincular", json={"codigo": codigo}).status_code == 404


def test_codigo_expirado_ou_inventado_nao_vincula():
    from app.models.confrapag import VinculoConfrapag

    with TestClient(app) as client:
        codigo = _gerar_codigo(client).json()["codigo"]
        db = SessionLocal()
        try:
            v = db.query(VinculoConfrapag).filter(VinculoConfrapag.codigo == codigo.replace("-", "")).one()
            v.expira_em = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        finally:
            db.close()
        assert client.post("/api/v1/confrapag/vincular", json={"codigo": codigo}).status_code == 404
        assert client.post("/api/v1/confrapag/vincular", json={"codigo": "ZZZ-ZZZ"}).status_code == 404


def test_codigo_so_para_maquina_confrapag_do_proprio_cliente():
    with TestClient(app) as client:
        assert _gerar_codigo(client, maquina="CF-APK").status_code == 404
        assert _gerar_codigo(client, maquina="CF-PAX-B").status_code == 404
        dono_b = _login(client, "dono-b@confrapag.test")
        # Cliente B tentando a maquina do A: usa o proprio cliente_id e nao acha.
        assert _gerar_codigo(client, maquina="CF-PAX", cliente_id=CLIENTE_A, headers=dono_b).status_code == 404


def test_cliente_so_com_cpf_usa_o_cpf():
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            c = Cliente(nome_empresa="Pessoa Fisica", email_contato="pf@confrapag.test", api_key="k-cf-pf",
                        cpf="123.456.789-09", cliente_confrapag=True)
            db.add(c)
            db.commit()
            db.add(Maquina(id_hardware="CF-PF", nome_local="Grua PF", cliente_id=c.id,
                           banco_pagamento="confrapag_sdk", ultimo_sinal=datetime.utcnow()))
            db.commit()
            pf_id = c.id
        finally:
            db.close()
        codigo = _gerar_codigo(client, maquina="CF-PF", cliente_id=pf_id).json()["codigo"]
        assert client.post("/api/v1/confrapag/vincular", json={"codigo": codigo}).json()["cnpj"] == "12345678909"
