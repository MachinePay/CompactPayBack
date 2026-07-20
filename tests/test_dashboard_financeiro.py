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
from app.models.models import (
    Cliente,
    EventoTipo,
    HistoricoOperacao,
    Maquina,
    MetodoPagamento,
    Transacao,
    UserRole,
    Usuario,
    VendaPagamento,
)
from app.services.maquinas_relatorio import (
    build_machine_history_payload,
    compute_financial_summary,
    compute_financial_summary_by_machine,
    daily_revenue_totals,
    latest_activity_by_machine,
    movement_counts_by_machine,
    sum_financial_summaries,
)
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


def _add_transacao(machine_id, tipo=EventoTipo.in_flux, metodo=MetodoPagamento.fisico, valor=1.0, data_hora=None):
    db = SessionLocal()
    try:
        db.add(
            Transacao(
                maquina_id=machine_id,
                tipo=tipo,
                metodo=metodo,
                valor=valor,
                data_hora=data_hora or datetime.utcnow(),
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


def test_machine_history_payload_includes_physical_transactions_saved_as_enum():
    machine_id = "CPM-DASH-HIST-FISICO"
    _create_maquina(machine_id)
    _add_transacao(machine_id, valor=1.0)

    db = SessionLocal()
    try:
        maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
        payload = build_machine_history_payload(db, maquina, periodo="mes")
    finally:
        db.close()

    vendas_fisicas = [item for item in payload["vendas"] if item["kind"] == "pagamento_fisico"]
    assert len(vendas_fisicas) == 1
    assert vendas_fisicas[0]["provider"] == "fisico"
    assert vendas_fisicas[0]["pulse_status"] == "fisico"
    assert payload["resumo"]["total_fisico"] == 1.0


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


def test_compute_financial_summary_by_machine_matches_per_machine_calls():
    # A versao em lote precisa bater exatamente com chamar compute_financial_summary
    # maquina por maquina - e essa equivalencia que permite trocar o loop de N+1
    # queries por uma unica leva de queries agregadas sem mudar o resultado.
    machine_a = "CPM-DASH-BATCH-A"
    machine_b = "CPM-DASH-BATCH-B"
    _create_maquina(machine_a)
    _create_maquina(machine_b)

    _add_venda(machine_a, origem="fisico", provider="fisico", valor_liquido=12.0)
    _add_venda(machine_a, origem="pix", provider="mercado_pago", valor_liquido=8.0)
    _add_venda(machine_a, valor_liquido=6.0, refunded_at=datetime.utcnow())
    _add_venda(machine_a, valor_liquido=3.0, status_pulso="falha_timeout")
    _add_teste_historico(machine_a, 2.0)

    _add_venda(machine_b, origem="pix", provider="mercado_pago", valor_liquido=50.0)
    _add_teste_historico(machine_b, 9.0)

    machine_ids = [machine_a, machine_b]
    db = SessionLocal()
    try:
        por_lote = compute_financial_summary_by_machine(db, machine_ids, WINDOW_START, WINDOW_END)
    finally:
        db.close()

    for machine_id in machine_ids:
        individual = _summary([machine_id])
        assert por_lote[machine_id] == individual, machine_id


def test_compute_financial_summary_by_machine_returns_empty_dict_for_no_machines():
    db = SessionLocal()
    try:
        assert compute_financial_summary_by_machine(db, [], WINDOW_START, WINDOW_END) == {}
    finally:
        db.close()


def test_sum_financial_summaries_recomputes_ticket_medio_from_combined_totals():
    machine_a = "CPM-DASH-SUM-A"
    machine_b = "CPM-DASH-SUM-B"
    _create_maquina(machine_a)
    _create_maquina(machine_b)

    # Uma maquina com poucas vendas de valor alto e outra com muitas vendas de
    # valor baixo: a media das duas medias seria diferente da media combinada.
    _add_venda(machine_a, valor_liquido=100.0)
    _add_venda(machine_b, valor_liquido=10.0)
    _add_venda(machine_b, valor_liquido=10.0)
    _add_venda(machine_b, valor_liquido=10.0)

    db = SessionLocal()
    try:
        por_lote = compute_financial_summary_by_machine(db, [machine_a, machine_b], WINDOW_START, WINDOW_END)
    finally:
        db.close()

    combinado = sum_financial_summaries([por_lote[machine_a], por_lote[machine_b]])

    assert combinado["faturamento_total"] == 130.0
    assert combinado["vendas_count"] == 4
    assert combinado["ticket_medio"] == 32.5


def test_daily_revenue_totals_buckets_by_calendar_day():
    machine_id = "CPM-DASH-DAILY"
    _create_maquina(machine_id)
    dia_1 = datetime.utcnow().replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=2)
    dia_2 = datetime.utcnow().replace(hour=15, minute=0, second=0, microsecond=0) - timedelta(days=1)
    _add_venda(machine_id, valor_liquido=11.0, created_at=dia_1)
    _add_venda(machine_id, valor_liquido=4.0, created_at=dia_1)
    _add_venda(machine_id, valor_liquido=7.0, created_at=dia_2)

    db = SessionLocal()
    try:
        totais = daily_revenue_totals(
            db, [machine_id], dia_1 - timedelta(hours=1), datetime.utcnow() + timedelta(hours=1)
        )
    finally:
        db.close()

    assert totais[dia_1.date()] == 15.0
    assert totais[dia_2.date()] == 7.0


def test_movement_and_latest_activity_by_machine():
    machine_with_movement = "CPM-DASH-MOV-A"
    machine_without_movement = "CPM-DASH-MOV-B"
    _create_maquina(machine_with_movement)
    _create_maquina(machine_without_movement)

    db = SessionLocal()
    try:
        db.add(
            Transacao(
                maquina_id=machine_with_movement,
                tipo=EventoTipo.in_flux,
                metodo=MetodoPagamento.fisico,
                valor=1.0,
                data_hora=datetime.utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        movimento = movement_counts_by_machine(
            db, [machine_with_movement, machine_without_movement], WINDOW_START, WINDOW_END
        )
        ultima_atividade = latest_activity_by_machine(db, [machine_with_movement, machine_without_movement])
    finally:
        db.close()

    assert movimento.get(machine_with_movement, 0) == 1
    assert movimento.get(machine_without_movement, 0) == 0
    assert machine_with_movement in ultima_atividade
    assert machine_without_movement not in ultima_atividade


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
