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

from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import Cliente, HistoricoOperacao, Maquina, UserRole, Usuario, VendaPagamento
from app.services.maquinas_relatorio import compute_financial_summary
from app.main import app

Base.metadata.create_all(bind=engine)

WINDOW_START = datetime.utcnow() - timedelta(hours=1)
WINDOW_END = datetime.utcnow() + timedelta(hours=1)


def _create_maquina(id_hardware, cliente_id=None):
    db = SessionLocal()
    try:
        if not db.query(Maquina).filter(Maquina.id_hardware == id_hardware).first():
            db.add(Maquina(id_hardware=id_hardware, nome_local=id_hardware, cliente_id=cliente_id))
            db.commit()
    finally:
        db.close()


def _add_venda(machine_id, **kwargs):
    db = SessionLocal()
    try:
        defaults = dict(
            maquina_id=machine_id,
            origem="digital",
            provider="mercado_pago",
            valor_bruto=kwargs.get("valor_liquido", 0.0),
            valor_liquido=0.0,
            conta_faturamento=True,
            conta_ticket_medio=True,
            created_at=datetime.utcnow(),
        )
        defaults.update(kwargs)
        venda = VendaPagamento(**defaults)
        db.add(venda)
        db.commit()
        return venda.id
    finally:
        db.close()


def _add_teste_historico(machine_id, valor, created_at=None):
    db = SessionLocal()
    try:
        db.add(
            HistoricoOperacao(
                maquina_id=machine_id,
                categoria="TESTE",
                descricao=f"Pagamento de teste enviado pelo painel no valor de R$ {valor:.2f}",
                valor=valor,
                pulse_status="pendente",
                created_at=created_at or datetime.utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()


def _summary(machine_ids, start=WINDOW_START, end=WINDOW_END):
    db = SessionLocal()
    try:
        return compute_financial_summary(db, machine_ids, start, end)
    finally:
        db.close()


def test_compute_financial_summary_breaks_down_fisico_digital_and_ticket_medio():
    machine_id = "CPM-DASH-1"
    _create_maquina(machine_id)
    _add_venda(machine_id, origem="fisico", provider="fisico", valor_liquido=10.0)
    _add_venda(machine_id, origem="pix", provider="mercado_pago", valor_liquido=20.0)

    resumo = _summary([machine_id])

    assert resumo["faturamento_total"] == 30.0
    assert resumo["faturamento_fisico"] == 10.0
    assert resumo["faturamento_digital"] == 20.0
    assert resumo["vendas_count"] == 2
    assert resumo["ticket_medio"] == 15.0


def test_compute_financial_summary_counts_testes_separately_from_faturamento():
    # Credito de teste (botao "Credito" de teste no painel) so grava em
    # HistoricoOperacao categoria=TESTE - nunca cria uma VendaPagamento - entao nao
    # deve entrar no faturamento real, so no contador de testes.
    machine_id = "CPM-DASH-2"
    _create_maquina(machine_id)
    _add_venda(machine_id, valor_liquido=40.0, conta_faturamento=True, conta_ticket_medio=True)
    _add_teste_historico(machine_id, 5.0)

    resumo = _summary([machine_id])

    assert resumo["faturamento_total"] == 40.0
    assert resumo["testes_count"] == 1
    assert resumo["testes_valor"] == 5.0


def test_compute_financial_summary_counts_estornos_inside_window_only():
    machine_id = "CPM-DASH-3"
    _create_maquina(machine_id)
    _add_venda(machine_id, valor_liquido=15.0, refunded_at=datetime.utcnow())
    _add_venda(machine_id, valor_liquido=8.0, refunded_at=WINDOW_START - timedelta(days=1))

    resumo = _summary([machine_id])

    assert resumo["estornos_count"] == 1
    assert resumo["estornos_valor"] == 15.0


def test_compute_financial_summary_counts_pulsos_ausentes():
    machine_id = "CPM-DASH-4"
    _create_maquina(machine_id)
    _add_venda(machine_id, valor_liquido=12.0, status_pulso="falha_timeout")
    _add_venda(machine_id, valor_liquido=9.0, status_pulso="pulso_confirmado")

    resumo = _summary([machine_id])

    assert resumo["pulsos_ausentes"] == 1


def test_compute_financial_summary_respects_date_window():
    machine_id = "CPM-DASH-5"
    _create_maquina(machine_id)
    _add_venda(machine_id, valor_liquido=100.0, created_at=WINDOW_START - timedelta(days=10))
    _add_venda(machine_id, valor_liquido=7.0, created_at=datetime.utcnow())

    resumo = _summary([machine_id])

    assert resumo["faturamento_total"] == 7.0


def test_compute_financial_summary_returns_zero_summary_for_empty_machine_list():
    resumo = _summary([])

    assert resumo["faturamento_total"] == 0.0
    assert resumo["vendas_count"] == 0
    assert resumo["ticket_medio"] == 0.0
    assert resumo["pulsos_ausentes"] == 0


def _create_admin_and_token(client):
    db = SessionLocal()
    try:
        db.add(
            Usuario(
                email="admin-dashboard@test.local",
                hashed_password=get_password_hash("123456"),
                role=UserRole.admin,
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.post(
        "/api/v1/login",
        data={"username": "admin-dashboard@test.local", "password": "123456"},
    )
    return response.json()["access_token"]


def test_dashboard_overview_endpoint_returns_financial_breakdown():
    with TestClient(app) as client:
        token = _create_admin_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        db = SessionLocal()
        try:
            cliente = Cliente(
                nome_empresa="Cliente Dashboard Teste",
                email_contato="cliente-dashboard@test.local",
                api_key="api-key-dashboard-teste",
            )
            db.add(cliente)
            db.commit()
            db.refresh(cliente)
            cliente_id = cliente.id
        finally:
            db.close()

        machine_id = "CPM-DASH-INTEGRATION"
        _create_maquina(machine_id, cliente_id=cliente_id)
        _add_venda(machine_id, origem="fisico", provider="fisico", valor_liquido=25.0)

        response = client.get("/api/v1/dashboard/overview?periodo=mes", headers=headers)

    assert response.status_code == 200
    data = response.json()
    stats = data["stats"]
    for key in [
        "faturamento_total",
        "faturamento_hoje",
        "faturamento_mes",
        "faturamento_digital",
        "total_fisico",
        "ticket_medio",
        "testes_count",
        "testes_valor",
        "estornos_count",
        "estornos_valor",
        "pulsos_ausentes",
    ]:
        assert key in stats

    assert stats["faturamento_hoje"] >= 25.0
    assert stats["faturamento_mes"] >= 25.0

    maquinas_resumo = data["maquinas_resumo"]
    assert any(item["id_hardware"] == machine_id for item in maquinas_resumo)
    alvo = next(item for item in maquinas_resumo if item["id_hardware"] == machine_id)
    assert alvo["total_faturado"] >= 25.0
    assert alvo["cliente_nome"] == "Cliente Dashboard Teste"

    clientes_resumo = data["clientes_resumo"]
    assert any(item["cliente_id"] == cliente_id for item in clientes_resumo)
