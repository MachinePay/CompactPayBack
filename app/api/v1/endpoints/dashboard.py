from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Maquina, Transacao, VendaPagamento
from app.services.maquinas_relatorio import (
    ONLINE_SIGNAL_WINDOW,
    apply_transacao_periodo,
    compute_financial_summary,
    compute_financial_summary_by_machine,
    daily_revenue_totals,
    latest_activity_by_machine,
    movement_counts_by_machine,
    real_revenue_totals,
    resolve_date_window,
    sum_financial_summaries,
)

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _maquina_query_por_usuario(db: Session, role: str, cliente_id):
    if role == "admin":
        return db.query(Maquina)
    return db.query(Maquina).filter(Maquina.cliente_id == cliente_id)


def _total_dinheiro_fisico(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime) -> float:
    if not machine_ids:
        return 0.0

    vendas_fisicas = db.query(VendaPagamento).filter(
        VendaPagamento.maquina_id.in_(machine_ids),
        VendaPagamento.created_at >= start_dt,
        VendaPagamento.created_at <= end_dt,
        VendaPagamento.conta_faturamento.is_(True),
        or_(VendaPagamento.origem == "fisico", VendaPagamento.provider == "fisico"),
    )
    total_vendas = vendas_fisicas.with_entities(func.sum(VendaPagamento.valor_liquido)).scalar() or 0.0

    transacoes_com_venda = db.query(VendaPagamento.transacao_id).filter(VendaPagamento.transacao_id.isnot(None))
    fisico_legado = db.query(Transacao).filter(
        Transacao.maquina_id.in_(machine_ids),
        Transacao.tipo == "IN",
        Transacao.metodo == "FISICO",
        Transacao.data_hora >= start_dt,
        Transacao.data_hora <= end_dt,
        ~Transacao.id.in_(transacoes_com_venda),
    )
    total_legado = fisico_legado.with_entities(func.sum(Transacao.valor)).scalar() or 0.0
    return float(total_vendas or 0.0) + float(total_legado or 0.0)


