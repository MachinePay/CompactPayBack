

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.models.models import Maquina, Transacao
from app.schemas.maquina import MaquinaOut
from app.schemas.transacao import TransacaoOut
from datetime import date
from sqlalchemy import func
from typing import List
from app.core.dependencies import get_current_user


from app.api.v1.endpoints import auth, usuarios, produtos
router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


from datetime import datetime, timedelta, date
from sqlalchemy import func

@router.get("/maquinas", response_model=List[MaquinaOut])
def listar_maquinas(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _, role, cliente_id = user
    if role == "admin":
        maquinas = db.query(Maquina).all()
    else:
        maquinas = db.query(Maquina).filter(Maquina.cliente_id == cliente_id).all()
    # Considera online se recebeu sinal nos últimos 3 minutos
    agora = datetime.utcnow()
    for m in maquinas:
        m.status_online = (m.ultimo_sinal and (agora - m.ultimo_sinal) < timedelta(minutes=3))
    return maquinas



# Endpoint de faturamento
@router.get("/faturamento")
def faturamento(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    id_hardware: str = None,
    periodo: str = "dia",
    data_inicio: str = None,
    data_fim: str = None
):
    from datetime import datetime
    _, role, cliente_id = user
    query = db.query(Transacao)
    if role != "admin":
        maquinas_ids = [m.id_hardware for m in db.query(Maquina).filter(Maquina.cliente_id == cliente_id)]
        query = query.filter(Transacao.maquina_id.in_(maquinas_ids))
    if id_hardware:
        query = query.filter(Transacao.maquina_id == id_hardware)
    if data_inicio and data_fim:
        dt_inicio = datetime.fromisoformat(data_inicio)
        dt_fim = datetime.fromisoformat(data_fim)
        query = query.filter(Transacao.data_hora >= dt_inicio, Transacao.data_hora <= dt_fim)
    elif periodo == "dia":
        hoje = date.today()
        query = query.filter(func.date(Transacao.data_hora) == hoje)
    elif periodo == "mes":
        hoje = date.today()
        query = query.filter(func.extract('month', Transacao.data_hora) == hoje.month)
        query = query.filter(func.extract('year', Transacao.data_hora) == hoje.year)
    total = query.with_entities(func.sum(Transacao.valor)).scalar() or 0.0
    return {"faturamento": float(total)}

# Incluir rotas de usuários e produtos
router.include_router(auth.router)
router.include_router(usuarios.router)
router.include_router(produtos.router)

@router.get("/dashboard/stats")
def dashboard_stats(db: Session = Depends(get_db)):
    hoje = date.today()
    faturamento = db.query(func.sum(Transacao.valor)).filter(
        Transacao.tipo == "IN",
        func.date(Transacao.timestamp) == hoje
    ).scalar() or 0.0
    premios = db.query(func.count(Transacao.id)).filter(
        Transacao.tipo == "OUT",
        func.date(Transacao.timestamp) == hoje
    ).scalar() or 0
    return {"faturamento_total_dia": faturamento, "premios_entregues": premios}
