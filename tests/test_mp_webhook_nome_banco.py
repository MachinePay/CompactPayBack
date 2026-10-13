"""O nome do banco do cartao so serve para o relatorio: nao pode atrasar o
credito do cliente na maquina (antes ele era buscado no catalogo do Mercado
Pago e no binlist.net antes de liberar o credito, com ate ~40s de espera)."""

from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import EscutaTerminal, HistoricoOperacao, Maquina, VendaPagamento
import app.services.mercado_pago_webhook as webhook
import app.services.pagamentos_helpers as pagamentos_helpers

Base.metadata.create_all(bind=engine)


def _payment_data(payment_id, terminal_id):
    return {
        "id": payment_id,
        "status": "approved",
        "transaction_amount": 2.0,
        "payment_method_id": "debvisa",
        "payment_type_id": "debit_card",
        "issuer_id": "999",
        "card": {"last_four_digits": "1234"},
        "point_of_interaction": {"transaction_data": {"terminal_id": terminal_id}},
    }


def _preparar(monkeypatch, machine_id, terminal_id, payment_id, ordem, nome_banco=None, falha_banco=False):
    db = SessionLocal()
    try:
        if not db.query(Maquina).filter(Maquina.id_hardware == machine_id).first():
            db.add(Maquina(id_hardware=machine_id, nome_local=machine_id))
        if not db.query(EscutaTerminal).filter(EscutaTerminal.terminal_id == terminal_id).first():
            db.add(EscutaTerminal(terminal_id=terminal_id, maquina_id=machine_id, ativo=True))
        db.commit()
    finally:
        db.close()

    def issuer_lookup(*args, **kwargs):
        ordem.append("banco")
        if falha_banco:
            raise RuntimeError("catalogo do Mercado Pago fora do ar")
        return nome_banco

    monkeypatch.setattr(webhook, "_acquire_payment_lock", lambda db, key: None)  # pg_advisory_lock nao existe no SQLite
    monkeypatch.setattr(webhook, "resolve_mp_token_by_user_id", lambda db, user_id: None)
    monkeypatch.setattr(
        webhook,
        "mp_request_with_known_tokens",
        lambda db, method, url, preferred_token=None: (_payment_data(payment_id, terminal_id), "token-teste"),
    )
    monkeypatch.setattr(webhook, "publish_machine_credit_pulses", lambda *a, **k: ordem.append("credito"))
    monkeypatch.setattr(webhook, "get_command_status", lambda command_id: "pendente")
    monkeypatch.setattr(pagamentos_helpers, "resolve_card_issuer_name", issuer_lookup)
    monkeypatch.setattr(pagamentos_helpers, "resolve_bank_name_from_bin", lambda bin_number: None)
    # Roda o preenchimento na mesma thread para o teste ser deterministico.
    monkeypatch.setattr(
        webhook,
        "preencher_nome_banco_em_segundo_plano",
        lambda historico_id, payment_data, token: pagamentos_helpers.preencher_nome_banco(
            historico_id, payment_data, token
        ),
    )


def _venda(payment_id):
    db = SessionLocal()
    try:
        venda = db.query(VendaPagamento).filter(VendaPagamento.provider_payment_id == payment_id).one()
        historico = db.query(HistoricoOperacao).filter(HistoricoOperacao.id == venda.historico_id).one()
        return venda.banco, historico.bank_name
    finally:
        db.close()


def test_credito_sai_antes_da_busca_do_nome_do_banco(monkeypatch):
    ordem = []
    _preparar(monkeypatch, "CPM-MP-BANCO", "T-BANCO", "PAY-BANCO-1", ordem, nome_banco="Banco Teste")

    resultado = webhook.processar_callback_mercado_pago(
        {"type": "payment", "action": "payment.created", "data": {"id": "PAY-BANCO-1"}}
    )

    assert resultado["status"] == "sucesso"
    assert ordem == ["credito", "banco"]
    assert _venda("PAY-BANCO-1") == ("Banco Teste", "Banco Teste")


def test_falha_no_nome_do_banco_nao_impede_o_credito(monkeypatch):
    ordem = []
    _preparar(monkeypatch, "CPM-MP-BANCO-FALHA", "T-BANCO-FALHA", "PAY-BANCO-2", ordem, falha_banco=True)

    resultado = webhook.processar_callback_mercado_pago(
        {"type": "payment", "action": "payment.created", "data": {"id": "PAY-BANCO-2"}}
    )

    assert resultado["status"] == "sucesso"
    assert ordem == ["credito", "banco"]
    assert _venda("PAY-BANCO-2") == (None, None)


def test_payment_metadata_sem_rede_usa_so_pagamento_e_cache(monkeypatch):
    def nao_pode_chamar(*args, **kwargs):
        raise AssertionError("nao pode fazer chamada externa com resolve_bank=False")

    monkeypatch.setattr(pagamentos_helpers, "resolve_card_issuer_name", nao_pode_chamar)
    monkeypatch.setattr(pagamentos_helpers, "resolve_bank_name_from_bin", nao_pode_chamar)
    monkeypatch.setitem(pagamentos_helpers._CARD_ISSUER_NAME_CACHE, ("visa", "777"), "Banco Em Cache")

    com_cache = pagamentos_helpers.payment_metadata(
        {"id": 1, "payment_method_id": "debvisa", "issuer_id": "777"}, "tok", resolve_bank=False
    )
    sem_cache = pagamentos_helpers.payment_metadata(
        {"id": 2, "payment_method_id": "master", "issuer_id": "555"}, "tok", resolve_bank=False
    )

    assert com_cache["bank_name"] == "Banco Em Cache"
    assert sem_cache["bank_name"] is None