@router.get("/dashboard/stats")
def dashboard_stats(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    hoje = date.today()
    start_dt = datetime.combine(hoje, datetime.min.time())
    end_dt = datetime.combine(hoje, datetime.max.time())
    _, role, cliente_id = user
    query = db.query(Transacao)
    maquinas_ids = [m.id_hardware for m in _maquina_query_por_usuario(db, role, cliente_id).all()]
    if role != "admin":
        query = query.filter(Transacao.maquina_id.in_(maquinas_ids))

    faturamento, _ = real_revenue_totals(db, maquinas_ids, start_dt, end_dt)
    premios = (
        query.with_entities(func.count(Transacao.id))
        .filter(
            Transacao.tipo == "OUT",
            func.date(Transacao.data_hora) == hoje,
        )
        .scalar()
        or 0
    )
    return {
        "faturamento_total_dia": float(faturamento),
        "premios_entregues": int(premios),
    }


@router.get("/dashboard/overview")
def dashboard_overview(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
    cliente_id: int = None,
    id_hardware: str = None,
):
    _, role, user_cliente_id = user
    maquinas_query = _maquina_query_por_usuario(db, role, user_cliente_id)
    if role == "admin" and cliente_id is not None:
        maquinas_query = maquinas_query.filter(Maquina.cliente_id == cliente_id)
    if id_hardware:
        maquinas_query = maquinas_query.filter(Maquina.id_hardware == id_hardware)

    maquinas = maquinas_query.all()
    maquinas_ids = [maquina.id_hardware for maquina in maquinas]

    transacoes_query = db.query(Transacao).filter(Transacao.maquina_id.in_(maquinas_ids))
    transacoes_periodo = apply_transacao_periodo(
        transacoes_query,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )

    start_dt, end_dt = resolve_date_window(periodo, data_inicio, data_fim)

    # Uma unica leva de queries agregadas para todas as maquinas do filtro, em vez
    # de uma chamada por maquina/cliente (evita N+1 quando a frota cresce).
    resumo_por_maquina = compute_financial_summary_by_machine(db, maquinas_ids, start_dt, end_dt)
    resumo_periodo = sum_financial_summaries(list(resumo_por_maquina.values()))
    faturamento = resumo_periodo["faturamento_total"]
    total_fisico = resumo_periodo["faturamento_fisico"]
    premios = (
        transacoes_periodo.filter(Transacao.tipo == "OUT")
        .with_entities(func.count(Transacao.id))
        .scalar()
        or 0
    )

    hoje = date.today()
    inicio_hoje = datetime.combine(hoje, datetime.min.time())
    fim_hoje = datetime.combine(hoje, datetime.max.time())
    inicio_mes = datetime.combine(hoje.replace(day=1), datetime.min.time())
    fim_mes = fim_hoje
    resumo_hoje = compute_financial_summary(db, maquinas_ids, inicio_hoje, fim_hoje)
    resumo_mes = compute_financial_summary(db, maquinas_ids, inicio_mes, fim_mes)

    agora = datetime.utcnow()
    maquinas_online = [
        maquina
        for maquina in maquinas
        if maquina.ultimo_sinal and (agora - maquina.ultimo_sinal) < ONLINE_SIGNAL_WINDOW
    ]
    ticket_medio = resumo_periodo["ticket_medio"]

    daily_totals = daily_revenue_totals(db, maquinas_ids, start_dt, end_dt)
    total_days = max(1, (end_dt.date() - start_dt.date()).days + 1)
    chart_data = []
    for index in range(total_days):
        current_day = start_dt.date() + timedelta(days=index)
        chart_data.append(
            {
                "dia": current_day.strftime("%d/%m"),
                "valor": float(daily_totals.get(current_day, 0.0)),
            }
        )

    movimento_por_maquina = movement_counts_by_machine(db, maquinas_ids, start_dt, end_dt)
    zero_movement = [
        maquina for maquina in maquinas if movimento_por_maquina.get(maquina.id_hardware, 0) == 0
    ]

    alerts = []
    for maquina in maquinas:
        if not maquina.ultimo_sinal or (agora - maquina.ultimo_sinal) >= ONLINE_SIGNAL_WINDOW:
            alerts.append(
                {
                    "title": f"Verificar conectividade da {maquina.nome_local or maquina.id_hardware}",
                    "status": "Offline",
                    "tone": "error",
                }
            )
    for maquina in zero_movement[:4]:
        alerts.append(
            {
                "title": f"Sem movimento em {maquina.nome_local or maquina.id_hardware}",
                "status": "Analise",
                "tone": "warning",
            }
        )

    if not alerts:
        alerts = [
            {
                "title": "Operacao estavel no periodo selecionado",
                "status": "Normal",
                "tone": "success",
            }
        ]

    clientes_resumo = []
    clientes_map = {}
    for maquina in maquinas:
        key = maquina.cliente_id or 0
        if key not in clientes_map:
            clientes_map[key] = {
                "cliente_id": maquina.cliente_id,
                "cliente_nome": maquina.dono.nome_empresa if getattr(maquina, "dono", None) else "Sem cliente",
                "maquinas": [],
                "maquinas_online": 0,
            }
        clientes_map[key]["maquinas"].append(maquina)
        if maquina.ultimo_sinal and (agora - maquina.ultimo_sinal) < ONLINE_SIGNAL_WINDOW:
            clientes_map[key]["maquinas_online"] += 1

    ultima_atividade_por_maquina = latest_activity_by_machine(db, maquinas_ids)

    for item in clientes_map.values():
        machine_ids = [maquina.id_hardware for maquina in item["maquinas"]]
        resumo_cliente = sum_financial_summaries([resumo_por_maquina[mid] for mid in machine_ids])
        atividades = [ultima_atividade_por_maquina[mid] for mid in machine_ids if mid in ultima_atividade_por_maquina]
        ultima_atividade_em = max(atividades) if atividades else None
        clientes_resumo.append(
            {
                "cliente_id": item["cliente_id"],
                "cliente_nome": item["cliente_nome"],
                "total_faturado": resumo_cliente["faturamento_total"],
                "faturamento_fisico": resumo_cliente["faturamento_fisico"],
                "faturamento_digital": resumo_cliente["faturamento_digital"],
                "ticket_medio": resumo_cliente["ticket_medio"],
                "testes_count": resumo_cliente["testes_count"],
                "testes_valor": resumo_cliente["testes_valor"],
                "estornos_count": resumo_cliente["estornos_count"],
                "estornos_valor": resumo_cliente["estornos_valor"],
                "pulsos_ausentes": resumo_cliente["pulsos_ausentes"],
                "maquinas": len(item["maquinas"]),
                "maquinas_online": item["maquinas_online"],
                "ultima_atividade_em": ultima_atividade_em,
            }
        )

    clientes_resumo.sort(key=lambda item: item["total_faturado"], reverse=True)

    maquinas_resumo = []
    for maquina in maquinas:
        resumo_maquina = resumo_por_maquina[maquina.id_hardware]
        maquinas_resumo.append(
            {
                "id_hardware": maquina.id_hardware,
                "nome": maquina.nome_local,
                "cliente_nome": maquina.dono.nome_empresa if getattr(maquina, "dono", None) else "Sem cliente",
                "status_online": bool(
                    maquina.ultimo_sinal and (agora - maquina.ultimo_sinal) < ONLINE_SIGNAL_WINDOW
                ),
                "total_faturado": resumo_maquina["faturamento_total"],
                "faturamento_fisico": resumo_maquina["faturamento_fisico"],
                "faturamento_digital": resumo_maquina["faturamento_digital"],
                "ticket_medio": resumo_maquina["ticket_medio"],
                "testes_count": resumo_maquina["testes_count"],
                "testes_valor": resumo_maquina["testes_valor"],
                "estornos_count": resumo_maquina["estornos_count"],
                "estornos_valor": resumo_maquina["estornos_valor"],
                "pulsos_ausentes": resumo_maquina["pulsos_ausentes"],
            }
        )
    maquinas_resumo.sort(key=lambda item: item["total_faturado"], reverse=True)

    return {
        "stats": {
            "faturamento_total": float(faturamento),
            "faturamento_hoje": resumo_hoje["faturamento_total"],
            "faturamento_mes": resumo_mes["faturamento_total"],
            "total_fisico": float(total_fisico),
            "faturamento_digital": resumo_periodo["faturamento_digital"],
            "premios_entregues": int(premios),
            "maquinas_ativas": len(maquinas_online),
            "total_maquinas": len(maquinas),
            "ticket_medio": float(ticket_medio),
            "percentual_ativas": round((len(maquinas_online) / len(maquinas)) * 100, 1) if maquinas else 0.0,
            "alertas": len(alerts),
            "testes_count": resumo_periodo["testes_count"],
            "testes_valor": resumo_periodo["testes_valor"],
            "estornos_count": resumo_periodo["estornos_count"],
            "estornos_valor": resumo_periodo["estornos_valor"],
            "pulsos_ausentes": resumo_periodo["pulsos_ausentes"],
        },
        "chart_data": chart_data,
        "alerts": alerts[:4],
        "clientes_resumo": clientes_resumo[:8],
        "maquinas_resumo": maquinas_resumo[:12],
    }
