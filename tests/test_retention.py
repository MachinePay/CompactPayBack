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

from app.core.config import settings
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import EventoTipo, HistoricoOperacao, Maquina, MetodoPagamento, Transacao, VendaPagamento
import app.models.models  # noqa: F401
import app.models.produto  # noqa: F401
from app.services.retention import purge_old_device_status_history

Base.metadata.create_all(bind=engine)


def _create_maquina(id_hardware):
    db = SessionLocal()
    try:
        if not db.query(Maquina).filter(Maquina.id_hardware == id_hardware).first():
            db.add(Maquina(id_hardware=id_hardware, nome_local="Maquina retencao"))
            db.commit()
    finally:
        db.close()


def test_purge_removes_only_device_status_rows_older_than_retention_window():
    machine_id = "CPM-RETAIN-1"
    _create_maquina(machine_id)
    old_cutoff = datetime.utcnow() - timedelta(days=settings.DEVICE_STATUS_RETENTION_DAYS + 5)
    recent = datetime.utcnow() - timedelta(days=1)

    db = SessionLocal()
    try:
        old_status = HistoricoOperacao(
            maquina_id=machine_id,
            categoria="DISPOSITIVO",
            descricao="Evento antigo de status",
            created_at=old_cutoff,
        )
        recent_status = HistoricoOperacao(
            maquina_id=machine_id,
            categoria="DISPOSITIVO",
            descricao="Evento recente de status",
            created_at=recent,
        )
        old_pagamento = HistoricoOperacao(
            maquina_id=machine_id,
            categoria="PAGAMENTO",
            descricao="Pagamento antigo (auditoria financeira)",
            created_at=old_cutoff,
        )
        db.add_all([old_status, recent_status, old_pagamento])
        db.commit()
        old_status_id = old_status.id
        recent_status_id = recent_status.id
        old_pagamento_id = old_pagamento.id
    finally:
        db.close()

    deleted = purge_old_device_status_history()
    assert deleted >= 1

    db = SessionLocal()
    try:
        assert db.query(HistoricoOperacao).filter(HistoricoOperacao.id == old_status_id).first() is None
        assert db.query(HistoricoOperacao).filter(HistoricoOperacao.id == recent_status_id).first() is not None
        # Categoria PAGAMENTO nunca e apagada por essa rotina, mesmo estando fora da janela.
        assert db.query(HistoricoOperacao).filter(HistoricoOperacao.id == old_pagamento_id).first() is not None
    finally:
        db.close()


def test_purge_never_touches_transacoes_or_vendas_pagamentos_no_matter_how_old():
    machine_id = "CPM-RETAIN-2"
    _create_maquina(machine_id)
    ancient = datetime.utcnow() - timedelta(days=3650)

    db = SessionLocal()
    try:
        transacao = Transacao(
            maquina_id=machine_id,
            tipo=EventoTipo.in_flux,
            metodo=MetodoPagamento.fisico,
            valor=1.0,
            data_hora=ancient,
        )
        db.add(transacao)
        db.flush()
        venda = VendaPagamento(
            maquina_id=machine_id,
            origem="fisico",
            provider="fisico",
            valor_bruto=1.0,
            valor_liquido=1.0,
            transacao_id=transacao.id,
            created_at=ancient,
        )
        db.add(venda)
        db.commit()
        transacao_id = transacao.id
        venda_id = venda.id
    finally:
        db.close()

    purge_old_device_status_history()

    db = SessionLocal()
    try:
        assert db.query(Transacao).filter(Transacao.id == transacao_id).first() is not None
        assert db.query(VendaPagamento).filter(VendaPagamento.id == venda_id).first() is not None
    finally:
        db.close()


def test_purge_keeps_device_status_rows_inside_retention_window():
    machine_id = "CPM-RETAIN-3"
    _create_maquina(machine_id)
    inside_window = datetime.utcnow() - timedelta(days=settings.DEVICE_STATUS_RETENTION_DAYS - 5)

    db = SessionLocal()
    try:
        status_row = HistoricoOperacao(
            maquina_id=machine_id,
            categoria="DISPOSITIVO",
            descricao="Evento dentro da janela de retencao",
            created_at=inside_window,
        )
        db.add(status_row)
        db.commit()
        status_id = status_row.id
    finally:
        db.close()

    purge_old_device_status_history()

    db = SessionLocal()
    try:
        assert db.query(HistoricoOperacao).filter(HistoricoOperacao.id == status_id).first() is not None
    finally:
        db.close()
