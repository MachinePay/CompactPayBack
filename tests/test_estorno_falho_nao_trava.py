import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["START_SUMUP_POLLER_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import Cliente, ComandoMaquina, HistoricoOperacao, Maquina, VendaPagamento
from app.services import command_queue, pagamentos_helpers, pulse_tracking

Base.metadata.create_all(bind=engine)


def _pagamento(machine_id, command_id):
    db = SessionLocal()
    try:
        cliente = Cliente(
            nome_empresa=f"Cli {machine_id}", email_contato=f"{machine_id}@t.test", api_key=f"k-{machine_id}",
            sumup_api_key="sup_sk_x", sumup_merchant_code="MCX",
        )
        db.add(cliente)
        db.commit()
        db.add(Maquina(id_hardware=machine_id, nome_local="Garra", cliente_id=cliente.id, banco_pagamento="sumup"))
        hist = HistoricoOperacao(
            maquina_id=machine_id, categoria="PAGAMENTO", descricao="Pix SumUp", valor=1.0,
            provider="sumup", provider_payment_id="txn-1", command_id=command_id, pulse_status="pendente",
        )
        db.add(hist)
        db.commit()
        db.add(VendaPagamento(
            maquina_id=machine_id, historico_id=hist.id, origem="sumup", provider="sumup", valor_bruto=1.0,
            valor_liquido=1.0, status_pulso="pendente", command_id=command_id,
        ))
        db.add(ComandoMaquina(
            command_id=command_id, maquina_id=machine_id, tipo="paid", topic="t", payload="p",
            status="falhou", detalhe_status="retry_esgotado", tentativas=3, finished_at=datetime.utcnow(),
        ))
        db.commit()
    finally:
        db.close()


def _status(command_id):
    db = SessionLocal()
    try:
        venda = db.query(VendaPagamento).filter(VendaPagamento.command_id == command_id).first()
        hist = db.query(HistoricoOperacao).filter(HistoricoOperacao.command_id == command_id).first()
        return venda.status_pulso, hist.pulse_status, hist.descricao, hist.refunded_at
    finally:
        db.close()


def test_estorno_que_falha_nao_deixa_pagamento_pendente(monkeypatch):
    _pagamento("EF-1", "cmd-ef-1")

    def recusa(*args, **kwargs):
        raise HTTPException(status_code=422, detail="A SumUp nao permite devolver Pix pelo sistema.")

    monkeypatch.setattr(pagamentos_helpers, "create_sumup_refund", recusa)
    pulse_tracking.update_pulse_status("cmd-ef-1", "falha_dispositivo_offline")
    venda, hist, descricao, refunded_at = _status("cmd-ef-1")
    assert venda == hist == "falha_dispositivo_offline"
    assert "Estorno automatico nao feito" in descricao
    assert refunded_at is None


def test_corrige_pagamento_preso_sem_tentar_estornar(monkeypatch):
    _pagamento("EF-2", "cmd-ef-2")
    chamadas = []
    monkeypatch.setattr(pagamentos_helpers, "create_sumup_refund", lambda *a, **k: chamadas.append(a))
    assert command_queue.corrigir_pagamentos_presos_em_comando_falho() >= 1
    venda, hist, _, _ = _status("cmd-ef-2")
    assert venda == hist == "falha_dispositivo_offline"
    assert chamadas == []
