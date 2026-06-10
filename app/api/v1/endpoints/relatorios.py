from datetime import date, datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import HistoricoOperacao, Maquina, Transacao, VendaPagamento
from app.schemas.transacao import TransacaoOut

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


def _apply_transacao_periodo(
    query,
    periodo: str | None = None,
    data_inicio: str | None = None,
    data_fim: str | None = None,
):
    if data_inicio and data_fim:
        dt_inicio = datetime.fromisoformat(data_inicio)
        dt_fim = datetime.fromisoformat(data_fim)
        return query.filter(
            Transacao.data_hora >= dt_inicio,
            Transacao.data_hora <= dt_fim,
        )

    if periodo == "dia":
        hoje = date.today()
        return query.filter(func.date(Transacao.data_hora) == hoje)

    if periodo == "mes":
        hoje = date.today()
        return query.filter(func.extract("month", Transacao.data_hora) == hoje.month).filter(
            func.extract("year", Transacao.data_hora) == hoje.year
        )

    return query


def _resolve_date_window(
    periodo: str | None = None,
    data_inicio: str | None = None,
    data_fim: str | None = None,
):
    if data_inicio and data_fim:
        return (
            datetime.fromisoformat(data_inicio),
            datetime.fromisoformat(data_fim) + timedelta(days=1) - timedelta(microseconds=1),
        )

    hoje = date.today()
    if periodo == "dia":
        start = datetime.combine(hoje, datetime.min.time())
        end = datetime.combine(hoje, datetime.max.time())
        return start, end

    if periodo == "mes":
        start = datetime.combine(hoje.replace(day=1), datetime.min.time())
        end = datetime.combine(hoje, datetime.max.time())
        return start, end

    end = datetime.combine(hoje, datetime.max.time())
    start = end - timedelta(days=6)
    return start, end


def _real_payment_history_query(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime):
    query = db.query(HistoricoOperacao).filter(
        HistoricoOperacao.categoria == "PAGAMENTO",
        HistoricoOperacao.created_at >= start_dt,
        HistoricoOperacao.created_at <= end_dt,
    )
    if not machine_ids:
        return query.filter(HistoricoOperacao.id.is_(None))
    return query.filter(
        HistoricoOperacao.maquina_id.in_(machine_ids),
        or_(HistoricoOperacao.provider.is_(None), HistoricoOperacao.provider != "manual"),
        ~HistoricoOperacao.descricao.ilike("%lancado pelo painel%"),
    )


def _real_revenue_totals(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime) -> tuple[float, int]:
    if not machine_ids:
        return 0.0, 0
    vendas_query = db.query(VendaPagamento).filter(
        VendaPagamento.maquina_id.in_(machine_ids),
        VendaPagamento.created_at >= start_dt,
        VendaPagamento.created_at <= end_dt,
        VendaPagamento.conta_faturamento.is_(True),
    )
    vendas_total = vendas_query.with_entities(func.sum(VendaPagamento.valor_liquido)).scalar() or 0.0
    vendas_count = (
        vendas_query.filter(VendaPagamento.conta_ticket_medio.is_(True))
        .with_entities(func.count(VendaPagamento.id))
        .scalar()
        or 0
    )

    historicos_com_venda = db.query(VendaPagamento.historico_id).filter(VendaPagamento.historico_id.isnot(None))
    digital_query = _real_payment_history_query(db, machine_ids, start_dt, end_dt).filter(
        ~HistoricoOperacao.id.in_(historicos_com_venda)
    )
    digital_total = digital_query.with_entities(func.sum(HistoricoOperacao.valor)).scalar() or 0.0
    digital_count = digital_query.with_entities(func.count(HistoricoOperacao.id)).scalar() or 0

    transacoes_com_venda = db.query(VendaPagamento.transacao_id).filter(VendaPagamento.transacao_id.isnot(None))
    fisico_query = db.query(Transacao).filter(
        Transacao.maquina_id.in_(machine_ids),
        Transacao.tipo == "IN",
        Transacao.metodo == "FISICO",
        Transacao.data_hora >= start_dt,
        Transacao.data_hora <= end_dt,
        ~Transacao.id.in_(transacoes_com_venda),
    )
    fisico_total = fisico_query.with_entities(func.sum(Transacao.valor)).scalar() or 0.0
    fisico_count = fisico_query.with_entities(func.count(Transacao.id)).scalar() or 0
    return (
        float(vendas_total or 0.0) + float(digital_total or 0.0) + float(fisico_total or 0.0),
        int(vendas_count or 0) + int(digital_count or 0) + int(fisico_count or 0),
    )


@router.get("/faturamento")
def faturamento(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    id_hardware: str = None,
    periodo: str = "dia",
    data_inicio: str = None,
    data_fim: str = None,
):
    _, role, cliente_id = user
    maquinas_ids = [m.id_hardware for m in _maquina_query_por_usuario(db, role, cliente_id).all()]
    if id_hardware:
        maquinas_ids = [id_hardware] if id_hardware in maquinas_ids or role == "admin" else []

    start_dt, end_dt = _resolve_date_window(periodo, data_inicio, data_fim)
    total, _ = _real_revenue_totals(db, maquinas_ids, start_dt, end_dt)
    return {"faturamento": float(total)}


@router.get("/transacoes", response_model=List[TransacaoOut])
def listar_transacoes(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    id_hardware: str = None,
    tipo: str = None,
    metodo: str = None,
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
    limit: int = 100,
):
    _, role, cliente_id = user
    maquinas = _maquina_query_por_usuario(db, role, cliente_id).all()
    maquinas_por_id = {maquina.id_hardware: maquina for maquina in maquinas}
    maquinas_ids = list(maquinas_por_id.keys())

    query = db.query(Transacao)
    if role != "admin":
        query = query.filter(Transacao.maquina_id.in_(maquinas_ids))
    if id_hardware:
        query = query.filter(Transacao.maquina_id == id_hardware)
    if tipo:
        query = query.filter(Transacao.tipo == tipo.upper())
    if metodo:
        query = query.filter(Transacao.metodo == metodo.upper())

    query = _apply_transacao_periodo(
        query,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )

    transacoes = (
        query.order_by(Transacao.data_hora.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )

    return [
        {
            "id": transacao.id,
            "maquina_id": transacao.maquina_id,
            "maquina_nome": (
                maquinas_por_id.get(transacao.maquina_id).nome_local
                if maquinas_por_id.get(transacao.maquina_id)
                else None
            ),
            "tipo": transacao.tipo.value if hasattr(transacao.tipo, "value") else str(transacao.tipo),
            "metodo": transacao.metodo.value if hasattr(transacao.metodo, "value") else str(transacao.metodo),
            "valor": float(transacao.valor),
            "data_hora": transacao.data_hora,
        }
        for transacao in transacoes
    ]
