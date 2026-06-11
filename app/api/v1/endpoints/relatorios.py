from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Maquina, Transacao
from app.schemas.transacao import TransacaoOut
from app.services.maquinas_relatorio import apply_transacao_periodo, real_revenue_totals, resolve_date_window

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

    start_dt, end_dt = resolve_date_window(periodo, data_inicio, data_fim)
    total, _ = real_revenue_totals(db, maquinas_ids, start_dt, end_dt)
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

    query = apply_transacao_periodo(
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
