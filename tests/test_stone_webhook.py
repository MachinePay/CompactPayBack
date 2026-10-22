"""Webhook charge.paid do Connect Stone: venda digitada direto na maquininha
chega com metadata.terminal_serial_number e credita a maquina desse serial.
A cobranca e' sempre reconsultada na API da Stone (o corpo nao e' confiavel)."""

from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import Cliente, HistoricoOperacao, Maquina, VendaPagamento
import app.services.stone_webhook as webhook
from app.services.stone import StoneAPIError

Base.metadata.create_all(bind=engine)


def _charge(charge_id, serial, status="paid", amount=500):
    return {
        "id": charge_id,
        "status": status,
        "amount": amount,
        "paid_amount": amount,
        "payment_method": "credit_card",
        "metadata": {"scheme_name": "MasterCard", "account_funding_source": "debit", "terminal_serial_number": serial},
    }


def _preparar(monkeypatch, sufixo, serial, charges_por_chave, creditos):
    db = SessionLocal()
    try:
        cliente = Cliente(
            nome_empresa=f"Stone {sufixo}",
            email_contato=f"stone-{sufixo}@teste.com",
            api_key=f"api-stone-{sufixo}",
            cliente_stone=True,
            stone_secret_key=f"sk_{sufixo}",
        )
        db.add(cliente)
        db.flush()
        db.add(
            Maquina(
                id_hardware=f"CPM-STONE-{sufixo}",
                nome_local=sufixo,
                cliente_id=cliente.id,
                banco_pagamento="stone",
                stone_serial_number=serial,
            )
        )
        db.commit()
    finally:
        db.close()

    def fake_get_charge(secret_key, charge_id, referer=None):
        charge = charges_por_chave.get(secret_key, {}).get(charge_id)
        if charge is None:
            raise StoneAPIError(404, "not found")
        return charge

    monkeypatch.setattr(webhook, "_acquire_payment_lock", lambda db, key: None)  # pg_advisory_lock nao existe no SQLite
    monkeypatch.setattr(webhook, "get_charge", fake_get_charge)
    monkeypatch.setattr(webhook, "publish_machine_credit_pulses", lambda machine_id, **k: creditos.append((machine_id, k["amount"])))
    monkeypatch.setattr(webhook, "get_command_status", lambda command_id: "pendente")


def _webhook(charge_id, serial, tipo="charge.paid"):
    return {"type": tipo, "data": {"id": charge_id, "metadata": {"terminal_serial_number": serial}}}


def test_venda_na_maquininha_credita_a_maquina_do_serial(monkeypatch):
    creditos = []
    _preparar(monkeypatch, "A", "6N0000A1", {"sk_A": {"ch_A1": _charge("ch_A1", "6N0000A1", amount=500)}}, creditos)

    resultado = webhook.processar_callback_stone(_webhook("ch_A1", "6N0000A1"))

    assert resultado["status"] == "sucesso"
    assert creditos == [("CPM-STONE-A", 5.0)]
    db = SessionLocal()
    try:
        venda = db.query(VendaPagamento).filter(VendaPagamento.provider_payment_id == "ch_A1").one()
        assert (venda.provider, venda.valor_bruto, venda.tipo_pagamento, venda.bandeira_cartao) == ("stone", 5.0, "debit", "MasterCard")
    finally:
        db.close()


def test_webhook_repetido_nao_credita_duas_vezes(monkeypatch):
    creditos = []
    _preparar(monkeypatch, "B", "6N0000B1", {"sk_B": {"ch_B1": _charge("ch_B1", "6N0000B1")}}, creditos)

    webhook.processar_callback_stone(_webhook("ch_B1", "6N0000B1"))
    resultado = webhook.processar_callback_stone(_webhook("ch_B1", "6N0000B1"))

    assert resultado["status"] == "ignorado"
    assert len(creditos) == 1


def test_corpo_falso_nao_credita_sem_cobranca_paga_na_api(monkeypatch):
    creditos = []
    _preparar(monkeypatch, "C", "6N0000C1", {"sk_C": {"ch_C1": _charge("ch_C1", "6N0000C1", status="pending")}}, creditos)

    pendente = webhook.processar_callback_stone(_webhook("ch_C1", "6N0000C1"))
    inexistente = webhook.processar_callback_stone(_webhook("ch_NAO_EXISTE", "6N0000C1"))

    assert pendente["status"] == "ignorado"
    assert inexistente["status"] == "ignorado"
    assert creditos == []


def test_maquininha_fora_do_compactpay_e_ignorada(monkeypatch):
    creditos = []
    _preparar(monkeypatch, "D", "6N0000D1", {"sk_D": {"ch_D1": _charge("ch_D1", "OUTRA-MAQUININHA")}}, creditos)

    resultado = webhook.processar_callback_stone(_webhook("ch_D1", "OUTRA-MAQUININHA"))

    assert resultado["status"] == "ignorado"
    assert creditos == []


def test_charge_refunded_marca_estorno(monkeypatch):
    creditos = []
    charges = {"sk_E": {"ch_E1": _charge("ch_E1", "6N0000E1")}}
    _preparar(monkeypatch, "E", "6N0000E1", charges, creditos)
    webhook.processar_callback_stone(_webhook("ch_E1", "6N0000E1"))

    charges["sk_E"]["ch_E1"] = _charge("ch_E1", "6N0000E1", status="refunded")
    resultado = webhook.processar_callback_stone(_webhook("ch_E1", "6N0000E1", tipo="charge.refunded"))

    assert resultado["status"] == "sucesso"
    db = SessionLocal()
    try:
        historico = db.query(HistoricoOperacao).filter(HistoricoOperacao.provider_payment_id == "ch_E1").one()
        venda = db.query(VendaPagamento).filter(VendaPagamento.provider_payment_id == "ch_E1").one()
        assert historico.refunded_at is not None and venda.refunded_at is not None
    finally:
        db.close()
